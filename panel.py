# -*- coding: utf-8 -*-
"""网页管理面板：页面 + JSON 接口

页面放在同目录的 panel_page.html（打包时一起打进 exe）。

──────────────────────────────────────────────
这个文件只放两样东西
──────────────────────────────────────────────
  ① **框架层**的接口 —— 所有机器人类型共用：
     页面、登录、机器人增删启停、商户管理、登录记录、谷歌验证码
  ② **各类型的路由表** —— 就是「哪段路径归哪个类型」那几行 if

各类型接口的**实现**在 `runners/<类型>/api.py`：
    runners/shop/api.py     /api/shop/*    商城
    runners/ledger/api.py   /api/ledger/*  记账
                            /api/archive/* 群消息记录（目前只有记账有）

这样分是为了：改记账的接口，一个字都不会碰到商城的代码。
出问题时想看全部接口 → 就在这个文件里（路由表都在这儿，一个都不藏）。

★ 加新机器人类型的接口：在上面那张路由表加几行 → 实现写到它自己的 api.py
"""
import json
import copy
import os
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
from urllib.error import HTTPError

import core
import login_log
import totp
from core import log, resource_path
from manager import DURATIONS, RUNNERS, type_name
from merchants import make_token, safe_eq

PAGE_FILE = 'panel_page.html'
MESSAGES_FILE = 'messages_page.html'

# 输过一次验证码之后，多久之内不用再输（秒）。
# ★ 看 5 个 IP 要输 5 遍码太折腾；嫌不够严格就调小或设 0。
TOTP_GRACE = 300

# ===== 密码错误锁定 =====
# 注意：走 Tailscale Funnel 进来时，所有外部连接在服务器看来都来自 127.0.0.1，
# 所以「按 IP 限流」对公网没用，真正管用的是「全局计数」。
#
# ★ 有了商户之后必须按「账号」分开计数：以前全局 8 次就锁，
#   商户打错几次密码会把管理员也锁在门外，外人还能拿商户登录框当武器。
FAIL_LIMIT = 5            # 同一个 IP 连续错几次就锁
MERCHANT_FAIL_LIMIT = 10  # 同一个商户账号连续错几次就锁
GLOBAL_FAIL_LIMIT = 30    # ★ 只统计【管理员】的失败（见 _record_fail）
FAIL_WINDOW = 300         # 统计窗口（秒）
LOCK_TIME = 300           # 锁定时长（秒）

TOKEN_HEADER = 'X-Panel-Token'
# 没改初始密码时，只放行改密码这一个接口
MUST_CHANGE_OK = ('/api/me/password',)

# 哪些 action 商户对自己的机器人也能做。
# 表里没有的一律当管理员专属 —— 加新 action 时别忘了来这里登记。
MERCHANT_ACTIONS = {
    'token': True,        # 换自己的机器人 token（用户明确要的功能）
    'newcode': False,     # 换绑定码：先只让管理员做，顺便确认钱到账
    'unbind': False,
    'enable': False,
    'disable': False,
    'delete': False,
    'extend': False,      # 续期是收费的，只能管理员做
}

_FALLBACK_PAGE = (
    '<!DOCTYPE html><html><head><meta charset="utf-8">'
    '<title>面板资源缺失</title></head><body style="font-family:sans-serif;'
    'background:#0f1420;color:#e6edf3;padding:40px">'
    '<h2>找不到面板页面文件</h2>'
    '<p>请确认 <code>panel_page.html</code> 和程序在同一个目录。</p>'
    '</body></html>')


PAGE_STAMP = ['']


def load_page():
    """读面板页面。★ 顺便记下文件的修改时间当「版本号」

    干嘛用的：这个项目前端改得勤，用户看到「没生效」时，
    十有八九是浏览器里还是旧页面。界面上显示一个版本号，
    一眼就能判断到底改没改上去（前端会在标题旁显示它）。
    """
    path = resource_path(PAGE_FILE)
    try:
        with open(path, 'r', encoding='utf-8') as f:
            body = f.read()
        try:
            PAGE_STAMP[0] = datetime.fromtimestamp(
                os.path.getmtime(path)).strftime('%m-%d %H:%M')
        except OSError:
            PAGE_STAMP[0] = ''
        return body
    except Exception as e:
        log('读取面板页面失败（%s）：%s' % (path, e))
        return _FALLBACK_PAGE


def merchant_host():
    """config.json 里的「商户专用域名」（没配就是空串）。

    ★★ 干嘛用的（2026-10-07 加）：让商户用一个**自己的域名**访问，
      地址栏干干净净的 `https://商户域名/`，不用带那个 `/m`。
      服务端认 Host 头，是它决定「这次请求算不算商户入口」。

    ★ 为什么不干脆让商户也用 `/m`：也能用，但用户要的是独立域名 +
      干净地址。前端判断商户模式原来是看路径里有没有 /m，
      干净地址下路径就是 `/` —— 光靠路径会把**商户认成管理员**，
      所以在页面里注一个 `__MERCHANT__` 标记告诉前端（见 _page_flags）。
    """
    try:
        cfg = core.load_json(core.CONFIG_FILE, {}) or {}
    except Exception:
        return ''
    return str(cfg.get('merchant_host') or '').strip().lower().rstrip('.')


def merchant_url():
    """商户该用哪个网址登录（给面板界面显示用）。没配就返回空串"""
    host = merchant_host()
    return ('https://' + host + '/') if host else ''


def _host_of(handler):
    """这次请求的 Host（去掉端口、转小写）。取不到就返回空串"""
    try:
        raw = handler.headers.get('Host') or ''
    except Exception:
        return ''
    raw = raw.strip().lower()
    if raw.startswith('['):                 # IPv6 字面量 [::1]:8080
        return raw.split(']')[0] + ']' if ']' in raw else raw
    return raw.split(':')[0].rstrip('.')


def merchant_manifest(raw):
    """商户域名下用的 manifest：只把「名字」换掉，其它字段原样保留。

    ★ 商户把网址加到手机桌面后，图标底下显示的就是 name/short_name。
      不换的话客户手机上是个「TG面板」—— 那是**你**的工具名，
      客户看了会懵（这是你的后台，不是他的）。

    ★ 改不动就原样返回：manifest 是「加到桌面」才用的，
      为了它把整个静态请求搞失败不值得。
    """
    try:
        data = json.loads(raw.decode('utf-8'))
    except Exception:
        return raw
    if not isinstance(data, dict):
        return raw
    data['name'] = '商户后台'
    data['short_name'] = '商户后台'
    data['description'] = '商户后台（手机加到桌面后可全屏打开）'
    return json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8')


def is_merchant_host(handler):
    """这次请求的域名是不是「商户专用域名」。没配过就永远是 False"""
    want = merchant_host()
    return bool(want) and _host_of(handler) == want


def _page_flags(merchant=False):
    """要往页面里插的那段标记（空串 = 不用插）"""
    bits = []
    url = merchant_url()
    if url:
        # ★ 用 json.dumps 生成字符串字面量 —— 域名是配置里的值，
        #   直接拼进 <script> 里必须转义，不然引号能跳出去
        bits.append('window.__MERCHANT_URL=%s;' % json.dumps(url))
        bits.append('window.__MERCHANT_HOST=%s;' % json.dumps(
            merchant_host()))
    if merchant:
        bits.append('window.__MERCHANT__=true;')
    return ('<script>%s</script>' % ''.join(bits)) if bits else ''


def inject_page_flags(body, merchant=False):
    """把标记插在 <head> 后面 —— **必须早于页面自己的那个 <script>**，
    不然前端读不到（它在顶层就用了 window.__MERCHANT__）。"""
    tag = _page_flags(merchant)
    if not tag:
        return body
    i = body.find('<head>')
    if i < 0:
        return body
    return body[:i + len('<head>')] + tag + body[i + len('<head>'):]


_MESSAGES_CACHE = [None]


def load_messages_page():
    """群消息记录页（独立页面）。改了 html 要重启 exe 才生效（跟主页面一样）"""
    if _MESSAGES_CACHE[0] is None:
        path = resource_path(MESSAGES_FILE)
        try:
            with open(path, 'r', encoding='utf-8') as f:
                _MESSAGES_CACHE[0] = f.read()
        except Exception as e:
            log('读取消息记录页面失败（%s）：%s' % (path, e))
            _MESSAGES_CACHE[0] = ('<!doctype html><meta charset="utf-8">'
                                  '<h3>消息记录页加载失败，看运行日志</h3>')
    return _MESSAGES_CACHE[0]


# ---- 「添加到桌面」的资源（图标 + manifest）----
# 手机把面板加到桌面后，图标和名字就是从这里来的。
# ★★ 这几个路由**必须是公开的**（见 do_GET 里那段）：浏览器来取的时候
#    不会带 X-Panel-Pass 头，挂鉴权的话手机上就是个白图标 + 没名字。
# 路径 -> (static/ 下的文件名, Content-Type)
STATIC_FILES = {
    '/manifest.webmanifest': ('manifest.webmanifest',
                              'application/manifest+json; charset=utf-8'),
    '/icon-192.png':         ('icon-192.png', 'image/png'),
    '/icon-512.png':         ('icon-512.png', 'image/png'),
    '/apple-touch-icon.png': ('apple-touch-icon.png', 'image/png'),
}


def load_static(name):
    """读 static/ 下的文件。返回 (内容, 错误说明)，出错时内容是空的。

    ★ 故意**不缓存到内存**：这几个文件很小，而且换了图标要**立刻生效** ——
      缓存起来的话得重启面板才看得到新图标，排查时很莫名其妙。
    """
    path = resource_path(os.path.join('static', name))
    try:
        with open(path, 'rb') as f:
            return f.read(), ''
    except Exception as e:
        log('读取 %s 失败（%s）：%s' % (name, path, e))
        return b'', '找不到 %s' % name


# 访问记录去重：同一个「IP + 身份」多久之内只记一条
ACCESS_WINDOW = 6 * 3600


class PanelHandler(BaseHTTPRequestHandler):
    mgr = None
    password = ''
    page_cache = None
    page_merchant_cache = None   # 商户专用域名那份（多了个 __MERCHANT__ 标记）
    merch = None            # MerchantStore（主程序注入）
    token_secret = ''       # 给登录 token 签名用
    # ★ 谷歌验证码（TOTP）：看真实 IP / 清空记录时要输。
    #   密钥存在 config.json 的 totp_secret 里（跟面板密码一个级别）。
    #   totp_ok_until = 刚验证过的话，多久之内不用再输（见 _totp_ok）
    totp_ok_until = 0.0

    # 锁定状态（类变量，所有请求共享）
    # ★ key 带前缀：'admin|1.2.3.4' / 'm|zhangsan' —— 各算各的，互不牵连
    _fail_lock = threading.Lock()
    _fails = {}
    _access_lock = threading.Lock()
    _access_seen = {}      # 'ip|身份' → 上次记访问的时间
    _lock_until = {}       # key -> 解锁时间
    _global_fails = []     # ★ 只统计管理员的失败
    _global_lock = 0.0     # 全局解锁时间（只锁管理员入口）

    def log_message(self, *a):
        pass

    # -------- 限流 --------
    def _locked(self, ip, now):
        """返回 (是否锁定, 还要等几秒)"""
        if now < PanelHandler._global_lock:
            return True, int(PanelHandler._global_lock - now)
        until = PanelHandler._lock_until.get(ip, 0)
        if now < until:
            return True, int(until - now)
        return False, 0

    def _record_fail(self, key, now, limit=None):
        """记一次密码错误。key 形如 'admin|1.2.3.4' 或 'm|zhangsan'

        ★ 只有管理员的失败才进全局计数 —— 否则商户打错密码会把管理员
          一起锁在门外，外人还能拿商户登录框当武器专门锁你。
        """
        lim = limit or FAIL_LIMIT
        with PanelHandler._fail_lock:
            lst = [t for t in PanelHandler._fails.get(key, [])
                   if now - t < FAIL_WINDOW]
            lst.append(now)
            if len(lst) >= lim:
                PanelHandler._lock_until[key] = now + LOCK_TIME
                log('⚠️ %s 密码连续错 %d 次，锁定 %d 分钟'
                    % (key, lim, LOCK_TIME // 60))
                lst = []
            PanelHandler._fails[key] = lst

            if key.startswith('admin|'):
                g = [t for t in PanelHandler._global_fails
                     if now - t < FAIL_WINDOW]
                g.append(now)
                if len(g) >= GLOBAL_FAIL_LIMIT:
                    PanelHandler._global_lock = now + LOCK_TIME
                    log('⚠️⚠️ 管理密码错误次数过多，管理入口锁定 %d 分钟'
                        '（如果有人在外面爆破，这条日志会不停出现）'
                        % (LOCK_TIME // 60))
                    g = []
                PanelHandler._global_fails = g

    def _clear_fail(self, ip):
        with PanelHandler._fail_lock:
            PanelHandler._fails.pop(ip, None)

    # -------- 工具 --------
    def _send(self, code, body, ctype='application/json; charset=utf-8',
              cache='no-store'):
        """cache 默认 no-store —— 接口响应一律别让浏览器存（含 token / 密码）

        ★ 只有归档的图片例外，见下面 media 那条：图片文件名里带着
          file_id 的哈希，同名 = 同内容，改不了，可以长缓存。
        """
        if isinstance(body, str):
            body = body.encode('utf-8')
        # 这次请求带了 body、但我们**没读**（比如鉴权没过就提前 return 了）
        # —— 这条连接上还留着没读走的字节。
        #
        # ★ 说明白：**现在用不上**。`protocol_version` 没设 = HTTP/1.0，
        #   而 http.server 只在 HTTP/1.1 下才认 `Connection: keep-alive`，
        #   所以每个请求都是**一条连接、用完就关**，不存在"上一个请求的
        #   body 被当成下一个请求解析"的问题。
        # ★ 留着是**护栏**：哪天为了性能把 protocol_version 改成 HTTP/1.1，
        #   没有这几行就会踩到那个坑 —— 客户端报
        #   「Connection aborted / 主机中的软件中止了一个已建立的连接」，
        #   而且只在"带了 body 又提前返回"的请求之后才出现，极难查。
        if not getattr(self, '_body_read', False):
            try:
                if int(self.headers.get('Content-Length') or 0) > 0:
                    self.close_connection = True
            except (TypeError, ValueError):
                pass
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', cache)
        # ★ 别让浏览器「猜」类型。少了这个头，被当成 text/html 猜中的响应里
        #   哪怕只是回显一段用户内容，也能直接跑脚本 ——
        #   便宜的一层保险，一行不亏。
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _deny(self):
        self._json({'ok': False,
                    'error': getattr(self, '_lock_msg', '密码错误')}, 401)

    def _auth(self):
        """解析出「身份」。返回 dict；没通过返回 None（调用方负责回包）

            {'role': 'admin',  'name': '管理员'}
            {'role': 'merchant', 'mid': …, 'user': …, 'name': …, 'must_change': …}
        """
        # ① 商户：带签名的 token
        tok = self.headers.get(TOKEN_HEADER, '')
        if tok:
            m = (self.merch.identify(tok, self.token_secret)
                 if (self.merch and self.token_secret) else None)
            if m:
                return {'role': 'merchant', 'mid': m['mid'],
                        'user': m.get('user') or '',
                        'name': m.get('name') or m.get('user') or m['mid'],
                        'must_change': bool(m.get('must_change'))}
            self._lock_msg = '登录已过期，请重新登录'
            return None

        # ② 管理员：老的密码头（保持兼容，现有脚本/习惯一行不用改）
        if not self.password:
            return {'role': 'admin', 'name': '管理员'}
        ip = (self.client_address or ('?',))[0]
        key = 'admin|%s' % ip
        now = time.time()
        locked, wait = self._locked(key, now)
        if locked:
            self._lock_msg = '密码错误次数过多，已暂时锁定，请 %d 秒后再试' % wait
            return None
        if safe_eq(self.headers.get('X-Panel-Pass', ''), self.password):
            self._clear_fail(key)
            return {'role': 'admin', 'name': '管理员'}
        self._record_fail(key, now)
        locked, wait = self._locked(key, time.time())
        self._lock_msg = ('密码错误次数过多，已暂时锁定，请 %d 秒后再试' % wait
                          if locked else '密码错误')
        return None

    def _gate(self):
        """所有 /api 的统一门口。拿到身份，或者已经回过包了（None）"""
        me = self._auth()
        if me is None:
            self._deny()
            return None
        # 初始密码没改时，除了改密码什么都不让做。
        # ★ 服务端强制，商户拿 curl 也绕不过去
        if (me.get('must_change')
                and urlparse(self.path).path not in MUST_CHANGE_OK):
            self._json({'ok': False, 'code': 'must_change',
                        'error': '请先修改初始密码'}, 403)
            return None
        # ★ 带凭据访问就留痕迹（自动登录不走 /api/login，不记就看不见）
        self._log_access(me)
        return me

    def _admin_only(self, me):
        if me.get('role') != 'admin':
            self._json({'ok': False, 'error': '只有管理员能做这个操作'}, 403)
            return False
        return True

    # -------- 谷歌验证码（看真实 IP / 清空记录时要输）--------
    @staticmethod
    def _cfg_path():
        return core.CONFIG_FILE

    def _totp_secret(self):
        """当前密钥。没有就现生成一个并存下来（第一次打开页面时）

        ★ 存 config.json，跟面板密码一个级别 —— 拿到密钥就能一直算出正确的码。
        """
        try:
            cfg = core.load_json(self._cfg_path(), {}) or {}
        except Exception:
            cfg = {}
        sec = str(cfg.get('totp_secret') or '').strip()
        if sec:
            return sec
        sec = totp.new_secret()
        self._save_totp_secret(sec)
        log('生成了新的谷歌验证密钥（去「登录记录」里绑定）')
        return sec

    def _save_totp_secret(self, sec):
        try:
            cfg = core.load_json(self._cfg_path(), {}) or {}
            cfg['totp_secret'] = sec
            core.save_json(self._cfg_path(), cfg)
            return True
        except Exception as e:
            log('保存验证密钥失败：%s' % e)
            return False

    def _totp_bound(self):
        """用户确认绑定过没有。

        ★ 「绑定」是个**用户说的事**，服务端没法知道 ——
          所以让用户绑好后点一下「我已绑定」，我们记个标记。
          没绑之前不拦人（不然谁都进不去了）。
        """
        try:
            cfg = core.load_json(self._cfg_path(), {}) or {}
        except Exception:
            return False
        return bool(cfg.get('totp_bound'))

    def _totp_ok(self):
        """刚才验证过没有（5 分钟内不用重复输）

        ★ 一次看 5 个 IP 要输 5 遍码，太折腾；所以验过之后记 5 分钟。
          嫌不够严格就把 TOTP_GRACE 调小或设 0。
        """
        return time.time() < PanelHandler.totp_ok_until

    def _need_totp(self, body):
        """要动真实 IP 时统一走这儿：验过就直接放行，没验过就校验 body 里的码

        返回 True = 可以继续；False = 已经回过包了
        """
        if not self._totp_bound():
            # 还没绑定 → 不拦。绑了之后才真正起作用（不然用户把自己锁在外面）
            return True
        if self._totp_ok():
            return True
        code_in = str((body or {}).get('code') or '')
        if totp.verify(self._totp_secret(), code_in):
            PanelHandler.totp_ok_until = time.time() + TOTP_GRACE
            return True
        self._json({'ok': False, 'error': '验证码不对，或者过期了（30 秒一换）',
                    'need_code': True}, 403)
        return False

    def _bot_of(self, me, bid):
        """取机器人 + 校验归属。不通过时自己回过包，返回 None

        ★ 不是自己的机器人，回的话跟「找不到」一模一样 ——
          别让商户靠试探摸清别人有哪些机器人。
        """
        b = self.mgr.find(bid)
        if b and (me.get('role') == 'admin'
                  or (b.get('mid') or '') == me.get('mid')):
            return b
        self._json({'ok': False, 'error': '找不到这个机器人'}, 404)
        return None

    def _archive_of(self, bid):
        """打开某个机器人的「群消息记录」库（只读用）

        ★ 归属校验在调用方（必须先走 _bot_of）—— 这里只管打开文件
        ★ 没开过记录 / 一条消息都没收到 → 文件不存在 → 返回 None，
          面板显示「还没有记录」，不是报错
        """
        p = os.path.join(core.bot_data_dir(self.mgr.find(bid)), '%s.archive.sqlite3' % bid)
        if not os.path.exists(p):
            return None
        try:
            from archive import MessageArchive
            return MessageArchive(p)
        except Exception as e:
            log('打开消息记录失败 %s：%s' % (bid, e))
            return None

    def _shop_of(self, me, bid):
        """取商城数据层（顺便做归属校验 + **类型**校验）"""
        b = self._bot_of(me, bid)
        if b is None:
            return None
        # ★ 必须校验类型：不然拿记账/客服的 id 打商城接口也能过，
        #   商户就能往非商城机器人身上写 shop 配置
        if (b.get('type') or '') != 'shop':
            self._json({'ok': False, 'error': '这不是商城机器人'}, 404)
            return None
        from runners.shop import store as SS
        st = SS.store_for(self.mgr, bid)
        if st is None:
            self._json({'ok': False, 'error': '找不到这个机器人'}, 404)
        return st

    def _shop_instance(self, path, me, method):
        parts = path.strip('/').split('/')
        if len(parts)<4:
            return False
        bot = self._bot_of(me, parts[2])
        if bot is None:
            return True
        if bot.get('type') != 'shop':
            self._json({'ok':False,'error':'这不是商城机器人'},404)
            return True
        if os.environ.get('PANEL_INSTANCE_CHILD') == '1':
            return False
        if not bot.get('instance_folder'):
            return False
        runner = self.mgr.runners.get(bot['id'])
        if not runner or not runner.is_alive():
            return False
        body = self._body() if method=='POST' else {}
        try:
            try:
                response = runner.request('/instance/panel', dict(path=self.path,method=method,
                    principal={k:me.get(k) for k in ('role','mid','user','must_change')},body=body))
            except HTTPError as error:
                response = error
            with response:
                self._json(json.load(response), response.status)
        except (OSError,ValueError):
            self._json({'ok':False,'error':'机器人实例暂不可用'},503)
        return True

    def _body(self):
        # ★ 标记「这次请求的 body 已经读掉了」——
        #   _send() 靠它判断要不要把连接关掉（见那里的注释）
        self._body_read = True
        try:
            n = int(self.headers.get('Content-Length') or 0)
            if n <= 0:
                return {}
            return json.loads(self.rfile.read(n).decode('utf-8'))
        except Exception:
            return {}

    def _raw_body(self, limit):
        """读**原始字节**的请求体（客户回传的图片用）。

        ★ 不能用 _body()：那个是解 JSON 的，图片是二进制，解 JSON 必炸。
        ★ 超过 limit 就**不读**，让 _send() 把连接关掉（读进来白占内存，
          一张图能到 10MB）。
        """
        try:
            n = int(self.headers.get('Content-Length') or 0)
        except (TypeError, ValueError):
            n = 0
        if n <= 0 or n > limit:
            return b''                 # 没读 → _body_read 保持 False → 关连接
        self._body_read = True
        try:
            return self.rfile.read(n)
        except Exception:
            return b''

    # ================= 登录 / 密码 =================
    def _client_ip(self):
        """取客户端 IP

        ★ 面板挂在 Tailscale funnel 后面，这时候 client_address 是内网地址
          （100.x 那种），真实 IP 在转发头里。有就用它。
        ⚠️ 这些头客户端可以伪造 —— 面板本来就不是高安全场景，
           日志用来「看个大概」，别当成铁证。
        """
        for h in ('X-Forwarded-For', 'X-Real-IP', 'CF-Connecting-IP'):
            v = (self.headers.get(h) or '').strip()
            if v:
                first = v.split(',')[0].strip()
                if first:
                    return first
        return (self.client_address or ('?',))[0]

    def _log_login(self, user, role, ok):
        """记一条登录日志。★ 绝不因为它失败而影响登录本身"""
        try:
            login_log.add(self._client_ip(), user, role, ok,
                          self.headers.get('User-Agent') or '',
                          kind='login')
        except Exception as e:
            log('写登录日志失败：%s' % e)

    def _log_access(self, me):
        """记一条「有人用凭据进来了」。

        ★★ 为什么需要它：浏览器会把密码存在 localStorage 里，
          下次打开**自动登录**，根本不调 /api/login ——
           只记登录按钮的话，别人拿着你的面板地址来看，日志里一片空白。
           现在只要带着凭据访问接口，就留一条痕迹。

        同一个「IP + 身份」6 小时内只记一次，免得刷屏。
        """
        key = '%s|%s|%s' % (self._client_ip(), me.get('role') or '',
                            me.get('user') or '')
        now = time.time()
        with PanelHandler._access_lock:
            last = PanelHandler._access_seen.get(key)
            if last and now - last < ACCESS_WINDOW:
                return
            PanelHandler._access_seen[key] = now
            if len(PanelHandler._access_seen) > 500:      # 顺手清过期
                for k in [x for x, v in PanelHandler._access_seen.items()
                          if now - v > ACCESS_WINDOW]:
                    PanelHandler._access_seen.pop(k, None)
        try:
            who = ('（管理员）' if me.get('role') == 'admin'
                   else (me.get('user') or me.get('name') or ''))
            login_log.add(self._client_ip(), who, me.get('role') or '', True,
                          self.headers.get('User-Agent') or '',
                          kind='access')
        except Exception as e:
            log('写访问记录失败：%s' % e)

    def _do_login(self):
        """登录。唯一免鉴权的 POST —— 它就是来换凭据的"""
        b = self._body()
        user = str(b.get('user') or '').strip()
        pw = str(b.get('pass') or '')
        ip = self._client_ip()

        if not user:
            # 管理员：用户名留空，走老的密码头那套（行为跟以前完全一样）
            key = 'admin|%s' % ip
            now = time.time()
            locked, wait = self._locked(key, now)
            if locked:
                self._log_login('（管理员）', 'admin', False)
                self._json({'ok': False, 'error':
                            '密码错误次数过多，已暂时锁定，请 %d 秒后再试' % wait},
                           401)
                return
            if self.password and not safe_eq(pw, self.password):
                self._record_fail(key, now)
                self._log_login('（管理员）', 'admin', False)
                self._json({'ok': False, 'error': '密码不对'}, 401)
                return
            self._clear_fail(key)
            self._log_login('（管理员）', 'admin', True)
            self._json({'ok': True, 'role': 'admin', 'name': '管理员',
                        'must_change': False, 'token': ''})
            return

        # 商户：按账号限流，绝不影响管理员
        key = 'm|%s' % user.lower()
        now = time.time()
        locked, wait = self._locked(key, now)
        if locked:
            self._log_login(user, 'merchant', False)
            self._json({'ok': False, 'error':
                        '密码错误次数过多，请 %d 秒后再试' % wait}, 401)
            return
        m = self.merch.verify(user, pw) if self.merch else None
        if not m:
            self._record_fail(key, now, MERCHANT_FAIL_LIMIT)
            self._log_login(user, 'merchant', False)
            # 不说「用户名不存在」还是「密码错」，别帮人枚举账号
            self._json({'ok': False, 'error': '账号或密码不对'}, 401)
            return
        self._clear_fail(key)
        self._log_login(user, 'merchant', True)
        self._json({'ok': True, 'role': 'merchant', 'mid': m['mid'],
                    'user': m.get('user'), 'name': m.get('name') or m.get('user'),
                    'must_change': bool(m.get('must_change')),
                    'token': make_token(m, self.token_secret)})

    def _do_chpass(self, me):
        """改自己的密码（商户用；管理员密码在 config.json，不在这改）"""
        if me.get('role') != 'merchant':
            self._json({'ok': False, 'error': '管理员的密码在 config.json 里改'})
            return
        b = self._body()
        old = str(b.get('old') or '')
        new = str(b.get('new') or '')
        m = self.merch.get(me.get('mid')) if self.merch else None
        if not m:
            self._json({'ok': False, 'error': '找不到这个商户'})
            return
        if not self.merch.verify(m.get('user'), old):
            self._json({'ok': False, 'error': '原密码不对'})
            return
        if safe_eq(old, new):
            self._json({'ok': False, 'error': '新密码跟原来一样'})
            return
        ok, why = self.merch.set_pass(m['mid'], new, must_change=False)
        if not ok:
            self._json({'ok': False, 'error': why})
            return
        # 改完 sess_ver 变了，旧 token 作废，得发一个新的给他
        m2 = self.merch.get(m['mid'])
        log('商户 %s 改了密码' % m.get('user'))
        self._json({'ok': True, 'token': make_token(m2, self.token_secret)})

    # ================= 商户管理（管理员专属）=================
    def _merch_list(self):
        counts = {}
        for b in self.mgr.bots:
            mid = b.get('mid') or ''
            if mid:
                counts[mid] = counts.get(mid, 0) + 1
        return self.merch.snapshot(counts) if self.merch else []

    def _do_merchants(self, path):
        parts = path.strip('/').split('/')       # api / merchants / <mid> / <op>
        b = self._body()
        if not (self.merch and self.mgr):
            self._json({'ok': False, 'error': '商户功能没启用'})
            return

        # 新建：POST /api/merchants
        if len(parts) == 2:
            try:
                m, pw = self.merch.add(b.get('name'), b.get('user'),
                                       b.get('note'))
            except ValueError as e:
                self._json({'ok': False, 'error': str(e)})
                return
            # ★ 初始密码只在这里回一次，不落盘（只存哈希）
            self._json({'ok': True, 'merchant': self.merch.public(m),
                        'pass': pw})
            return

        if len(parts) != 4:
            self._json({'ok': False, 'error': '不认识的商户操作'})
            return

        mid, op = parts[2], parts[3]
        m = self.merch.get(mid)
        if not m:
            self._json({'ok': False, 'error': '找不到这个商户'})
            return

        if op == 'reset':
            pw, err = self.merch.reset_pass(mid)
            if err:
                self._json({'ok': False, 'error': err})
                return
            self._json({'ok': True, 'pass': pw, 'user': m.get('user')})
            return

        if op == 'enable' or op == 'disable':
            self.merch.set_enabled(mid, op == 'enable')
            self._json({'ok': True})
            return

        if op == 'delete':
            # ★ 还有机器人挂在他名下就不让删，否则机器人成了没人管的状态
            n = len(self.mgr.of_merchant(mid))
            if n:
                self._json({'ok': False,
                            'error': '他名下还有 %d 个机器人，先收回再删' % n})
                return
            self.merch.remove(mid)
            self._json({'ok': True})
            return

        if op == 'bots':
            # 一次性完成「勾上的划给他 + 原来属于他但没勾的收回」
            bids = b.get('bids') or []
            clear = b.get('clear_secrets', True)
            try:
                moved, taken, cleared = self.mgr.assign_bots(mid, bids, clear)
            except OSError:
                self._json({'ok':False,'error':'部分服务器未确认分配结果，请刷新后核对；未迁移机器人'},503)
                return
            # ★ 收款地址被清掉了，扫描基线必须重建，
            #   否则新地址历史上的转账会被当成新收款，白送能量出去
            self.mgr.reset_shop_scans(cleared)
            self._json({'ok': True, 'moved': moved, 'taken': taken,
                        'cleared': len(cleared)})
            return

        self._json({'ok': False, 'error': '不认识的商户操作'})

    # -------- GET --------
    def do_GET(self):
        path = urlparse(self.path).path
        if path.startswith('/api/nodes'):
            me = self._gate()
            if me is not None:
                import nodes
                nodes.handle(self,path,'GET',me)
            return
        if any(b.get('node_id') for b in getattr(self.mgr,'bots',[])):
            import nodes
            if nodes.proxy(self,path,'GET',None):
                return
        if path.startswith('/miniapp/'):
            import miniapp
            if miniapp.handle_get(self, path):
                return
        # 管理后台和商户后台是同一个页面文件，角色由登录结果决定
        if path in ('/', '/index.html', '/m', '/m/'):
            # ★ 这次请求算不算「商户入口」？
            #   ① 网址带 /m                —— 老办法
            #   ② Host 配成了商户专用域名   —— 2026-10-07 加（干净地址）
            #   两种都只是**告诉前端用商户界面**，不是鉴权 ——
            #   真正的门是登录接口，商户没有管理员权限。
            as_merchant = path.startswith('/m') or is_merchant_host(self)
            if as_merchant:
                if self.page_merchant_cache is None:
                    PanelHandler.page_merchant_cache = inject_page_flags(
                        load_page(), merchant=True)
                self._send(200, self.page_merchant_cache,
                           'text/html; charset=utf-8')
                return
            if self.page_cache is None:
                PanelHandler.page_cache = inject_page_flags(load_page())
            self._send(200, self.page_cache, 'text/html; charset=utf-8')
            return
        # 群消息记录：独立页面（★ 数据接口本身是管理端专属，见下面 /api/archive/）
        # HTML 本身不拦 —— 浏览器导航发不了自定义头，密码是页面里的 JS 带上的，
        # 所以这里只发文件，真正的门在 API 上。
        if path in ('/messages', '/messages/'):
            self._send(200, load_messages_page(),
                       'text/html; charset=utf-8')
            return

        # ---- 「添加到桌面」用的图标和 manifest ----
        # ★★ 必须是**公开的**：浏览器取 manifest / 图标时不会带
        #    X-Panel-Pass 头（那是页面里的 JS 自己加的），挂鉴权的话
        #    手机加到桌面就是个白图标 + 没有名字。跟上面两个 HTML 同理。
        if path in STATIC_FILES:
            fn, ctype = STATIC_FILES[path]
            body, err = load_static(fn)
            if err:
                self._send(404, err, 'text/plain; charset=utf-8')
                return
            # ★ 商户专用域名下，「加到桌面」的名字要换成商户自己的 ——
            #   不然客户手机上装出来叫「TG面板」（那是**你**的工具名）。
            if path == '/manifest.webmanifest' and is_merchant_host(self):
                body = merchant_manifest(body)
            # 图标基本不变，可以让浏览器存一天（省流量，手机上也快）
            self._send(200, body, ctype, cache='public, max-age=86400')
            return
        if path == '/api/appearance':
            me = self._gate()
            if me is None or not self._admin_only(me):
                return
            import miniapp
            self._json({'ok': True, 'footer_text': self.mgr.cfg.get('miniapp_footer_text', miniapp.DEFAULT_FOOTER)})
            return

        if path == '/api/server':
            me = self._gate()
            if me is None or not self._admin_only(me):
                return
            nid = (parse_qs(urlparse(self.path).query).get('node') or [''])[0]
            if nid:
                import nodes
                registry = nodes.for_manager(self.mgr)
                try:
                    registry.get(nid)
                    state = registry.cache.get(nid,{})
                    value = copy.deepcopy(state.get('server') or {'ready':False,'reason':'运行服务器未连接'})
                    value['stale'] = bool(not state.get('online') or time.time()-state.get('updated_at',0)>45 or value.get('stale'))
                except OSError:
                    value = {'ready':False,'reason':'运行服务器未配置'}
            else:
                monitor = getattr(self.mgr, 'server_monitor', None)
                value = monitor.snapshot() if monitor else {'ready':False,'reason':'监控尚未启动，请检查主服务'}
            self._json({'ok': True, 'server':value})
            return

        if path == '/api/bots':
            me = self._gate()
            if me is None:
                return
            admin = me.get('role') == 'admin'
            self._json({
                'ok': True,
                'me': {'role': me.get('role'),
                       'mid': me.get('mid') or '',
                       'user': me.get('user') or '',
                       'name': me.get('name') or '管理员',
                       'must_change': bool(me.get('must_change'))},
                # ★ 商户只看得到自己名下的（隔离就在这里）
                'bots': self.mgr.snapshot(None if admin else me.get('mid')),
                'types': [{'value': k, 'label': v[1]} for k, v in RUNNERS.items()],
                'durations': [{'value': k, 'label': lb, 'days': d}
                              for k, lb, d in DURATIONS],
                'grace_days': self.mgr.grace_days(),
                # 页面文件的修改时间 —— 前端显示出来，方便判断是不是旧页面
                'page_v': PAGE_STAMP[0],
            })
            return

        # ---- 登录记录（只有管理员能看 —— 里面是 IP）----
        if path == '/api/logins':
            me = self._gate()
            if me is None:
                return
            if not self._admin_only(me):
                return
            rows = login_log.recent(200)
            # ★★ 默认只给打码的 IP（后两段藏起来）：1.2.3.4 → 1.2.*.*
            #   想看真实 IP 得去 /api/logins/reveal 输谷歌验证码。
            #   ★ 这是**默认就藏**，不是「藏起来还能从别的地方拿到」——
            #     所以这里把 ip 字段直接替换掉，别另外放个 ip_real 字段。
            for r in rows:
                r['ip_masked'] = totp.mask_ip(r.get('ip'))
                r.pop('ip', None)
            self._json({'ok': True, 'logins': rows,
                        'stats': login_log.stats(),
                        'bound': self._totp_bound(),
                        'unlocked': self._totp_ok()})
            return

        # ---- 谷歌验证码：拿密钥（给用户去 App 里绑定）----
        # ★ 是 GET（纯读）；看真实 IP / 清空记录那两个是 POST，在下面 do_POST 里
        if path == '/api/totp':
            me = self._gate()
            if me is None:
                return
            if not self._admin_only(me):
                return
            sec = self._totp_secret()
            self._json({'ok': True, 'secret': sec,
                        'url': totp.otpauth_url(sec),
                        'bound': self._totp_bound()})
            return

        # ---- 商户列表（只有管理员能看）----
        if path == '/api/merchants':
            me = self._gate()
            if me is None or not self._admin_only(me):
                return
            self._json({'ok': True, 'merchants': self._merch_list()})
            return

        # ---- 单个机器人的 token（给「换 Token」弹窗预填）----
        if (path.startswith('/api/bots/') and path.endswith('/token')
                and len(path.strip('/').split('/')) == 4):
            me = self._gate()
            if me is None:
                return
            bid = path[len('/api/bots/'):-len('/token')].strip('/')
            b = self._bot_of(me, bid)
            if b is None:
                return
            self._json({'ok': True, 'token': b.get('token') or '',
                        'username': b.get('username') or ''})
            return

        # ---- 客户自建机器人：客户那边 config.json 要填的东西 ----
        # ★ 里面**没有密钥** —— 回传的暗号是两边从 token 现算的
        #   （core.ingest_sig），用户不用管、也不用抄。
        #   这里只给「机器人 id + 回传地址」，都是不敏感的。
        if (path.startswith('/api/bots/') and path.endswith('/clientcfg')
                and len(path.strip('/').split('/')) == 4):
            me = self._gate()
            if me is None:
                return
            if not self._admin_only(me):
                return
            bid = path[len('/api/bots/'):-len('/clientcfg')].strip('/')
            b = self._bot_of(me, bid)
            if b is None:
                return
            self._json({'ok': True, 'id': bid,
                        'remote': bool(b.get('remote')),
                        'note': b.get('note') or ''})
            return

        # ---- ★★ 一键下载「客户安装包」----
        # 全自动的那一步：把客户要装的东西**现打成一个 zip**，
        # 里面的 config.json 已经填好了（编号 + token + 你的地址）——
        # 用户下载下来直接发过去，**不用粘任何东西**，客户解压就跑。
        # ★ 文件清单跟 `_build_solo.py` 用的是同一份（solo_pack.py），
        #   别另写一份，不然迟早对不上。
        if (path.startswith('/api/bots/') and path.endswith('/solozip')
                and len(path.strip('/').split('/')) == 4):
            me = self._gate()
            if me is None:
                return
            if not self._admin_only(me):
                return
            bid = path[len('/api/bots/'):-len('/solozip')].strip('/')
            b = self._bot_of(me, bid)
            if b is None:
                return
            q = parse_qs(urlparse(self.path).query)
            url = (q.get('url') or [''])[0].strip()
            # ★ 只认 http/https：这个值会写进客户那边的配置，别让人塞别的东西
            if not url.startswith(('http://', 'https://')):
                self._json({'ok': False, 'error': '回传地址不对'}, 400)
                return
            try:
                import io as _io
                import solo_pack
                miss = solo_pack.missing_sources(b)
                if miss:
                    raise RuntimeError('缺少源文件：%s' % ', '.join(miss))
                buf = _io.BytesIO()
                solo_pack.make_zip(buf, solo_pack.client_config(
                    bid, b.get('token'), url, b.get('note'),
                    b.get('bind_code'), bot=b, settings=self.mgr.cfg), bot=b)
                data = buf.getvalue()
            except Exception as e:
                log('生成客户安装包失败 %s：%s' % (bid, e))
                self._json({'ok': False,
                            'error': '生成失败：%s（可以用 _build_solo.py 手动打）'
                                     % e}, 500)
                return
            log('生成了客户安装包：%s（%d 字节）' % (bid, len(data)))
            self._send(200, data, 'application/zip')
            return

        # ---- 各机器人类型的接口 ----
        # ★ 判断「这段路径归哪个类型」留在这里（一个文件就能看全，出问题好查），
        #   每条的**实现**都在 runners/<类型>/api.py ——
        #   所以改记账的接口，一个字都不会碰到商城的代码。
        #   见 runners/__init__.py 里的结构说明。
        if path.startswith('/api/shop/'):
            me = self._gate()
            if me is None or self._shop_instance(path,me,'GET'):
                return
            from runners.shop import api as shop_api
            if shop_api.handle_get(self, path):
                return
        if path.startswith('/api/archive/') or path.startswith('/api/ledger/'):
            from runners.ledger import api as ledger_api
            if ledger_api.handle_get(self, path):
                return


        self._send(404, 'Not Found', 'text/plain; charset=utf-8')

    # -------- POST --------
    def do_POST(self):
        path = urlparse(self.path).path
        if path.startswith('/api/miniapp/'):
            import miniapp
            if miniapp.handle_post(self, path):
                return

        # 登录是唯一免鉴权的 POST
        if path == '/api/login':
            return self._do_login()

        # ---- 客户自建机器人的消息回传（★ 第二个免鉴权的口子）----
        # 客户那边没有面板密码，也**不该有**（有了他就能看所有商户）。
        # 这里改用「一家客户一把」的回传密钥，校验在 runners/ledger/api.py。
        if path.startswith('/api/ingest'):
            from runners.ledger import api as ledger_api
            if ledger_api.handle_ingest(self, path):
                return

        me = self._gate()
        if me is None:
            return
        if path.startswith('/api/nodes'):
            import nodes
            nodes.handle(self,path,'POST',me)
            return
        if any(b.get('node_id') for b in getattr(self.mgr,'bots',[])):
            import nodes
            if nodes.proxy(self,path,'POST',me):
                return

        if path == '/api/appearance':
            if not self._admin_only(me):
                return
            value = self._body().get('footer_text')
            if not isinstance(value, str) or len(value.encode('utf-16-le', errors='surrogatepass')) > 128 or '\n' in value or '\r' in value:
                self._json({'ok': False, 'error': '请填写64字以内的单行文字'}, 400)
                return
            value = value.strip()
            with self.mgr.lock:
                updated = dict(self.mgr.cfg, miniapp_footer_text=value)
                core.save_json(core.CONFIG_FILE, updated)
                self.mgr.cfg['miniapp_footer_text'] = value
            self._json({'ok': True, 'footer_text': value})
            return

        # 改自己的密码
        if path == '/api/me/password':
            return self._do_chpass(me)

        # 商户管理：管理员专属
        if path.startswith('/api/merchants'):
            if not self._admin_only(me):
                return
            return self._do_merchants(path)

        if path == '/api/bots':
            # ★ 加机器人只能管理员做（商户想加就找你分配），
            #   否则他能往面板里塞机器人，还会挂到管理员名下
            if not self._admin_only(me):
                return
            b = self._body()
            # remote=True = 客户自建（跑在客户自己的服务器上，面板不启动它）
            bot, err = self.mgr.add(b.get('note', ''), b.get('token', ''),
                                    b.get('type') or 'kefu',
                                    b.get('duration') or 'forever',
                                    remote=bool(b.get('remote')),node_id=b.get('node_id'))
            if err:
                self._json({'ok': False, 'error': err})
            else:
                self._json({'ok': True, 'bot': {
                    'id': bot['id'], 'note': bot['note'],
                    'type': bot.get('type'),
                    'type_name': type_name(bot.get('type')),
                    'username': bot['username'], 'bind_code': bot['bind_code'],
                    'duration': bot.get('duration'),
                    'remote': bool(bot.get('remote')),
                    'node_id':bot.get('node_id',''),'pending':bool(bot.get('node_pending'))}})
            return


        # ---- 各机器人类型的接口（实现见 runners/<类型>/api.py）----
        #   路径前缀互不重叠，所以放在这里和原来分散在各处是等价的
        if path.startswith('/api/shop/'):
            if self._shop_instance(path,me,'POST'):
                return
            from runners.shop import api as shop_api
            if shop_api.handle_post(self, path, me):
                return
        if path.startswith('/api/archive/') or path.startswith('/api/ledger/'):
            from runners.ledger import api as ledger_api
            if ledger_api.handle_post(self, path, me):
                return

        # ---- 清空登录记录 /api/logins/clear ----
        # ★ 这是 POST（写操作）。之前误挂到 do_GET 里了，
        #   前端发 POST 打到 GET 路由上 → 404，而且那段还用了没定义的 me 会崩
        # ★ 清空要**毁掉审计痕迹**，所以跟看真实 IP 一样要验证码。
        if path == '/api/logins/clear':
            if not self._admin_only(me):
                return
            if not self._need_totp(self._body() or {}):
                return
            n = login_log.clear()
            log('登录记录已清空（%d 条）' % n)
            self._json({'ok': True, 'cleared': n})
            return

        # ---- 看真实 IP /api/logins/reveal（要验证码）----
        # ★ 单独开一个接口，不复用 /api/logins —— 免得验证码的逻辑跟
        #   普通读混在一起，哪天改错就把真实 IP 漏出去了
        # ★ 是 POST：验证码放在请求体里，别塞进 URL（URL 会进浏览器历史和日志）
        if path == '/api/logins/reveal':
            if not self._admin_only(me):
                return
            if not self._need_totp(self._body() or {}):
                return
            self._json({'ok': True, 'logins': login_log.recent(200),
                        'unlocked': True})
            return

        # ---- 谷歌验证码：启用（用户绑好后回来点一次）----
        if path == '/api/totp/bind':
            if not self._admin_only(me):
                return
            body = self._body() or {}
            # 启用前先让你输一次码，确认 App 里确实加对了 ——
            # 不然绑错了自己都不知道，下次想看真实 IP 就抓瞎
            if not totp.verify(self._totp_secret(), body.get('code')):
                # ★ 失败也要记一笔：用户报「我明明绑了却说没绑定」时，
                #   翻日志就能看出到底是「没点按钮」还是「码不对」
                log('启用谷歌验证码失败：验证码不对（用户可能输错、'
                    '或者手机时间不准、或者用的是另一台面板的密钥）')
                self._json({'ok': False,
                            'error': '验证码不对。检查：① 手机时间准不准'
                                     '（开「自动设置时间」）'
                                     '② 密钥是不是**这台面板**的'
                                     '（两台面板密钥不一样）'}, 400)
                return
            try:
                cfg = core.load_json(self._cfg_path(), {}) or {}
                cfg['totp_bound'] = True
                core.save_json(self._cfg_path(), cfg)
            except Exception as e:
                self._json({'ok': False, 'error': '保存失败：%s' % e})
                return
            log('谷歌验证码已绑定 —— 之后看真实 IP / 清空记录要输验证码')
            self._json({'ok': True})
            return

        # ---- 换一个密钥（手机丢了/换手机用）----
        if path == '/api/totp/reset':
            if not self._admin_only(me):
                return
            # ★ 已绑定的话必须先输旧码 —— 不然「重置」就成了绕过验证码的后门，
            #   光是知道面板密码的人就能把 2FA 拆掉
            if self._totp_bound() and not self._need_totp(self._body() or {}):
                return
            sec = totp.new_secret()
            self._save_totp_secret(sec)
            try:
                cfg = core.load_json(self._cfg_path(), {}) or {}
                cfg['totp_bound'] = False
                core.save_json(self._cfg_path(), cfg)
            except Exception:
                pass
            PanelHandler.totp_ok_until = 0
            log('换了一个新的谷歌验证密钥')
            self._json({'ok': True, 'secret': sec,
                        'url': totp.otpauth_url(sec)})
            return

        # 续期已合并进下面的 action 分发（/api/bots/<id>/extend）

        parts = path.strip('/').split('/')
        # /api/bots/<id>/<action>
        if len(parts) == 4 and parts[0] == 'api' and parts[1] == 'bots':
            bid, action = parts[2], parts[3]
            # 归属校验：不是自己的机器人，回的话跟「找不到」一样
            bot = self._bot_of(me, bid)
            if bot is None:
                return
            # 商户对自己人也只能做表里登记过的 action
            if (not MERCHANT_ACTIONS.get(action, False)
                    and not self._admin_only(me)):
                return
            if action == 'enable':
                good, err = self.mgr.set_enabled(bid, True)
                if not good:
                    self._json({'ok': False, 'error': err})
                    return
            elif action == 'disable':
                self.mgr.set_enabled(bid, False)
            elif action == 'newcode':
                self.mgr.new_code(bid)
            elif action == 'unbind':
                self.mgr.unbind(bid)
            elif action == 'delete':
                good, err = self.mgr.remove(bid)
                if not good:
                    self._json({'ok': False, 'error': err})
                    return
            elif action == 'restart':
                if not self.mgr.should_run(bot):
                    self._json({'ok': False, 'error': '机器人已停用或到期，请先启用或续期'})
                    return
                try:
                    self.mgr.restart_bot(bid)
                except (ValueError, OSError) as exc:
                    self._json({'ok': False, 'error': str(exc)})
                    return
            elif action == 'extend':
                d = (self._body() or {}).get('duration') or 'forever'
                ok2, err = self.mgr.extend(bid, d)
                if not ok2:
                    self._json({'ok': False, 'error': err})
                    return
            elif action == 'assign':
                if not self._admin_only(me):
                    return
                mid = (self._body() or {}).get('mid') or ''
                if mid and self.merch and not self.merch.get(mid):
                    self._json({'ok': False, 'error': '找不到这个商户'})
                    return
                ok2, err = self.mgr.assign(bid, mid)
                if not ok2:
                    self._json({'ok': False, 'error': err})
                    return
            elif action == 'token':
                token = (self._body() or {}).get('token') or ''
                ok2, err = self.mgr.set_token(bid, token)
                if not ok2:
                    self._json({'ok': False, 'error': err})
                    return
            elif action == 'remote':
                # 补票：添加的时候忘了勾「跑在客户自己的服务器上」，
                # 在这儿补上（不然只能删掉重建，而重建会换 id，
                # 已经发出去的安装包就失效了）
                ok2, err = self.mgr.set_remote(
                    bid, bool((self._body() or {}).get('on')))
                if not ok2:
                    self._json({'ok': False, 'error': err})
                    return
            elif action == 'note':
                # 改备注名。★ 只有管理员 —— 商户的「配置」权限只限商城，
                #   改名字不在里面（MERCHANT_ACTIONS 里没有这个键，上面已经拦了）
                note = (self._body() or {}).get('note') or ''
                ok2, err = self.mgr.set_note(bid, note)
                if not ok2:
                    self._json({'ok': False, 'error': err})
                    return
            else:
                self._json({'ok': False, 'error': '未知操作'})
                return
            self._json({'ok': True})
            return

        self._send(404, 'Not Found', 'text/plain; charset=utf-8')
