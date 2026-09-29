# -*- coding: utf-8 -*-
"""自定义上游：占位符、取值路径、成功判定、订单号提取、防重复扣费分类"""
import sys

sys.stdout.reconfigure(encoding='utf-8')

from core import TgError
from runners.shop.providers import CustomProvider, UncertainError, get_provider

# ★★ 把「域名解析」换掉：`api.test` 是个不存在的域名，
#    而 `CustomProvider.order()` 现在会先验「目标是不是公网」（SSRF 防护），
#    真去解析它必然失败 → 这个测试会全红。
#    这里 HTTP 那一层本来就是**桩**（FakeSess，不发真请求），
#    所以要验的是占位符/取路径/判定这些逻辑，不该被 DNS 挡着。
#    ★ 公网校验本身在 `_test_security.py` 里单独测（那边用注入的假 DNS
#      一条条过内网/混合/重定向的样例）。
import runners.shop.providers as _PV

_PV._default_resolve = lambda host: ['93.184.216.34']

OK, BAD = [], []
ADDR = 'TUaZ5nGofnSFpoYB5DSfqpZBZAAbmuWzcF'


def check(name, cond, extra=''):
    (OK if cond else BAD).append(name)
    print('  %s %s%s' % ('✅' if cond else '❌', name,
                         ('  → %s' % (extra,)) if extra else ''))


class Resp:
    def __init__(self, obj=None, status=200, text=''):
        self.status_code = status
        self._o = obj
        self.text = text or (str(obj) if obj is not None else '')

    def json(self):
        if self._o is None:
            raise ValueError('不是 JSON')
        return self._o


class FakeSess:
    def __init__(self, resp):
        self.resp = resp
        self.seen = []

    def get(self, url, **kw):
        self.seen.append(('GET', url))
        return self.resp

    def post(self, url, **kw):
        self.seen.append(('POST', url))
        return self.resp


DEFAULT_URL = ('https://api.test/buy?key={key}&addr={addr}'
               '&e={energy}&h={hours}')


def mk(resp, url=None, **cfg):
    c = {'custom_url': url if url is not None else DEFAULT_URL}
    c.update(cfg)
    p = CustomProvider('MYKEY', '', c)
    p._sess = FakeSess(resp)
    return p


print('=' * 64)
print('一、占位符替换')
print('=' * 64)
p = mk(Resp({'code': 200, 'data': {'orderId': 'A1'}}))
p.order(ADDR, 65000, 1)
url = p._sess.seen[-1][1]
check('走的是 GET', p._sess.seen[-1][0] == 'GET')
check('{key} 换成了 API Key', 'key=MYKEY' in url)
check('{addr} 换成了地址', 'addr=' + ADDR in url)
check('{energy} 换成了能量数', 'e=65000' in url)
check('{hours} 换成了小时数', 'h=1' in url)
check('地址里没有残留占位符', '{' not in url, url[:70])

p = mk(Resp({'code': 200, 'data': {'orderId': 'A1'}}), custom_method='POST')
p.order(ADDR, 32000, 24)
check('选 POST 就走 POST', p._sess.seen[-1][0] == 'POST')
check('POST 也一样替换占位符', 'e=32000' in p._sess.seen[-1][1]
      and 'h=24' in p._sess.seen[-1][1])

print()
print('=' * 64)
print('二、订单号取值路径')
print('=' * 64)
for path, obj, want in (
        ('data.orderId', {'data': {'orderId': 'X1'}}, 'X1'),
        ('orderId', {'orderId': 'X2'}, 'X2'),
        ('result.id', {'result': {'id': 'X3'}}, 'X3'),
        ('data.list.0.id', {'data': {'list': [{'id': 'X4'}]}}, 'X4'),
        ('data.orderId', {'data': {}}, None),
        ('a.b.c', {'a': 'string'}, None),
        ('', {'x': 1}, None),
):
    got = CustomProvider.dig(obj, path)
    check('dig(%r) → %s' % (path, want), got == want, repr(got))

print()
print('=' * 64)
print('三、成功判定 + 订单号提取')
print('=' * 64)
p = mk(Resp({'code': 200, 'data': {'orderId': 'OK-1'}}))
oid, raw = p.order(ADDR, 65000, 1)
check('code=200 → 成功', oid == 'OK-1', oid)

# 常见的几种「成功」写法
for obj, cfg_ok, want in (
        ({'code': 0, 'data': {'orderId': 'Z1'}}, ('code', '0'), 'Z1'),
        ({'status': 'success', 'orderId': 'Z2'}, ('status', 'success'), 'Z2'),
        ({'success': True, 'data': {'orderId': 'Z3'}},
         ('success', 'True'), 'Z3'),
):
    path, val = cfg_ok
    p = mk(Resp(obj), custom_ok_path=path, custom_ok_value=val,
           custom_order_path='data.orderId')
    oid, _ = p.order(ADDR, 65000, 1)
    check('自定义判定 %s=%s 能认成功' % cfg_ok, oid == want, oid)

# 订单号回退：配的路径取不到就试 data.txid / txid
p = mk(Resp({'code': 200, 'data': {'txid': 'TX-9'}}))
oid, _ = p.order(ADDR, 65000, 1)
check('配的路径取不到时回退到 txid', oid == 'TX-9', oid)

print()
print('=' * 64)
print('四、失败分类（决定会不会重复扣钱）')
print('=' * 64)
# 上游明确说失败 → TgError（钱没扣，可以安全重发）
p = mk(Resp({'code': 500, 'message': 'insufficient balance'}))
try:
    p.order(ADDR, 65000, 1)
    check('上游说失败要抛错', False, '居然没抛')
except TgError as e:
    check('★ 上游明确失败 → TgError（可安全重发）',
          not isinstance(e, UncertainError), str(e)[:50])

# 连不上 → UncertainError（可能已经扣了，绝不能重发）
class Boom:
    def get(self, url, **kw):
        raise OSError('网络断了')

    def post(self, url, **kw):
        raise OSError('网络断了')


p = mk(Resp({'code': 200}))
p._sess = Boom()
try:
    p.order(ADDR, 65000, 1)
    check('连不上要抛错', False, '居然没抛')
except UncertainError as e:
    check('★ 连不上 → UncertainError（结果不明，不能重发）', True, str(e)[:40])
except TgError as e:
    check('★ 连不上 → UncertainError（结果不明，不能重发）', False,
          '错抛成 TgError 了：%s' % e)

# HTTP 500 → UncertainError
p = mk(Resp({'code': 200}, status=502))
try:
    p.order(ADDR, 65000, 1)
    check('HTTP 502 要抛错', False, '居然没抛')
except UncertainError:
    check('★ HTTP 502 → UncertainError', True)
except TgError:
    check('★ HTTP 502 → UncertainError', False, '错抛成 TgError 了')

# 返回不是 JSON（比如被 WAF 挡了）→ UncertainError
p = mk(Resp(None, text='<html>blocked</html>'))
try:
    p.order(ADDR, 65000, 1)
    check('非 JSON 要抛错', False, '居然没抛')
except UncertainError:
    check('★ 返回不是 JSON → UncertainError', True)

# 回了成功但没订单号 → UncertainError（最危险，可能已经扣了钱）
p = mk(Resp({'code': 200, 'data': {}}))
try:
    p.order(ADDR, 65000, 1)
    check('成功但没订单号要抛错', False, '居然没抛')
except UncertainError as e:
    check('★ 成功但取不到订单号 → UncertainError', True, str(e)[:44])

print()
print('=' * 64)
print('五、参数校验')
print('=' * 64)
for bad in ('', 'abc', '0x123'):
    try:
        mk(Resp({'code': 200})).order(bad, 65000, 1)
        check('坏地址被拦(%r)' % bad, False, '居然没抛')
    except TgError as e:
        check('坏地址被拦(%r)' % (bad or '空'), '地址' in str(e))

p = CustomProvider('', '', {'custom_url': DEFAULT_URL})
try:
    p.order(ADDR, 65000, 1)
    check('没填 key 被拦', False)
except TgError as e:
    check('没填 API Key 被拦', 'API Key' in str(e), str(e)[:40])

p = CustomProvider('K', '', {})
try:
    p.order(ADDR, 65000, 1)
    check('没填地址被拦', False)
except TgError as e:
    check('没填接口地址被拦', '还没配好' in str(e), str(e)[:40])

print()
print('=' * 64)
print('六、自检 + 注册表')
print('=' * 64)
check('配好之前自检不过',
      CustomProvider('', '', {}).self_test()[0] is False)
check('只填地址没填 key 也过不了',
      CustomProvider('', '', {'custom_url': DEFAULT_URL}).self_test()[0] is False)
ok, msg = CustomProvider('K', '', {'custom_url': DEFAULT_URL}).self_test()
check('都填了自检通过', ok, msg[:50])

ok, msg = CustomProvider('K', '', {'custom_url': 'https://x/buy?key={key}'}
                         ).self_test()
check('★ 地址里没有 {addr} 会被自检拦下', not ok, msg[:44])

check('custom 已注册进上游列表', get_provider('custom') is not None
      and get_provider('custom').name == 'custom')
check('未知上游仍然退回 mock',
      get_provider('不存在的').name == 'mock')
check('自定义上游查不到余额（返回 None）',
      CustomProvider('K', '', {'custom_url': DEFAULT_URL}).balance() is None)
check('自定义上游报的价是 0（成本未知）',
      CustomProvider('K', '', {'custom_url': DEFAULT_URL}).quote(65000)[0] == 0.0)

print()
print('=' * 64)
print('结果：%d 项通过，%d 项失败' % (len(OK), len(BAD)))
for b in BAD:
    print('  ❌', b)
print('=' * 64)
sys.exit(1 if BAD else 0)
