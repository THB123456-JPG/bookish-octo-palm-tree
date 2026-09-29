# -*- coding: utf-8 -*-
"""公共层：Telegram API 封装、工具函数、机器人运行基类

所有机器人（客服、USDT助手……）都继承 BaseRunner，
共用：绑定管理员、发消息、拉取更新、数据存取这几件事。
"""
import hashlib
import importlib.util
import json
import os
import random
import re
import string
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime

import requests

try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

# ================= 路径 =================
def _base_dir():
    if getattr(sys, 'frozen', False):
        # 打包后：配置文件放在 exe 旁边
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def resource_path(name):
    """读打包进 exe 的资源（如 panel_page.html）"""
    if getattr(sys, 'frozen', False):
        return os.path.join(getattr(sys, '_MEIPASS', _base_dir()), name)
    return os.path.join(_base_dir(), name)


BASE_DIR = _base_dir()
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')
BOTS_FILE = os.path.join(BASE_DIR, 'bots.json')
DATA_DIR = os.path.join(BASE_DIR, 'data')

# 某个机器人**独有**的定制代码：codes/<机器人id>.py
#   ★ 平时这个目录是空的 —— 没有这个文件 = 什么都没发生，跟以前一模一样。
#     只有「这个客户要个别人没有的功能」时才建一个，**只影响那一个机器人**。
#   ★ 故意放在 data/ 外面：data/ 是数据（不跟代码走），
#     codes/ 是代码（要跟项目一起部署到服务器）。
CODES_DIR = os.path.join(BASE_DIR, 'codes')
LOG_FILE = os.path.join(BASE_DIR, '运行日志.txt')

API_URL = 'https://api.telegram.org/bot{token}/{method}'

# 「群消息记录」只对**记账机器人**开放（用户 2026-09-28 最终指定）。
#   ★ 一开始还带上了客服机器人，后来用户说「客服不用加入进来了」——
#     客服是私聊转发，记账是群消息留档，两回事，别再加回去。
# 三处白名单必须一致：这里 / panel_page.html / messages_page.html
ARCHIVE_TYPES = ('ledger',)

# 机器人到期后收到消息时的统一回复
EXPIRED_MSG = ('⏰ 机器人已到期\n'
               '\n'
               '请及时续费。\n'
               '7 天内未续费，数据将被删除。')


# ================= 日志 =================
LOG_MAX_BYTES = 2 * 1024 * 1024     # 日志超过 2MB 就砍一半
_log_writes = 0


def _trim_log():
    """日志太大就砍掉前面一半，保留最近的

    ★ 为什么要有：客户自建那台的硬盘不大，而这个文件**只增不减**。
      实测一晚上的测试量就涨了 50KB，跑一年能到几十兆 ——
      对账本没影响，但没必要一直占着人家的地方。
    ★ 砍一半而不是清空：出问题时**最近的**记录最有用。
    ★ 从中间切之前先 readline() 丢掉半行，不然会留一条断头的记录。
    """
    try:
        size = os.path.getsize(LOG_FILE)
        if size <= LOG_MAX_BYTES:
            return
        with open(LOG_FILE, 'rb') as f:
            f.seek(size // 2)
            f.readline()                  # 扔掉被切成两半的那一行
            tail = f.read()
        with open(LOG_FILE, 'wb') as f:
            f.write('（日志太大，前面的已自动清理）\n'.encode('utf-8'))
            f.write(tail)
    except Exception:
        pass


def log(msg):
    global _log_writes
    line = '[%s] %s' % (datetime.now().strftime('%Y-%m-%d %H:%M:%S'), msg)
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        with open(LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(line + '\n')
        # ★ 每 200 条才查一次大小：日志涨得慢，晚几百条瘦身无所谓，
        #   每条都 stat 一次纯属浪费
        _log_writes += 1
        if _log_writes >= 200:
            _log_writes = 0
            _trim_log()
    except Exception:
        pass


class TgError(Exception):
    pass


# ================= Telegram API =================
# 这些方法发出去的是「消息」，发完要顺手归档一份（机器人收不到自己发的）
SEND_METHODS = ('sendMessage', 'sendPhoto', 'sendDocument', 'sendVideo',
                'sendVoice', 'sendAudio', 'sendAnimation', 'sendSticker')


class TgAPI:
    def __init__(self, token):
        self.token = token
        self.session = requests.Session()
        # ★ 发消息成功后的回调（runner 用它把机器人自己的回复也归档）
        #   为什么需要：getUpdates **不会**把机器人自己发的消息回给你，
        #   不主动记的话，账单/回执这些在消息记录里永远看不到
        self.on_sent = None

    def call(self, method, **params):
        url = API_URL.format(token=self.token, method=method)
        last_err = None
        for attempt in range(3):
            try:
                r = self.session.post(url, json=params, timeout=70)
            except requests.RequestException as e:
                last_err = e
                if attempt == 2:
                    raise TgError('网络错误：%s' % e)
                time.sleep(3)
                continue
            try:
                data = r.json()
            except ValueError:
                last_err = r.text[:200]
                if attempt == 2:
                    raise TgError('返回异常：%s' % last_err)
                time.sleep(3)
                continue
            if data.get('ok'):
                result = data.get('result')
                if self.on_sent and method in SEND_METHODS:
                    try:
                        self.on_sent(method, params, result)
                    except Exception:
                        # ★ 归档失败绝不能影响发消息本身
                        log('归档已发消息失败：\n%s' % traceback.format_exc())
                return result
            code = data.get('error_code')
            desc = data.get('description', '')
            if code == 429:
                wait = (data.get('parameters') or {}).get('retry_after', 5)
                log('触发限流，等待 %d 秒' % wait)
                time.sleep(wait + 1)
                last_err = '限流'
                continue
            raise TgError('%s %s' % (code, desc))
        raise TgError('多次重试仍失败：%s' % last_err)

    def call_file(self, method, field, filename, fileobj, **params):
        """上传文件（sendPhoto / sendDocument 用）。

        `call()` 走 JSON 发不了图，这个走 multipart。
        fileobj 要能 seek —— 重试时会重置到开头，不然第二次上传是空的。
        """
        url = API_URL.format(token=self.token, method=method)
        last_err = None
        for attempt in range(3):
            try:
                if hasattr(fileobj, 'seek'):
                    fileobj.seek(0)
                r = self.session.post(url, data=params,
                                      files={field: (filename, fileobj)},
                                      timeout=120)
            except requests.RequestException as e:
                last_err = e
                if attempt == 2:
                    raise TgError('上传失败：%s' % e)
                time.sleep(3)
                continue
            try:
                data = r.json()
            except ValueError:
                last_err = r.text[:200]
                if attempt == 2:
                    raise TgError('上传返回异常：%s' % last_err)
                time.sleep(3)
                continue
            if data.get('ok'):
                result = data.get('result')
                # ★ 上传发出去的也算「机器人发的」—— 账单是文字走 call()，
                #   但 TRC20 核对图这类是上传，不记的话记录里只看得到文字
                if self.on_sent and method in SEND_METHODS:
                    try:
                        self.on_sent(method, params, result)
                    except Exception:
                        log('归档已发消息失败：\n%s' % traceback.format_exc())
                return result
            code = data.get('error_code')
            desc = data.get('description', '')
            if code == 429:
                wait = (data.get('parameters') or {}).get('retry_after', 5)
                log('上传触发限流，等待 %d 秒' % wait)
                time.sleep(wait + 1)
                last_err = '限流'
                continue
            raise TgError('%s %s' % (code, desc))
        raise TgError('上传多次重试仍失败：%s' % last_err)


# ================= 工具 =================
# Telegram 允许的那几种 HTML 标签，去掉标签只留里面的字
_HTML_TAG = re.compile(
    r'</?(?:a|b|i|u|s|code|pre|em|strong|span|tg-spoiler)\b[^>]*>', re.I)
_HTML_ENTITIES = (('&lt;', '<'), ('&gt;', '>'), ('&quot;', '"'),
                  ('&#39;', "'"), ('&amp;', '&'))


def html_to_text(s):
    """把按 HTML 发的消息转回能读的纯文本

    ★ 为什么需要：记账的账单是拿 HTML 发的 —— 金额套一层
      `<a href="https://t.me/">1100</a>` 让它在 TG 里显示成蓝色。
      归档存的是**原始 HTML**，面板照原样显示就会变成
      「<a href="https://t.me/">1100</a>」这种尖括号乱码。

    只做「去标签 + 还原实体」，不解析复杂结构 —— Telegram 就允许这几个标签。
    ★ 顺序不能反：**先**去标签**再**还原实体。反过来的话，用户在群里
      打的 `&lt;b&gt;` 会先变成 `<b>`，然后被当成标签删掉。
    """
    if not s:
        return s
    for br in ('<br>', '<br/>', '<br />', '<BR>'):
        s = s.replace(br, '\n')
    s = _HTML_TAG.sub('', s)
    for a, b in _HTML_ENTITIES:
        s = s.replace(a, b)
    return s


def raw_name(u):
    n = ' '.join(x for x in [u.get('first_name'), u.get('last_name')] if x)
    if n:
        return n
    if u.get('username'):
        return '@' + u['username']
    return '用户%s' % u.get('id')


def tag_of(u):
    s = '【%s' % raw_name(u)
    if u.get('username'):
        s += ' @%s' % u['username']
    s += ' ID:%s】' % u.get('id')
    return s


def gen_id(n=8):
    return ''.join(random.choices('0123456789abcdef', k=n))


def gen_code(n=8):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=n))


def ingest_sig(bid, token):
    """「客户自建机器人」回传消息时，用来证明身份的那串东西。

    ★★ 为什么不另发一个随机密钥：
       用户看不懂「密钥」这个概念（他原话：「有没有简单点的啊，
       你说的那个密钥我没看懂」）。而这个是从**机器人自己的 token**
       算出来的 —— 服务商面板有 token、客户那边也有，**两边都能算**，
       于是**没有任何东西需要生成、保存、复制、解释**。

    ★★ 为什么不干脆直接把 token 发过去：
       机器人 token 是能**完全接管机器人**的（拿它发消息、读客户消息）。
       这个是**单向**的 —— 网上被人截到了也**反推不出 token**，
       最多只能往那台机器人的消息记录里灌数据。
       所以安全性跟「另发一个随机密钥」**一样**，但用户少一个概念。
    """
    raw = ('%s|%s' % (bid or '', token or '')).encode('utf-8')
    return hashlib.sha256(raw).hexdigest()


def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        log('读取 %s 失败：%s' % (os.path.basename(path), e))
        return default


_PATH_LOCKS = {}
_PATH_LOCKS_GUARD = threading.Lock()


def _path_lock(path):
    """拿「这个路径」的锁。同一个文件被谁写都串行。

    ★ 为什么按**路径**加锁而不是各自加锁：同一个文件会有**多个写入方** ——
      比如 `data/<id>.json` 会被机器人轮询线程和面板 HTTP 线程同时写，
      而且 `runners/shop/store.py` 里还有**第二份实现**写同一个路径。
      各自的锁锁不住对方，必须按路径共用一把。
    ★ 锁的数量跟路径数一样多，长期运行不会涨（路径是有限的几种）。
    """
    key = os.path.abspath(path)
    with _PATH_LOCKS_GUARD:
        lk = _PATH_LOCKS.get(key)
        if lk is None:
            lk = _PATH_LOCKS[key] = threading.RLock()
        return lk


def save_json(path, obj):
    """原子写：临时文件 → fsync → os.replace

    ★★ 这里踩过三个坑（2026-09-29 审计指出，我逐条核实过）：

      ① 原来是 `os.remove(path)` 再 `os.rename(tmp, path)` ——
         **中间那一瞬间文件是不存在的**。这时候进程被杀 / 断电，
         整个配置就没了（不是「旧内容」也不是「新内容」，是**没有**）。
         `os.replace` 是原子的：要么旧要么新，没有中间态。

      ② 临时文件名原来写死成 `path + '.tmp'` —— 两个人同时写会用
         **同一个临时文件**，内容互相交叠，写出来的 JSON 直接坏掉。
         改成 `mkstemp`（系统保证唯一）。

      ③ 原来没有 flush/fsync —— 数据还在系统缓存里就把文件改名了，
         断电可能留下一个**空文件**。

    ★ 函数签名和调用方**一个字都没改**，所有调用处不用动。
    """
    with _path_lock(path):
        d = os.path.dirname(os.path.abspath(path)) or '.'
        base = os.path.basename(path)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=base + '.', suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                _dump_json(obj, f)
                f.flush()
                os.fsync(f.fileno())      # ★ 真正落到盘上再改名
            _replace_retry(tmp, path)     # ★ 原子替换（不用先删）
        except Exception:
            # 写失败就把临时文件清掉，别留一地碎片
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def _replace_retry(tmp, path, times=8):
    """`os.replace` 失败就重试几次 —— **Windows 专属的坑**。

    ★ 症状：`PermissionError: [WinError 5] 拒绝访问。: '...tmp' -> '...json'`
    ★ 根因：Windows 不允许替换一个**正被别人打开着**的文件路径
      （Python 的 `open()` 不带 FILE_SHARE_DELETE）。所以只要**读的人**
      还没 close，写的人就替换不了 —— 这跟「两个写的人撞车」不是一回事，
      上面的路径锁拦不住它（读的时候没拿锁）。
      本机实测：8 个线程一边写一边读，几十轮就能撞上。
    ★ 生产里同样会发生：面板 HTTP 线程在读 `data/<id>.json`，
      机器人轮询线程正好在写同一个文件。
    ★ 重试是安全的：`os.replace` 本身幂等，重试就是再换一次名。
      读文件是瞬时的，几十毫秒就够它松手。
    """
    last = None
    for i in range(int(times)):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as e:       # WinError 5 / 32
            last = e
            time.sleep(0.03 * (i + 1))
    raise last


def _dump_json(obj, f):
    """★ 序列化 + 对付「边写边被别的线程改」这一个具体的坑。

    `_path_lock` 只拦得住**写入方之间**，拦不住「我在 dump、别人在往
    dict 里加键」。后者会让 json 抛
    `RuntimeError: dictionary changed size during iteration` ——
    结果就是这次保存**整个丢掉**（文件没坏，但这次改动没落盘）。
    面板 HTTP 线程和机器人轮询线程同时读写同一个 `data/<id>.json`
    时就会撞上。

    ★ 重试一次就够：对方的改动是瞬时的，第二次 dump 时它早完事了。
      还不行就照旧抛出去（宁可让调用方知道没存上，也不要静默丢数据）。
    """
    for i in (0, 1):
        try:
            json.dump(obj, f, ensure_ascii=False, indent=2)
            return
        except RuntimeError:
            if i:                       # 第二次还是不行 → 抛
                raise
            f.seek(0)
            f.truncate()


# ================= 每个机器人的定制代码 =================
def load_code(bid):
    """读 `codes/<机器人id>.py`，没有这个文件就返回 None。

    这是给**某一个机器人**开小灶用的：平时不用建，
    要单独定制某个客户时才建一个，碰不到任何其他机器人。

    文件里可以写（三个都是可选的，写哪个算哪个）：

        def on_start(runner):            机器人启动时跑一次
        def on_message(runner, u):       每条消息先过这里，
                                         返回 True = 我处理完了，不再走默认流程
        def on_tick(runner):             跟着 runner 的周期走（默认 30 秒）

    ★★ 安全底线：这个文件是**手写的**，写错了绝不能把机器人搞挂 ——
       所以这里任何异常都吞掉、只记日志、当作「没有这个文件」。
       机器人照常跑，只是少了那点定制功能。
       （同理，钩子执行出错也只记日志，见 BaseRunner.code_call）
    """
    path = os.path.join(CODES_DIR, '%s.py' % bid)
    if not os.path.isfile(path):
        return None
    try:
        spec = importlib.util.spec_from_file_location('botcode_%s' % bid, path)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        # ★ 模块名要放进 sys.modules —— 定制文件里如果用了 dataclass /
        #   类内自引用，Python 会回头查 sys.modules，不放会报错
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        log('[%s] 加载了定制文件 codes/%s.py' % (bid, bid))
        return mod
    except Exception:
        log('!! 定制文件 codes/%s.py 加载失败 —— **已忽略，机器人照常跑**：\n%s'
            % (bid, traceback.format_exc()))
        return None


# ================= 机器人基类 =================
class BaseRunner(threading.Thread):
    """一个机器人 = 一个线程。

    子类需要实现：
        on_owner(msg)   已绑定的管理员发来的消息
        on_guest(msg)   其他人发来的消息

    可选覆盖：
        on_tick()       周期性任务（比如扫链上交易）
        poll_timeout    getUpdates 长轮询秒数
        help_text()     /help 的内容
    """
    kind = 'base'
    poll_timeout = 20
    max_mapping = 5000
    # 需要收按钮点击（InlineKeyboard）的类型要带上 callback_query
    allowed_updates = ['message', 'callback_query']
    # ★ 默认**不收群消息**（客服/USDT/商城都只服务私聊，行为保持不变）。
    #   记账机器人要在群里干活，它自己覆写成 True 并实现 on_group()。
    allow_groups = False

    def __init__(self, manager, bot):
        super().__init__(daemon=True)
        self.mgr = manager
        self.bot = bot
        self.bid = bot['id']
        self.api = TgAPI(bot['token'])
        self.stop_evt = threading.Event()
        self.offset = 0
        self.me = None
        self._next_tick = 0.0
        self._archive = None        # 群消息记录，懒加载（sqlite 不能跨线程）

        # 发出去的消息也归档（机器人收不到自己发的，只能自己记）
        self.api.on_sent = self.archive_sent

        self.data_file = os.path.join(DATA_DIR, '%s.json' % self.bid)
        self.data = load_json(self.data_file, {})
        if not isinstance(self.data, dict):
            self.data = {}

        # 绑定者正在输入「要添加的操作人用户名」：aid -> 开始时间戳
        self._await_op = {}
        # 到期后给绑定者发提醒的去重表：客户ID -> 上次提醒时间
        self._expired_notice = {}

        # 这个机器人**独有**的定制代码（codes/<机器人id>.py）。
        # ★ 没有这个文件就是 None —— 什么都没发生，跟以前一模一样
        self.code = load_code(self.bid)

    # -------- 身份 --------
    def note(self):
        return self.bot.get('note') or self.bot.get('username') or self.bid

    def admins(self):
        """所有能用这个机器人的人：绑定者 + 被添加的操作人"""
        return set(int(x) for x in (self.bot.get('admin_ids') or []))

    def owner_id(self):
        """绑定者（第一个用绑定码绑定的那个人）。只有他能管操作人"""
        oid = self.bot.get('owner_id')
        if oid:
            return int(oid)
        ids = self.bot.get('admin_ids') or []
        return int(ids[0]) if ids else None

    def is_owner(self, aid):
        return self.owner_id() == aid

    def admin_names(self):
        return self.bot.setdefault('admin_names', {})

    def name_of(self, aid):
        return self.admin_names().get(str(aid)) or ('ID:%s' % aid)

    def add_admin(self, aid, user, as_operator=False):
        """as_operator=True 表示是绑定者手动添加的操作人，不会顶掉 owner"""
        with self.mgr.lock:
            ids = self.bot.get('admin_ids') or []
            if aid not in ids:
                ids.append(aid)
            self.bot['admin_ids'] = ids
            if not as_operator and not self.bot.get('owner_id'):
                self.bot['owner_id'] = aid
            names = self.admin_names()
            names[str(aid)] = raw_name(user)
            self.bot['admin_username'] = user.get('username') or self.bot.get('admin_username') or ''
            if self.is_owner(aid):
                self.bot['admin_name'] = raw_name(user)
                self.bot['bound_at'] = datetime.now().strftime('%Y-%m-%d %H:%M')
            self.mgr.save()

    def remove_admin(self, aid):
        """把某个操作人踢掉（绑定者除外）"""
        if self.is_owner(aid):
            return False
        with self.mgr.lock:
            ids = [x for x in (self.bot.get('admin_ids') or []) if int(x) != int(aid)]
            self.bot['admin_ids'] = ids
            self.admin_names().pop(str(aid), None)
            self.mgr.save()
        return True

    # -------- 邀请操作人 --------
    @property
    def pending_ops(self):
        """等着对方来「报到」的用户名 / 邀请码"""
        return self.data.setdefault('pending_ops', [])

    def add_pending_op(self, username, by):
        username = (username or '').lstrip('@').strip()
        code = gen_code(6).lower()
        self.pending_ops.append({
            'username': username.lower(),
            'display': username,
            'code': code,
            'by': by,
            'at': datetime.now().strftime('%Y-%m-%d %H:%M'),
        })
        self.save_data()
        return code

    def invite_link(self, code):
        un = (self.me or {}).get('username') or self.bot.get('username') or ''
        return 'https://t.me/%s?start=op_%s' % (un, code) if un else ''

    def accept_pending_op(self, cid, user):
        """有人发消息过来，看看是不是我们等着的操作人"""
        un = (user.get('username') or '').lower()
        if not un:
            return False
        for p in list(self.pending_ops):
            if p.get('username') and p['username'] == un:
                self.pending_ops.remove(p)
                self.add_admin(cid, user, as_operator=True)
                self.save_data()
                log('[%s] 操作人加入（按用户名匹配）：%s' % (self.note(), raw_name(user)))
                self.send(cid, '✅ 你已被添加为操作人，现在可以使用这个机器人了。')
                self.send(cid, self.help_text())
                oid = self.owner_id()
                if oid:
                    self.send(oid, '👥 %s（@%s）已加入为操作人'
                              % (raw_name(user), user.get('username') or ''))
                return True
        return False

    def accept_invite(self, cid, user, payload):
        """处理 /start op_XXXXXX 这种带邀请码的深链"""
        if not payload.startswith('op_'):
            return False
        code = payload[3:].strip().lower()
        for p in list(self.pending_ops):
            if p.get('code') and p['code'] == code:
                self.pending_ops.remove(p)
                self.add_admin(cid, user, as_operator=True)
                self.save_data()
                log('[%s] 操作人加入（按邀请链接）：%s' % (self.note(), raw_name(user)))
                self.send(cid, '✅ 你已被添加为操作人，现在可以使用这个机器人了。')
                self.send(cid, self.help_text())
                oid = self.owner_id()
                if oid:
                    self.send(oid, '👥 %s（@%s）已通过邀请链接加入为操作人'
                              % (raw_name(user), user.get('username') or ''))
                return True
        return False

    # -------- 状态 --------
    def set_status(self, status, error=''):
        with self.mgr.lock:
            self.bot['status'] = status
            self.bot['error'] = error
            self.mgr.save()

    # -------- 数据 --------
    def save_data(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        try:
            save_json(self.data_file, self.data)
        except Exception as e:
            log('[%s] 保存数据失败：%s' % (self.note(), e))

    # -------- 发消息 --------
    def send(self, chat_id, text, **kw):
        try:
            return self.api.call('sendMessage', chat_id=chat_id, text=text,
                                 disable_web_page_preview=True, **kw)
        except TgError as e:
            log('[%s] 发消息给 %s 失败：%s' % (self.note(), chat_id, e))
            return None

    def send_to_admins(self, text):
        out = []
        for aid in sorted(self.admins()):
            r = self.send(aid, text)
            if r:
                out.append((aid, r['message_id']))
        return out

    # -------- 主循环 --------
    def run(self):
        """线程入口。★ 别直接覆盖这个方法，要改循环就覆盖 _loop() ——
        on_stop() 的收尾必须保证被执行到（记账机器人的 sqlite 靠它关）"""
        try:
            self._loop()
        finally:
            # ★ 归档库是基类自己开的，基类自己关（而且必须在**这个线程**里关）
            try:
                self.close_archive()
            except Exception:
                log('[%s] 关归档出错：\n%s' % (self.note(), traceback.format_exc()))
            try:
                self.on_stop()
            except Exception:
                log('[%s] 收尾出错：\n%s' % (self.note(), traceback.format_exc()))

    def _loop(self):
        try:
            self.me = self.api.call('getMe')
        except Exception as e:
            self.set_status('error', 'token 无效或网络不通：%s' % e)
            log('[%s] 启动失败：%s' % (self.note(), e))
            return

        # 到期的机器人不能改成 running，要保持「已到期」的状态
        if self.expired():
            log('[%s] 已到期（@%s），只会回复到期提示'
                % (self.note(), self.me.get('username')))
        else:
            self.set_status('running')
            self.on_start()
            # 定制文件里的 on_start（可选）
            self.code_call('on_start', self)
            log('[%s] 已启动 @%s%s' % (self.note(), self.me.get('username'),
                                       '' if self.admins() else '（尚未绑定管理员）'))
            # ★ 开了群消息记录的话，开机就把归档拉起来。
            #   归档是懒加载的，不预热的话——重启之后要是群里一直没人说话，
            #   那些「还没下完的图」就永远轮不到下载（踩过）
            try:
                if self.archive_cfg().get('enabled'):
                    self.get_archive()
            except Exception as e:
                log('[%s] 预热消息记录失败：%s' % (self.note(), e))

        while not self.stop_evt.is_set():
            try:
                updates = self.api.call('getUpdates', offset=self.offset,
                                        timeout=self.poll_timeout,
                                        allowed_updates=self.allowed_updates)
            except TgError as e:
                msg = str(e)
                if '409' in msg or 'terminated by other' in msg:
                    self.set_status('conflict', '这个 token 正在别的地方运行，请关掉另一个')
                    log('[%s] 冲突：%s' % (self.note(), msg))
                    return
                self.set_status('error', msg)
                log('[%s] 拉取失败：%s' % (self.note(), msg))
                self.stop_evt.wait(5)
                continue

            # 注意：如果已经被要求停止、或者已经到期，
            # 就不要再把状态改回 running —— 那样会把「已到期」覆盖掉
            if (not self.stop_evt.is_set() and not self.expired()
                    and self.bot.get('status') != 'running'):
                self.set_status('running')

            for u in updates or []:
                self.offset = u['update_id'] + 1
                # ★ 定制文件先过一遍：返回 True = 它自己处理完了，
                #   不再走默认流程。offset 上面已经推过，continue 是安全的
                if self.code_call('on_message', self, u) is True:
                    continue
                try:
                    self.handle(u)
                except Exception:
                    log('[%s] 处理消息出错：\n%s' % (self.note(), traceback.format_exc()))

            # 周期性任务
            now = time.time()
            if now >= self._next_tick:
                try:
                    self.on_tick()
                except Exception:
                    log('[%s] 周期任务出错：\n%s' % (self.note(), traceback.format_exc()))
                # 定制文件里的 on_tick（可选）
                self.code_call('on_tick', self)
                self._next_tick = time.time() + self.tick_interval()

            self.stop_evt.wait(0.2 if updates else 0)

        # 是我们主动要求停的、或者已到期，就别改状态，
        # 由调用方决定该显示什么（比如「已到期」）
        if not self.stop_evt.is_set() and not self.expired():
            self.set_status('stopped')

    # -------- 可覆盖的钩子 --------
    def on_start(self):
        pass

    def on_tick(self):
        pass

    def tick_interval(self):
        return 30.0

    def code_call(self, name, *args):
        """调用定制文件（codes/<机器人id>.py）里的钩子。

        返回钩子的返回值；没有这个钩子、或者没有定制文件，都返回 None。
        ★ 钩子出错只记日志 —— 定制文件写坏了不能影响机器人本体。
        """
        fn = getattr(self.code, name, None) if self.code else None
        if not callable(fn):
            return None
        try:
            return fn(*args)
        except Exception:
            log('[%s] 定制文件里的 %s() 出错（**已忽略**）：\n%s'
                % (self.note(), name, traceback.format_exc()))
            return None

    def on_owner(self, msg):
        pass

    def on_guest(self, msg):
        pass

    def on_group(self, msg):
        """群消息。★ 只有 allow_groups=True 的类型会收到（默认收不到）

        注意：群消息**不会**走 on_owner / on_guest，也不会触发 /admin 绑定、
        /id 那些 —— 想按发言人区分「是不是管理员」，自己用 msg['from']['id']
        跟 self.admins() 比。
        """
        pass

    def on_stop(self):
        """线程要退出了，做收尾。

        ★★ 必须在**自己的线程里**做 —— sqlite 连接默认不允许跨线程使用，
           `close()` 从别的线程调会直接抛异常（被 except 一吞就变成
           「看起来关了、其实文件还锁着」，删都删不掉）。踩过。
        """
        pass

    # -------- 群消息记录（本地归档）--------
    def archive_cfg(self):
        """群消息记录的配置。

        ★ 这里是**唯一的闸口**：类型不在白名单里就直接当没开 ——
          wants_groups / record_archive / get_archive 全走它，
          所以加这一处就够了，不会漏。

        ★★ **默认是开的**（用户 2026-09-28 明确：「机器人默认都是开的，
           只有消息列表是我个人选择开或者关的」）。
           所以 `enabled` 字段**缺失 = 开**，只有**明确写 False** 才是关。
           关的粒度在「按群」（`mute_chats`），不是按机器人。
           —— 之前把「缺字段」当成「关」，结果新加的机器人永远不记录，
              群列表里又因为一条消息都没有而没有开关可点，直接卡死。
        """
        if (self.bot.get('type') or '') not in ARCHIVE_TYPES:
            return {}
        cfg = self.bot.get('archive')
        cfg = dict(cfg) if isinstance(cfg, dict) else {}
        cfg.setdefault('enabled', True)
        return cfg

    def wants_groups(self):
        """要不要收群消息。

        两种情况：
          · 类型天生就要收（记账机器人，`allow_groups = True`）
          · 这台在面板上开了「群消息记录」
        ★ 收群消息的前提是在 BotFather 关掉 Group Privacy，
          没关的话只能收到 @它 和 /命令（面板上有提示）
        """
        return bool(self.allow_groups or self.archive_cfg().get('enabled'))

    def get_archive(self):
        """群消息记录库。懒加载 —— ★ 只能在自己线程里第一次开（sqlite 不能跨线程）"""
        if self._archive is None and self.archive_cfg().get('enabled'):
            from archive import MessageArchive
            self._archive = MessageArchive(
                os.path.join(DATA_DIR, '%s.archive.sqlite3' % self.bid),
                token=self.bot.get('token') or '',
                owner_id=self.owner_id() or '')
            self._archive.start()
        return self._archive

    def record_archive(self, u, is_own=False):
        """把这条更新写进归档。没开记录的直接返回（开销只有一个 dict 取键）

        is_own=True 表示这是**机器人自己发出去的**（账单、回执这类），
        标记成 is_bot —— 界面上单独一种样式，而且**不算未读**
        （它是对别人消息的反应，不是新来的信息）。
        """
        cfg = self.archive_cfg()
        if not cfg.get('enabled'):
            return
        try:
            a = self.get_archive()
            if a:
                # 面板上按群「关闭记录」的，直接不写（不是存了再隐藏）
                a.record_result(u, mute=cfg.get('mute_chats') or (),
                                is_own=is_own)
        except Exception as e:
            log('[%s] 归档消息失败：%s' % (self.note(), e))

    def archive_sent(self, method, params, result):
        """机器人刚发出去一条消息 → 归档一份

        ★ 为什么要单独走这条路：Telegram 的 getUpdates **不返回机器人
          自己发的消息**，所以账单、回执这些不主动记的话，
          消息记录里永远只有别人说的话。
        """
        if not isinstance(result, dict) or not result.get('message_id'):
            return
        # ★ 按 HTML 发的（账单就是），result 里回来的是**原始 HTML** ——
        #   转成纯文本再存。不转的话面板上看到的是
        #   「<a href="https://t.me/">1100</a>」这种东西，根本没法读。
        #   只认 HTML；别的 parse_mode（Markdown 那种）不碰。
        if str(params.get('parse_mode') or '').upper() == 'HTML':
            msg = dict(result)
            for k in ('text', 'caption'):
                if msg.get(k):
                    msg[k] = html_to_text(msg[k])
            result = msg
        self.record_archive({'message': result}, is_own=True)

    def close_archive(self):
        if self._archive is not None:
            try:
                self._archive.close()
            except Exception as e:
                log('[%s] 关闭归档失败：%s' % (self.note(), e))
            self._archive = None

    def on_my_chat_member(self, mcm):
        """机器人**自己**被拉进群 / 被踢出群（不是普通成员进出）

        ★ 只有 allow_groups=True 的类型会收到（现在只有记账机器人）。
          想收这个更新，子类还要把 'my_chat_member' 加进 allowed_updates。

        mcm 是 update['my_chat_member']，结构：
          {'chat': {...}, 'from': {...}, 'old_chat_member': {'status': 'left'},
           'new_chat_member': {'status': 'member'}}
        status: left / kicked / member / administrator / restricted
        """
        pass

    def on_callback(self, cq):
        """按钮点击。默认什么都不做，需要的类型自己覆盖"""
        pass

    def allow_callback(self, cid):
        """谁能点按钮触发 on_callback。

        默认只有已绑定的管理员（客服/USDT助手是自用工具，够用）。
        ★ 面向公众的服务机器人（比如商城）必须覆写成 return True，
          否则普通客户点按钮会被静默丢掉，整个交互全废。
        """
        return cid in self.admins()

    def on_expired(self, cid, frm, is_admin):
        """机器人已到期时收到消息。默认直接回一句到期提示。

        子类可以覆盖（比如客服机器人不想让客户知道欠费，
        那就只通知绑定者，不回客户）
        """
        self.send(cid, EXPIRED_MSG)

    def expired(self):
        """这个机器人是不是已经到期停用了。

        只看持久化的到期时间，不看 status ——
        status 不写进 bots.json，重启后不准
        """
        try:
            return self.mgr.is_expired(self.bot)
        except Exception:
            return self.bot.get('status') == 'expired'

    @staticmethod
    def help_text():
        return '（暂无说明）'

    # -------- 分发 --------
    def handle(self, u):
        # ★ 先归档（开了「群消息记录」的机器人才写）。
        #   放在最前面 = 群里每条消息都能留下，不管后面怎么分发；
        #   没开的只有一次 dict 取值，没有开销
        self.record_archive(u)

        # 按钮点击
        cq = u.get('callback_query')
        if cq:
            frm = cq.get('from') or {}
            if self.allow_callback(frm.get('id')):
                self.on_callback(cq)
            return

        # 机器人自己被拉进群 / 踢出群：跟普通 message 不是一回事，
        # 结构在 update['my_chat_member'] 里，得单独接
        mcm = u.get('my_chat_member')
        if mcm:
            if self.allow_groups:
                self.on_my_chat_member(mcm)
            return

        msg = u.get('message')
        if not msg:
            return
        frm = msg.get('from') or {}
        if frm.get('is_bot'):
            return
        chat = msg.get('chat') or {}
        ctype = chat.get('type') or 'private'

        # 群消息：只有声明要收的类型（记账机器人）、或者开了「群消息记录」的
        # 才放行，其余照旧丢掉。
        # ★ 这里提前 return，所以下面的 /admin、/id、绑定逻辑对群消息都不生效
        if ctype != 'private':
            if self.wants_groups():
                self.on_group(msg)
            return

        cid = frm.get('id')
        text = (msg.get('text') or '').strip()

        # 已到期：什么都别干，只处理「到期」这件事
        if self.expired():
            self.on_expired(cid, frm, cid in self.admins())
            return

        # 邀请链接进来的人：/start op_XXXXXX
        if text.startswith('/start'):
            parts = text.split(None, 1)
            if len(parts) > 1 and self.accept_invite(cid, frm, parts[1].strip()):
                return

        # /admin 必须最先判断：没绑定时，管理员本人也不算管理员
        if text.startswith('/admin'):
            self.cmd_admin(cid, frm, text)
            return
        if text.startswith('/id'):
            self.send(cid, '你的 Telegram ID：%d' % cid)
            return

        if cid in self.admins():
            self.on_owner(msg)
            return

        # 不是管理员：看看是不是绑定者正在等的那个人（按用户名匹配）
        if self.accept_pending_op(cid, frm):
            return

        self.on_guest(msg)

    def cmd_admin(self, aid, user, text):
        parts = text.split()
        if len(parts) < 2:
            self.send(aid, '用法：/admin 绑定码\n绑定码请向给你开通机器人的人索取。')
            return
        code = parts[1].strip().upper()
        if aid in self.admins():
            self.send(aid, '你已经绑定过了，无需重复绑定。')
            return
        if code != (self.bot.get('bind_code') or '').upper():
            self.send(aid, '❌ 绑定码不对，请核对后重试。')
            log('[%s] 有人用错误绑定码尝试绑定：%s (ID %s)' % (self.note(), code, aid))
            return
        self.add_admin(aid, user)
        self.send_welcome(aid, '✅ 绑定成功！\n\n' + self.welcome_owner())
        log('[%s] 管理员绑定成功：%s (ID %s)' % (self.note(), raw_name(user), aid))

    def welcome_owner(self):
        return '输入 /help 查看指令。'

    def send_welcome(self, chat_id, text):
        """绑定成功的欢迎消息。带菜单的机器人可以覆盖它顺便把菜单发出去"""
        self.send(chat_id, text)

    # ================= 操作人管理 =================
    # 一个机器人可以给多个人用：绑定者 + 他添加的操作人。
    # 只有绑定者能加人 / 踢人。
    def match_button(self, text):
        """子类覆盖它来做底部菜单匹配；基类默认没有菜单"""
        return None

    def ops_view(self):
        """返回 (文本, inline键盘)，给「操作人管理」用"""
        owner = self.owner_id()
        others = sorted(i for i in self.admins() if i != owner)

        lines = ['👥 操作人管理', '', '绑定者：', '  %s' % self.name_of(owner), '']
        if others:
            lines.append('操作人（%d 个）：' % len(others))
            for i in others:
                lines.append('  %s' % self.name_of(i))
        else:
            lines.append('还没有添加操作人。')
        lines.append('')

        pend = self.pending_ops
        if pend:
            lines.append('等待加入（还没跟机器人说过话）：')
            for p in pend:
                lines.append('  @%s' % (p.get('display') or '?'))
            lines.append('')

        lines.append('操作人和绑定者一样，能用这个机器人的全部功能。')

        rows = [[{'text': '➕ 添加操作人', 'callback_data': 'opadd'}]]
        for i in others:
            nm = self.name_of(i)
            if len(nm) > 14:
                nm = nm[:13] + '…'
            rows.append([{'text': '🗑 移除 %s' % nm, 'callback_data': 'opdel:%d' % i}])
        return '\n'.join(lines), {'inline_keyboard': rows}

    def send_ops_view(self, aid, mid=None):
        text, kb = self.ops_view()
        try:
            if mid:
                self.api.call('editMessageText', chat_id=aid, message_id=mid,
                              text=text, disable_web_page_preview=True, reply_markup=kb)
            else:
                self.api.call('sendMessage', chat_id=aid, text=text,
                              disable_web_page_preview=True, reply_markup=kb)
        except TgError as e:
            log('[%s] 发操作人列表失败：%s' % (self.note(), e))

    def start_add_op(self, aid):
        if not self.is_owner(aid):
            self.send(aid, '只有绑定者才能添加操作人。')
            return
        self._await_op[aid] = time.time()
        self.send_menu(aid,
                       '➕ 添加操作人\n\n'
                       '把对方的 Telegram 用户名发给我，例如：\n'
                       '@zhangsan\n\n'
                       '（在 Telegram 里点对方头像就能看到用户名）\n\n'
                       '输入 /取消 可以放弃')

    def handle_op_input(self, aid, text):
        """绑定者正在输入操作人用户名。返回 True 表示这条消息被吃掉了"""
        t = self._await_op.get(aid)
        if not t:
            return False
        if time.time() - t > 300:
            self._await_op.pop(aid, None)
            return False
        # 点按钮 / 打指令 → 不当用户名，取消等待
        if text.startswith('/') or self.match_button(text):
            self._await_op.pop(aid, None)
            return False

        self._await_op.pop(aid, None)
        un = text.lstrip('@').strip()
        if not un or ' ' in un or len(un) < 3:
            self.send_menu(aid, '❌ 这不是一个有效的用户名。\n\n'
                                'Telegram 用户名只能有字母、数字、下划线，'
                                '长度至少 5 位，例如 @zhangsan\n\n'
                                '重新点「👥 操作人」→「➕ 添加操作人」再试一次。')
            return True

        code = self.add_pending_op(un, aid)
        link = self.invite_link(code)
        lines = ['✅ 已记下用户名：@%s' % un, '',
                 '让 TA 成为操作人，两种方式任选一种：', '',
                 '【方式一 · 推荐，最快】',
                 '把下面这条链接发给 TA，TA 点一下就通过了：',
                 link, '',
                 '【方式二】',
                 '让 TA 直接给机器人发任意一条消息，也会自动通过。', '',
                 '（TA 成为操作人后，和你一样能用全部功能）']
        self.send_menu(aid, '\n'.join(lines))
        log('[%s] 记下待加入的操作人 @%s' % (self.note(), un))
        return True

    def op_callback(self, cq, aid):
        """处理操作人管理里的按钮。返回 True 表示已处理"""
        data = cq.get('data') or ''
        cb_id = cq.get('id')
        mid = (cq.get('message') or {}).get('message_id')

        if data == 'opadd':
            if not self.is_owner(aid):
                self._answer_cb(cb_id, '只有绑定者能添加操作人')
                return True
            self._answer_cb(cb_id, '')
            self.start_add_op(aid)
            return True

        if data.startswith('opdel:'):
            if not self.is_owner(aid):
                self._answer_cb(cb_id, '只有绑定者能移除操作人')
                return True
            try:
                target = int(data[6:])
            except ValueError:
                self._answer_cb(cb_id, '参数不对')
                return True
            nm = self.name_of(target)
            if self.remove_admin(target):
                self._answer_cb(cb_id, '已移除 %s' % nm)
                log('[%s] 移除操作人 %s' % (self.note(), target))
                self.send_ops_view(aid, mid)
                self.send(target, '你已被移出操作人名单，将无法继续使用这个机器人。')
            else:
                self._answer_cb(cb_id, '绑定者不能被移除')
            return True

        return False

    def _answer_cb(self, cb_id, text=''):
        try:
            self.api.call('answerCallbackQuery', callback_query_id=cb_id, text=text)
        except TgError as e:
            log('[%s] 回应按钮失败：%s' % (self.note(), e))

    def send_menu(self, chat_id, text):
        """带底部菜单发消息。子类覆盖它挂上自己的菜单"""
        self.send(chat_id, text)
