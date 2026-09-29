# -*- coding: utf-8 -*-
"""安全回归：XSS / SSRF / 重复发货 / JSON 原子性

★★ 这个文件**全程离线**，不联网、不碰真实 data/、不动线上任何东西：
   · SSRF 用**注入的假 DNS** 造样例，不真去解析
   · 重定向用**本机**一个桩 HTTP 服务（只在 127.0.0.1 上听一个随机端口）
   · 并发用线程打同一笔假流水，上游是 MockProvider（不花钱）
   · 写盘全在临时目录里

跑法：python _test_security.py
"""
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time

sys.stdout.reconfigure(encoding='utf-8')

_HERE = os.path.dirname(os.path.abspath(__file__))

# ★ 先把数据目录挪到临时目录 —— 别往真实 data/ 里写东西
_TMP = os.path.join(_HERE, '_tmp_security')
shutil.rmtree(_TMP, ignore_errors=True)
os.makedirs(os.path.join(_TMP, 'data'), exist_ok=True)

import core

core.BASE_DIR = _TMP
core.DATA_DIR = os.path.join(_TMP, 'data')

from runners.shop import pay as SP
from runners.shop import store as S
from runners.shop.providers import (MockProvider, UncertainError, TgError,
                                    check_public_url)

OK = []
BAD = []
SKIP = []


def check(name, cond, extra=''):
    (OK if cond else BAD).append(name)
    print('  %s %s%s' % ('✅' if cond else '❌', name,
                         ('  → %s' % (extra,)) if extra else ''))


def skip(name, why):
    SKIP.append(name)
    print('  ⏭ %s  → %s' % (name, why))


def read(p):
    return io.open(os.path.join(_HERE, p), encoding='utf-8').read()


# =====================================================================
print('=' * 68)
print('一、XSS：内联事件里的字符串转义')
print('=' * 68)

PAGES = ('panel_page.html', 'messages_page.html')
ATTR = re.compile(r'on(?:click|change|input|submit|load)\s*=\s*"((?:[^"\\]|\\.)*)"')

for pg in PAGES:
    html = read(pg)
    check('%s 有 jsq()' % pg, 'function jsq(s){' in html)
    check('%s 有 esc()（正常文本还得靠它）' % pg, 'function esc(' in html)

# --- 1.1 逐字节一致：两份 jsq 必须一模一样 ---
_js = {}
for pg in PAGES:
    lines = read(pg).split('\n')
    for i, l in enumerate(lines):
        if l.strip().startswith('function jsq(s){'):
            _js[pg] = '\n'.join(lines[i:i + 5])
            break
check('★ 两个页面的 jsq() 逐字节一致（改一份忘一份就会分叉）',
      len(set(_js.values())) == 1,
      '%d 种写法' % len(set(_js.values())))

# --- 1.2 源码扫描：内联事件里不许出现 esc() 拼参数 ---
#     ★ 这条是**防回退**的：以后谁再写一个 onclick="f(''+esc(x))，这里就红。
#       为什么 esc 在这里不管用：内联事件属性是**先做 HTML 解码、再当 JS 解析**，
#       esc("'") 产出 `&#39;`，浏览器解码回 `'`，照样闭合 JS 字符串。
for pg in PAGES:
    hits = []
    for n, line in enumerate(read(pg).split('\n'), 1):
        for m in ATTR.finditer(line):
            a = m.group(1)
            if 'esc(' in a:
                hits.append('%s:%d %s' % (pg, n, a[:70]))
            # 内联事件里出现 $ 拼接但没走 jsq 的，也拦一下
            elif re.search(r"'\s*\+", a) and 'jsq(' not in a:
                hits.append('%s:%d(没走 jsq) %s' % (pg, n, a[:70]))
    check('★ %s 的内联事件里没有 esc()/裸拼接（防回退）' % pg,
          not hits, '；'.join(hits[:4]))

# --- 1.3 真跑一遍 jsq()：用 node 执行**页面上那份原文** ---
NODE = shutil.which('node')
PAYLOADS = [
    "');alert(1);//",
    "');alert(1);//`",
    '<img src=x onerror=alert(1)>',
    'a"b\\c',
    'line1\nline2\rline3',
    '  ',
    '正常中文没问题',
    "单引号'双引号\"尖括号<>和&符号",
]
if not NODE:
    skip('jsq() 实际执行验证', '这台机器没装 node')
else:
    import subprocess
    fn = _js['panel_page.html']
    src = fn + '\n' + '''
var out = [];
PAYLOADS.forEach(function(s){ out.push(jsq(s)); });
console.log(JSON.stringify(out));
'''
    tmpjs = os.path.join(_TMP, '_jsq.js')
    body = ('var PAYLOADS = %s;\n' % json.dumps(PAYLOADS)) + src
    io.open(tmpjs, 'w', encoding='utf-8', newline='\n').write(body)
    r = subprocess.run([NODE, tmpjs], capture_output=True)
    if r.returncode != 0:
        check('jsq() 能被 node 跑起来', False,
              (r.stderr or b'').decode('utf-8', 'replace')[:200])
    else:
        got = json.loads((r.stdout or b'').decode('utf-8'))
        danger = set("'\"<>&\n\r") | {chr(0x2028), chr(0x2029)}
        bad = []
        for src_s, out_s in zip(PAYLOADS, got):
            if set(out_s) & danger:
                bad.append('%r → %r' % (src_s, out_s))
        check('★★ 危险字符（引号/尖括号/&/换行）在 jsq 产物里一个都没有',
              not bad, '；'.join(bad[:3]))
        # ★ 反证：esc() 挡不住 —— 产物里明晃晃带着 &#39;
        # ★ split 之后剩下的部分**从参数表开始**（"s){..."），别再补一个 s)
        esc_src = read('panel_page.html').split('function esc(')[1].split('\n}')[0]
        tmp2 = os.path.join(_TMP, '_esc.js')
        _xss = "x');alert(1);//"
        io.open(tmp2, 'w', encoding='utf-8', newline='\n').write(
            'function esc(' + esc_src + '\n}\n'
            'console.log(esc(%s));\n' % json.dumps(_xss))
        r2 = subprocess.run([NODE, tmp2], capture_output=True)
        out2 = (r2.stdout or b'').decode('utf-8', 'replace').strip()
        check('★★ esc() 的产物里带着 &#39;（反证：它在内联事件里挡不住）',
              '&#39;' in out2, out2[:60] if out2 else '（没跑出来）')

# --- 1.4 真正危险的那几处，确认改成了 jsq ---
html = read('panel_page.html')
check('★ IP 那一格（X-Forwarded-For 可控）走的是 jsq',
      "copyText(\\'' + jsq(r.ip)" in html)
msg = read('messages_page.html')
check('★ 群那一行（群名可控）走的是 jsq',
      "pickChat(\\'' + jsq(id)" in msg
      and "showCtx(event,\\'' + jsq(id)" in msg)
check('★ 面板页点群走的是 jsq',
      "msgOpenChat(\\'' + jsq(cid)" in html)

# =====================================================================
print()
print('=' * 68)
print('二、SSRF：自定义上游只能连公网')
print('=' * 68)


def fake(all_ips):
    return lambda host: all_ips


PUB = ['93.184.216.34']            # 真实存在的公网地址（只当字符串用，不联网）

# --- 2.1 各种内网/保留地址，一条条过 ---
BAD_URLS = [
    ('http://127.0.0.1/x', ['127.0.0.1'], '回环'),
    ('http://localhost/x', ['127.0.0.1'], 'localhost 解析到回环'),
    ('http://169.254.169.254/latest/meta-data/', ['169.254.169.254'],
     '★ 云元数据（能拿到机器临时密钥）'),
    ('http://10.0.0.5:6379/', ['10.0.0.5'], '内网 10 段'),
    ('http://192.168.1.1/', ['192.168.1.1'], '内网 192.168 段'),
    ('http://172.16.0.1/', ['172.16.0.1'], '内网 172.16 段'),
    ('http://100.64.0.1/', ['100.64.0.1'], '运营商 NAT 段'),
    ('http://0.0.0.0/', ['0.0.0.0'], '未指定地址'),
    ('http://[::1]/', ['::1'], 'IPv6 回环'),
    ('http://[fc00::1]/', ['fc00::1'], 'IPv6 私有'),
    ('http://[fe80::1]/', ['fe80::1'], 'IPv6 链路本地'),
    ('http://[::ffff:127.0.0.1]/', ['::ffff:127.0.0.1'], 'IPv4 映射的回环'),
    ('http://evil.com/', ['93.184.216.34', '127.0.0.1'],
     '★★ 多 A 记录：第一个公网、第二个回环（只查第一个就漏了）'),
    ('file:///etc/passwd', ['127.0.0.1'], '非 http 协议'),
    ('ftp://evil.com/x', ['93.184.216.34'], '非 http 协议'),
    ('http://user:pw@evil.com/', ['93.184.216.34'], '带 userinfo'),
    ('http:///', ['93.184.216.34'], '没有主机名'),
    ('', [], '空地址'),
    ('http://evil.com:99999/', PUB, '端口越界'),
]
for u, ips, why in BAD_URLS:
    ok, msg_txt = check_public_url(u, resolve=fake(ips))
    check('拦住 %-46s（%s）' % (u[:46] or '（空）', why), not ok,
          '居然放行了' if ok else msg_txt[:56])

# --- 2.2 正常公网要放行（别把功能一起挡了）---
for u in ('https://api.example.com/buy?key=k&addr={addr}',
          'http://upstream.other.com:8080/go',
          'https://api.x.com/v1/order'):
    ok, msg_txt = check_public_url(u, resolve=fake(PUB))
    check('放行正常公网  %s' % u[:44], ok, msg_txt[:60])

# --- 2.3 十进制/十六进制写法的 IP（不联网也能判：要么解析不了、要么拦）---
for u in ('http://2130706433/', 'http://0x7f000001/', 'http://0177.0.0.1/'):
    ok, _ = check_public_url(u)          # ★ 故意用真解析器
    check('拦住变形写法的回环 %s' % u, not ok)

# --- 2.4 供应商：请求前真的会验、不跟重定向、不走环境代理 ---
from runners.shop.providers import CustomProvider

ADDR = 'TBKw17Fxy5kBbev8MMZ16yVKuQTQfsETta'
cfg = {'custom_url': 'http://127.0.0.1:1/buy?key={key}&addr={addr}'}
cp = CustomProvider('k', '', cfg)
try:
    cp.order(ADDR, 32000, 1)
    check('★ order() 里真的会验地址（内网应当被拒）', False, '居然发出去了')
except TgError as e:
    check('★ order() 里真的会验地址（内网应当被拒）',
          '不能用' in str(e) or '内网' in str(e), str(e)[:70])
except UncertainError as e:
    check('★ order() 里真的会验地址（内网应当被拒）', False,
          '变成「结果不明」了，说明请求真发出去了：%s' % e)

check('★ 会话关掉了系统代理（trust_env=False）',
      CustomProvider('k', '', cfg)._session().trust_env is False)

_ok, _why = CustomProvider('k', '', cfg).self_test()
check('★ 自检跟发货走同一道校验（自检也拦内网）', not _ok, _why[:60])

# --- 2.5 302 跳内网：必须不跟过去 ---
import http.server
import socketserver


class _Redirect(http.server.BaseHTTPRequestHandler):
    hits = []

    def do_GET(self):
        _Redirect.hits.append(self.path)
        self.send_response(302)
        self.send_header('Location', 'http://169.254.169.254/latest/meta-data/')
        self.end_headers()

    def log_message(self, *a):
        pass


srv = socketserver.TCPServer(('127.0.0.1', 0), _Redirect)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

import runners.shop.providers as PV
_real_resolve = PV._default_resolve
# ★ 把 DNS 换掉，让「本机桩服务」看起来像公网 —— 这条测的是**重定向**，
#   不是公网校验那道门（那道门在上面单独测过了）
PV._default_resolve = lambda host: PUB
try:
    # ★ 地址仍写 127.0.0.1（真连得上），只是把**校验用的 DNS** 换掉，
    #   让它「看起来像公网」—— 这条要单独测的是重定向，不是公网那道门
    cp2 = CustomProvider('k', '', {
        'custom_url': 'http://127.0.0.1:%d/buy?addr={addr}' % port})
    try:
        cp2.order(ADDR, 32000, 1)
        r = '居然成功返回了'
    except UncertainError as e:
        r = str(e)
    except TgError as e:
        r = str(e)
    check('★★ 上游 302 跳内网 → 不跟过去（当结果不明停下来）',
          '跳转' in r, r[:76])
    check('★★ 桩服务只收到过一次请求（没有第二次跟过去的请求）',
          len(_Redirect.hits) == 1, '收到 %d 次' % len(_Redirect.hits))
finally:
    PV._default_resolve = _real_resolve
    srv.shutdown()
    srv.server_close()

# --- 2.6 防回退：存配置的地方也得调校验 ---
_api = read(os.path.join('runners', 'shop', 'api.py'))
check('★ 存 custom_url 时会先校验（写接口那道）',
      'check_public_url' in _api)

# =====================================================================
print()
print('=' * 68)
print('三、重复发货：并发也不能扣两次钱')
print('=' * 68)


class FakeMgr:
    def save(self):
        pass


class FakeRunner:
    def __init__(self, provider, cfg=None):
        self.bot = {'id': 'sec', 'shop': dict(cfg or {})}
        self.data = {}
        self.mgr = FakeMgr()
        self._prov = provider
        self.done = []
        self.failed = []

    def note(self):
        return '安全测试'

    def save_data(self):
        pass

    def provider(self):
        return self._prov

    def on_payment_seen(self, p):
        pass

    def on_payment_done(self, p):
        self.done.append(p)

    def on_payment_failed(self, p, e):
        self.failed.append((p, e))

    def on_payment_skipped(self, p):
        pass


class SlowProvider(MockProvider):
    """order() 故意慢一点 —— 不慢的话线程还没来得及撞上就结束了，
    测出来的「只调了一次」是假象。"""

    def __init__(self):
        super().__init__('')
        self.calls = 0
        self.lock = threading.Lock()

    def order(self, address, energy, hours=1, trace=''):
        with self.lock:
            self.calls += 1
        time.sleep(0.05)
        return 'SEC-TXID', {'amount': 1.1, 'balance': 9.0}


BASE = {'trx_own': ADDR, 'price': 3.5, 'energy': 32000, 'max': 10,
        'enabled': True}


def mk(prov=None):
    r = FakeRunner(prov or SlowProvider(), BASE)
    st = S.ShopStore(r)
    w = SP.PayWatcher(r, st, tron=object())
    return r, st, w


# --- 3.1 10 个线程同时发货同一笔 ---
r, st, w = mk()
pay = st.add_payment('SEC-1', ADDR, 3.5, 1, 32000, S.P_PENDING)
th = [threading.Thread(target=lambda: w.deliver(pay, ADDR, 32000), daemon=True)
      for _ in range(10)]
for t in th:
    t.start()
for t in th:
    t.join()
check('★★ 10 个线程同时发货 → 上游**只被调 1 次**',
      r._prov.calls == 1, '调了 %d 次' % r._prov.calls)
check('★ 结果只有一笔已发货', pay['status'] == S.P_DONE, pay['status'])
check('★ 没有留下 dispatched 标记', not pay.get('dispatched'))
check('★ 地址档案只记了 1 笔（没重复记账）',
      st.addrs.get(ADDR, {}).get('count') == 1)

# --- 3.2 9 个人工重发 + 1 个自动，同一时刻 ---
r, st, w = mk()
pay = st.add_payment('SEC-2', ADDR, 3.5, 1, 32000, S.P_PENDING)
th = [threading.Thread(target=lambda m=(i == 0): w.deliver(
        pay, ADDR, 32000, manual=m), daemon=True) for i in range(10)]
for t in th:
    t.start()
for t in th:
    t.join()
check('★★ 人工重发和自动扫描撞在一起 → 上游**只调 1 次**',
      r._prov.calls == 1, '调了 %d 次' % r._prov.calls)

# --- 3.3 已发货的，10 个线程一起点重发 ---
before = r._prov.calls
th = [threading.Thread(target=lambda: w.deliver(pay, ADDR, 32000, manual=True),
                       daemon=True)
      for _ in range(10)]
for t in th:
    t.start()
for t in th:
    t.join()
check('★★ 已发货的单子并发重发 → 上游一次都不调',
      r._prov.calls == before, '多调了 %d 次' % (r._prov.calls - before))

# --- 3.4 落盘失败 → 绝不许发货 ---
r, st, w = mk()
pay = st.add_payment('SEC-3', ADDR, 3.5, 1, 32000, S.P_PENDING)
w._save_hard = lambda: (_ for _ in ()).throw(OSError('磁盘满了'))
ok = w.deliver(pay, ADDR, 32000)
check('★★ 发货前状态存不进磁盘 → 这笔不发（上游 0 次调用）',
      r._prov.calls == 0 and ok is False,
      '调了 %d 次' % r._prov.calls)
check('★ 发不出去也不能留 dispatched 标记（否则重启后被当成「发过了」）',
      not pay.get('dispatched'))
check('★ 失败后还能重试（没有被永久锁死）',
      w._key(pay) not in w._shipping)

# --- 3.5 「结果不明」的单子：并发也不许自动重发 ---
class UnclearProvider(MockProvider):
    def __init__(self):
        super().__init__('')
        self.calls = 0

    def order(self, address, energy, hours=1, trace=''):
        self.calls += 1
        time.sleep(0.05)
        raise UncertainError('模拟超时')


r, st, w = mk(UnclearProvider())
pay = st.add_payment('SEC-4', ADDR, 3.5, 1, 32000, S.P_PENDING)
w.deliver(pay, ADDR, 32000)
c1 = r._prov.calls
check('★ 结果不明 → 状态留在待处理', pay['status'] == S.P_PENDING
      and bool(pay.get('dispatched')))
th = [threading.Thread(target=lambda: w.deliver(pay, ADDR, 32000), daemon=True)
      for _ in range(10)]
for t in th:
    t.start()
for t in th:
    t.join()
check('★★ 结果不明的单子并发重试 → 一次都不再打上游',
      r._prov.calls == c1, '多调了 %d 次' % (r._prov.calls - c1))
n = w.retry_pending()
check('★ 自动补发流程也跳过它', n == 0 and r._prov.calls == c1,
      '补发 %d 笔' % n)

# --- 3.6 进程重启：dispatched 落盘了 → 不能被当成可重试 ---
r, st, w2 = mk(SlowProvider())
snap = st.data                      # 模拟「重启」：新 watcher、同一份盘上数据
w3 = SP.PayWatcher(r, st, tron=object())
check('★ 重启后 _shipping 是空的（进程内状态本来就没了）',
      not w3._shipping)
c0 = r._prov.calls
w3.deliver(pay, ADDR, 32000)
check('★★ 重启后那笔「结果不明」仍然不自动重发（靠的是落盘的标记）',
      r._prov.calls == c0, '多调了 %d 次' % (r._prov.calls - c0))

# --- 3.7 上游明确失败 → 标记清掉，之后还能重发（别把功能锁死）---
class FailProvider(MockProvider):
    def __init__(self):
        super().__init__('')
        self.calls = 0

    def order(self, address, energy, hours=1, trace=''):
        self.calls += 1
        raise TgError('模拟明确失败')


r, st, w = mk(FailProvider())
pay = st.add_payment('SEC-5', ADDR, 3.5, 1, 32000, S.P_PENDING)
w.deliver(pay, ADDR, 32000)
check('★ 明确失败 → 状态 failed 且不留标记（钱没扣，可以重发）',
      pay['status'] == S.P_FAILED and not pay.get('dispatched'))
check('★ 明确失败后 _shipping 清干净了（没卡住这笔）',
      w._key(pay) not in w._shipping)

# =====================================================================
print()
print('=' * 68)
print('四、JSON 落盘：原子、并发、写不进去就别覆盖')
print('=' * 68)

W = os.path.join(_TMP, 'data', 'w.json')

# --- 4.1 并发写同一路径 ---
core.save_json(W, {'n': 0, 'rows': []})
bad_json = []
stop = threading.Event()


def writer(idx):
    for i in range(40):
        core.save_json(W, {'n': idx, 'rows': list(range(i))})
        try:
            core.load_json(W, None)
        except Exception as e:              # 读到半个文件
            bad_json.append('第%d轮 %s' % (i, e))


def reader():
    """★ 专门盯「文件不存在」的窗口：老实现是先 delete 再 rename"""
    missing = 0
    while not stop.is_set():
        if not os.path.exists(W):
            missing += 1
        time.sleep(0.0005)
    return missing


th = [threading.Thread(target=writer, args=(i,), daemon=True)
      for i in range(8)]
rd = threading.Thread(target=reader, daemon=True)
rd.start()
for t in th:
    t.start()
for t in th:
    t.join()
stop.set()
rd.join()
check('★★ 8 线程并发写 → 过程中读到的一直是合法 JSON',
      not bad_json, '；'.join(bad_json[:3]))
final = core.load_json(W, None)
check('★ 最后落盘的是个正常对象', isinstance(final, dict) and 'rows' in final)
leftover = [f for f in os.listdir(os.path.dirname(W)) if '.tmp' in f]
check('★ 没有留下 .tmp 碎片', not leftover, str(leftover[:3]))

# --- 4.2 读的时候文件一直在（原子替换，不是先删后改名）---
W2 = os.path.join(_TMP, 'data', 'w2.json')
core.save_json(W2, {'a': 1})
missing_seen = []
stop2 = threading.Event()


def swap():
    for _ in range(200):
        core.save_json(W2, {'a': 2, 'b': [1, 2, 3]})
    stop2.set()


def watch():
    while not stop2.is_set():
        if not os.path.exists(W2):
            missing_seen.append(1)
        time.sleep(0.0002)


t1 = threading.Thread(target=swap, daemon=True)
t2 = threading.Thread(target=watch, daemon=True)
t2.start()
t1.start()
t1.join()
t2.join()
check('★★ 反复覆盖的过程中，文件**一次都没消失过**（不存在「中间态」）',
      not missing_seen, '消失过 %d 次' % len(missing_seen))

# --- 4.3 写失败：原文件必须原样还在 ---
W3 = os.path.join(_TMP, 'data', 'w3.json')
core.save_json(W3, {'keep': '这是原来的内容'})
before_txt = io.open(W3, encoding='utf-8').read()
try:
    core.save_json(W3, {'bad': object()})       # 序列化不了
    check('★ 序列化失败要抛出来（不能静默）', False, '居然没报错')
except Exception:
    check('★ 序列化失败要抛出来（不能静默）', True)
after_txt = io.open(W3, encoding='utf-8').read()
check('★★ 写失败之后原文件一字未动', before_txt == after_txt)
check('★ 失败也没留下 .tmp',
      not [f for f in os.listdir(os.path.dirname(W3)) if '.tmp' in f])

# --- 4.4 「边 dump 边被改」不再整份丢掉 ---
W4 = os.path.join(_TMP, 'data', 'w4.json')
big = {'rows': [{'i': i} for i in range(4000)]}
core.save_json(W4, big)
errs = []
stop3 = threading.Event()


def churn():
    i = 0
    while not stop3.is_set():
        big['rows'].append({'i': i})
        i += 1
        time.sleep(0.0001)


def dump_loop():
    for _ in range(30):
        try:
            core.save_json(W4, big)
        except Exception as e:
            errs.append(str(e))


t1 = threading.Thread(target=churn, daemon=True)
t2 = threading.Thread(target=dump_loop, daemon=True)
t1.start()
t2.start()
t2.join()
stop3.set()
t1.join()
check('★★ 一边改字典一边落盘 → 不报「dictionary changed size」',
      not [e for e in errs if 'changed size' in e], '；'.join(errs[:2]))
check('★ 这轮写盘没有抛别的异常', not errs, '；'.join(errs[:2]))

# --- 4.5 同一个路径共用一把锁（两个写入方）---
from runners.shop.store import StoreShim
shim = StoreShim(FakeMgr(), {'id': 'lockprobe'})
check('★ 面板那份 StoreShim 走的也是 core.save_json（共用同一把路径锁）',
      'core.save_json' in io.open(
          os.path.join(_HERE, 'runners', 'shop', 'store.py'),
          encoding='utf-8').read())

# =====================================================================
shutil.rmtree(_TMP, ignore_errors=True)
print()
print('=' * 68)
print('结果：%d 项通过，%d 项失败，%d 项跳过' % (len(OK), len(BAD), len(SKIP)))
if BAD:
    print('失败项：')
    for b in BAD:
        print('  ❌', b)
if SKIP:
    print('跳过项：')
    for s in SKIP:
        print('  ⏭', s)
print('=' * 68)
sys.exit(1 if BAD else 0)
