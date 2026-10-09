# -*- coding: utf-8 -*-
"""把收到的群消息转发给服务商的面板

★★ 两条铁律，改这个文件之前先读一遍：

  1. **绝不能拖慢记账。** `on_message()` 只往内存队列里塞一下就立刻返回，
     真正的发送全在后台线程里做。服务商那边挂了、DNS 不通、网络卡死 ——
     **记账功能一点都不受影响**（记账是本职，转发只是附带）。
  2. **失败就丢，不堆积。** 重试几次还不行就扔掉并记日志。
     消息记录是附带功能，不值得为它把内存吃光。

图片：在这边用自己的 token 下好，**单独** POST 过去。
（塞进消息的 JSON 里的话，一张 10MB 的图会让请求体和内存都爆掉。）

★★ 两条来路**都要转**（少一条就是功能缺失）：
   ① 群里**别人**说的话 —— getUpdates 给我们的，走 on_message
   ② 机器人**自己**发的（账单！）—— getUpdates **不给**，只能挂在
      api.on_sent 上截。少了这条，面板上就只剩群友闲聊，
      **最该看的账单一条都没有**（2026-09-29 客户那边实测就是这样）

关掉转发：config.json 里 `report.enabled` 改成 false，重启即可。
"""
import collections
import json
import os
import threading
import time
from urllib.parse import urlsplit

import requests

MAX_MEDIA_BYTES = 10 * 1024 * 1024          # 单张图上限（跟对面一致）
IMAGE_MIMES = ('image/jpeg', 'image/png', 'image/webp')


def _log(msg):
    """记日志。★ 尽量用主程序的 log()，用不了就退回到 print ——
    转发出问题时**必须留下痕迹**，不然用户只看到"消息没同步"却无从查起。"""
    try:
        import core
        core.log(msg)
    except Exception:
        print('[转发] %s' % msg)


def is_private(update):
    """这条更新是私聊吗？

    ★★ 私聊**不转发**（2026-09-29 用户要求只记群聊）：
      私聊机器人的内容 —— 绑定管理员、广播、查地址 —— 全是噪音，
      混进群消息记录里，翻起来找不到真正要看的东西。

    ★ 顺带也是隐私：私人对话根本没必要离开客户那台服务器。
      在**源头**就拦住，比传到对面再丢掉干净。
    """
    if not isinstance(update, dict):
        return False
    msg = (update.get('message') or update.get('edited_message')
           or update.get('channel_post') or {})
    return ((msg.get('chat') or {}).get('type') or '') == 'private'


class Reporter:
    """后台转发线程。一个进程一个。"""

    def __init__(self, cfg, bid, token):
        cfg = cfg if isinstance(cfg, dict) else {}
        self.url = str(cfg.get('url') or '').rstrip('/')
        self.enabled = bool(cfg.get('enabled', True))
        self.bid = str(bid or '')
        self.token = str(token or '')
        # ★★ 回传暗号：**从机器人自己的 token 现算**，没有单独的密钥要管。
        #    服务商那边也这么算，两边对得上就行 —— 谁都不用生成/保存/复制。
        #    （不直接发 token 是因为 token 能完全接管机器人；这个是单向的）
        try:
            import core
            self.sig = core.ingest_sig(self.bid, self.token)
        except Exception:
            self.sig = ''
        self.timeout = int(cfg.get('timeout') or 10)
        # ★ 队列满了丢**最老的**：宁可少几条，不能把客户的内存吃光
        self.max_queue = int(cfg.get('max_queue') or 500)
        self.batch = max(1, int(cfg.get('batch') or 20))
        self.interval = float(cfg.get('interval') or 3)
        self.q = collections.deque()
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.thread = None
        self.config_thread = None
        # 机器人本体（入口调 attach() 挂上来）—— 要读它的绑定状态报给服务商
        self.runner = None
        self._last_state = None
        # 统计（日志里会打，方便排查）
        self.sent = 0
        self.dropped = 0
        self.failed = 0

    # -------- 能不能发 --------
    def ready(self):
        return bool(self.enabled and self.url and self.sig)

    def why_not(self):
        """不能发的原因（给入口启动时打日志用）"""
        if not self.enabled:
            return '配置里 report.enabled 是 false'
        if not self.url:
            return '没填 report.url'
        if not self.sig:
            return '拿不到机器人 token（算不出回传暗号）'
        return ''

    # -------- 收 --------
    def push(self, update):
        """★ 只塞队列，立刻返回 —— 这里**绝对不能**发网络请求"""
        if not self.ready() or not isinstance(update, dict):
            return
        if is_private(update):
            return              # ★ 私聊不转发，见 is_private 的说明
        with self.lock:
            if len(self.q) >= self.max_queue:
                self.q.popleft()
                self.dropped += 1
            self.q.append(update)

    # -------- 后台线程 --------
    def start(self):
        if not self.ready():
            _log('转发没开：%s' % self.why_not())
            return
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._run, name='relay',
                                       daemon=True)
        self.thread.start()
        _log('转发已开：%s（机器人 %s）' % (self.url, self.bid))

    def stop(self):
        self.stopped.set()

    # -------- 把绑定状态报给服务商 --------
    def attach(self, runner):
        """入口把机器人本体挂上来 —— 后台线程要读它的绑定状态"""
        self.runner = runner
        self._last_state = None         # 挂了就立刻报一次
        self._hook_sent(runner)
        from customer_ui import base_url
        endpoint = urlsplit(self.url)
        secure = endpoint.scheme == 'https' or (endpoint.scheme == 'http' and endpoint.hostname in ('127.0.0.1', 'localhost', '::1'))
        mgr = getattr(runner, 'mgr', None)
        if self.ready() and secure and mgr and base_url(mgr) and not self.config_thread:
            self.config_thread = threading.Thread(target=self._run_config, name='customer-config', daemon=True)
            self.config_thread.start()

    def _config_result(self, job):
        """Validate Telegram identity again and use the same hosted actions."""
        import miniapp
        import sqlite3
        from decimal import InvalidOperation
        try:
            if not isinstance(job, dict) or job.get('expires', 0) <= time.time():
                raise ValueError('请求已过期，请刷新确认结果后再操作')
            body = job['body']
            if not isinstance(body, dict) or not isinstance(body.get('payload', {}), dict):
                raise ValueError('请求格式无效')
            bot = self.runner.bot
            uid = miniapp.identity(body.get('init_data'), bot['token'])
            if not miniapp.owner_id(bot):
                raise PermissionError('尚未激活，请先私聊机器人发送 /admin 激活码')
            data = miniapp.action(self.runner.mgr, bot, uid, job['action'], body.get('payload', {}))
            result, status = dict(ok=True, data=data), 200
        except PermissionError as e:
            result, status = dict(ok=False, error=str(e)), 403
        except (ValueError, TypeError, KeyError, InvalidOperation) as e:
            message = str(e) if isinstance(e, ValueError) and not isinstance(e, json.JSONDecodeError) else '请求格式无效'
            result, status = dict(ok=False, error=message), 400
        except (sqlite3.Error, OSError):
            result, status = dict(ok=False, error='数据暂不可用，请稍后重试'), 503
        return dict(id=job['id'], result=result, status=status)

    def _run_config(self):
        # Separate from media/report uploads: a slow image must not stall settings.
        with requests.Session() as sess:
            reply = None
            while not self.stopped.is_set():
                try:
                    response = sess.post(self.url + '/api/ingest/config', json={'reply': reply or {}},
                                         headers={'X-Ingest-Sig': self.sig}, timeout=(5, 15), allow_redirects=False)
                    response.raise_for_status()
                    body = response.json()
                    if not body.get('ok'):
                        raise ValueError('配置连接未就绪')
                    job = body.get('request')
                    reply = self._config_result(job) if job else None
                except Exception:
                    # Retain the completed response for delivery, never rerun its write.
                    self.stopped.wait(2)

    # -------- ★★ 机器人自己发的消息（账单）--------
    def _hook_sent(self, runner):
        """钩上 `api.on_sent` —— 这是拿到「机器人自己发的消息」的**唯一**办法。

        ★ 为什么非要它：Telegram 的 getUpdates **不返回机器人自己发的消息**。
          账单、回执这些全是机器人自己发的，不主动截就永远传不过来。
          （面板版也是这么干的：core.BaseRunner.archive_sent）

        ★ 用**包装**而不是直接赋值：core 已经在 on_sent 上挂了它自己的归档。
          直接覆盖会把人家的钩子弄丢 —— 独立版里归档是关的，但那是
          config 的事，不该靠它来保证。
        """
        api = getattr(runner, 'api', None)
        if api is None:
            return
        prev = getattr(api, 'on_sent', None)
        if getattr(prev, '_relay_hooked', False):
            return                      # 已经钩过了，别套两层
        rep = self

        def on_sent(method, params, result):
            if prev is not None:
                try:
                    prev(method, params, result)
                except Exception:
                    pass                # 别人的钩子坏了不关转发的事
            try:
                rep.push_sent(result, params)
            except Exception:
                pass                # ★ 转发出岔子绝不能影响发消息本身

        on_sent._relay_hooked = True
        api.on_sent = on_sent

    def push_sent(self, result, params):
        """机器人刚发出去一条 → 当成一条 update 塞进队列。

        ★ 形状必须跟收消息那边**一模一样**（`{'message': {...}}`）——
          服务端就是拿它直接喂 archive.record_result 的。
        ★ 按 HTML 发的（账单就是），回来的 text 是**原始 HTML**：
          不转成纯文本的话，面板上看到的是
          「<a href="https://t.me/">1100</a>」这种东西，根本没法读。
          这段逻辑跟面板版 core.archive_sent 是同一套。
        """
        if not isinstance(result, dict) or not result.get('message_id'):
            return
        msg = dict(result)
        if str((params or {}).get('parse_mode') or '').upper() == 'HTML':
            try:
                import core
                for k in ('text', 'caption'):
                    if msg.get(k):
                        msg[k] = core.html_to_text(msg[k])
            except Exception:
                pass
        # ★ 标明是机器人自己发的：服务端靠它换一种显示样式，
        #   而且**不算未读** —— 自己发的账单不该让面板一直冒红点
        frm = dict(msg.get('from') or {})
        frm['is_bot'] = True
        msg['from'] = frm
        self.push({'message': msg})

    def _state(self):
        b = (getattr(self.runner, 'bot', None) or {}) if self.runner else {}
        ids = []
        for x in (b.get('admin_ids') or []):
            try:
                ids.append(int(x))
            except (TypeError, ValueError):
                pass
        return {'admin_ids': ids,
                'owner_id': int(b.get('owner_id') or 0),
                'admin_name': str(b.get('admin_name') or ''),
                'admin_username': str(b.get('admin_username') or '')}

    def _report_state(self, sess):
        """★★ 把「谁绑定了」告诉服务商。

        为什么必须报：绑定是在**这台机器上**发生的，管理员 id 存在
        data/<id>.state.json 里。服务商的面板自己那份永远是空的 ——
        不报回去的话，面板就永远显示「等待绑定」，客户明明已经绑好了。
        （2026-09-29 第一次真给客户装，卡在这儿）

        ★ 变了才报（每轮比一下），没变化不占带宽。
        ★ 发不出去就算了 —— 下次循环还会再试，绝不影响记账。
        """
        if self.runner is None:
            return
        try:
            st = self._state()
        except Exception:
            return
        if st == self._last_state:
            return
        try:
            r = sess.post(self.url + '/api/ingest/state', json=st,
                          headers={'X-Ingest-Sig': self.sig},
                          timeout=self.timeout)
            if r.status_code == 200:
                self._last_state = st
                if st.get('admin_ids'):
                    _log('已把绑定状态报给服务商')
        except Exception:
            pass

    def _run(self):
        sess = requests.Session()
        while not self.stopped.is_set():
            self._report_state(sess)
            if not self.q:
                self.stopped.wait(self.interval)
                continue
            batch = []
            with self.lock:
                while self.q and len(batch) < self.batch:
                    batch.append(self.q.popleft())
            self._send(sess, batch)
            self.stopped.wait(0.5)

    def _send(self, sess, batch):
        """发一批。★ 重试 3 次还不行就丢掉，绝不无限重试。"""
        payload = json.dumps({'events': batch}, ensure_ascii=False).encode()
        last = ''
        for attempt in range(3):
            if self.stopped.is_set():
                return
            if attempt:
                time.sleep(min(2 * attempt, 6))
            try:
                r = sess.post(self.url + '/api/ingest', data=payload,
                              headers={'X-Ingest-Sig': self.sig,
                                       'Content-Type': 'application/json'},
                              timeout=self.timeout)
                if r.status_code == 200:
                    try:
                        ok = bool((r.json() or {}).get('ok'))
                    except ValueError:
                        ok = False
                    if ok:
                        self.sent += len(batch)
                        self._send_photos(sess, batch)
                        return
                    last = r.text[:80]
                elif r.status_code == 403:
                    # ★ 密钥不对 / 机器人到期或停用了 —— **再试也没用**，
                    #   直接丢，别在这儿死循环刷屏
                    _log('转发被服务端拒收（HTTP 403，%s）。检查密钥，'
                         '或者问问是不是该续费了。丢掉 %d 条'
                         % ((r.text or '')[:80], len(batch)))
                    self.dropped += len(batch)
                    return
                else:
                    last = 'HTTP %s' % r.status_code
            except Exception as e:
                last = str(e)
        self.failed += 1
        self.dropped += len(batch)
        _log('转发失败（%s），试了 3 次，丢掉 %d 条' % (last, len(batch)))

    # -------- 图片 --------
    def _photo_of(self, item):
        """从一条 update 里找出图片信息。返回 (chat_id, message_id, file_id)
        或 None。★ 判断规则跟服务端的 archive.record_result 保持一致。"""
        msg = item.get('message') or item.get('edited_message') or \
            item.get('channel_post') or {}
        chat = msg.get('chat') or {}
        if not (chat.get('id') and msg.get('message_id')):
            return None
        photo = (msg.get('photo') or [{}])[-1]
        if not photo:
            doc = msg.get('document') or {}
            if doc.get('mime_type') in IMAGE_MIMES:
                photo = doc
        fid = str(photo.get('file_id') or '')
        if not fid:
            return None
        return str(chat['id']), str(msg['message_id']), fid

    def _send_photos(self, sess, batch):
        """把这一批里有图的下载下来单独传过去。

        ★ 下不动 / 传不动**就算了** —— 消息本身已经进去了，
          面板上那张图会一直显示「[图片]」。转发失败不影响记账。
        """
        if not self.token:
            return
        for item in batch:
            if self.stopped.is_set():
                return
            got = self._photo_of(item)
            if not got:
                continue
            cid, mid, fid = got
            try:
                data, suffix = self._download(sess, fid)
                if not data:
                    continue
                r = sess.post(self.url + '/api/ingest/media', data=data,
                              headers={'X-Ingest-Sig': self.sig,
                                       'X-Chat': cid, 'X-Msg': mid,
                                       'X-File-Id': fid, 'X-Suffix': suffix,
                                       'Content-Type': 'application/octet-stream'},
                              timeout=max(self.timeout, 30))
                if r.status_code != 200:
                    _log('图片没传上去（HTTP %s）：%s/%s'
                         % (r.status_code, cid, mid))
            except Exception as e:
                _log('图片处理失败（%s）：%s/%s' % (e, cid, mid))

    def _download(self, sess, file_id):
        """走 Telegram 官方 getFile 两步下载。返回 (字节, 后缀)。

        ★ 跟服务端 archive._download 是同一套逻辑 —— 这边是独立版，
          不带 archive.py，所以这里重写了一份（只有二十来行）。
        """
        r = sess.post('https://api.telegram.org/bot%s/getFile' % self.token,
                      json={'file_id': file_id}, timeout=15)
        r.raise_for_status()
        info = (r.json() or {}).get('result') or {}
        remote = info.get('file_path') or ''
        if not remote or (info.get('file_size') or 0) > MAX_MEDIA_BYTES:
            return b'', ''
        suffix = os.path.splitext(remote)[1].lower()
        if suffix not in ('.jpg', '.jpeg', '.png', '.webp'):
            return b'', ''
        # ★★ 必须用 sess.request(..., stream=True)，**不能**用 sess.stream()：
        #   在 requests 2.34 里 Session.stream 是个布尔属性，调它就报
        #   「'bool' object is not callable」。（服务端那边踩过同一个坑）
        with sess.request('GET', 'https://api.telegram.org/file/bot%s/%s'
                          % (self.token, remote), timeout=30,
                          stream=True) as stream:
            stream.raise_for_status()
            data = bytearray()
            for chunk in stream.iter_content(65536):
                data.extend(chunk)
                if len(data) > MAX_MEDIA_BYTES:
                    return b'', ''
        return bytes(data), suffix


# ================= 钩子（由 core 的定制文件机制调用）=================
_reporter = None


def setup(cfg, bid, token):
    """入口启动时调一次。cfg = config.json 里的 report 段"""
    global _reporter
    _reporter = Reporter(cfg, bid, token)
    _reporter.start()
    return _reporter


def attach(runner):
    """入口把机器人本体挂上来。

    ★★ 必须有个**模块级**的同名函数！`attach` 本来是 Reporter 类的方法，
      而入口写的是 `relay.attach(runner)`（模块级调用）—— 少这一层转发
      就会 `AttributeError: module 'relay' has no attribute 'attach'`，
      而且是在**启动那一行**抛，整个机器人起不来。
      （2026-09-29 给客户装完才发现，服务在崩溃重启循环里）
    """
    if _reporter is not None:
        _reporter.attach(runner)


def on_message(runner, u):
    """★ 记账机器人每收到一条 update 都会先过这里。

    这里**只塞队列**。返回 None = 不拦，记账流程照常往下走。
    （返回 True 的话会把这条消息从记账那边吃掉，绝对不要那么干！）
    """
    if _reporter is not None:
        # ★ 整段包住：转发里出任何岔子都不能把记账搞挂
        try:
            _reporter.push(u)
        except Exception:
            pass
    return None


def stats():
    if _reporter is None:
        return {}
    return {'sent': _reporter.sent, 'dropped': _reporter.dropped,
            'failed': _reporter.failed, 'queued': len(_reporter.q)}
