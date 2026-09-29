# -*- coding: utf-8 -*-
"""多租户隔离测试 —— 商户绝不能看到 / 碰到别人的机器人

这是第一个真正打 HTTP 测 panel.py 的测试：起个真服务，用两个商户的
账号去打对方的路由，验证全部被挡住。
"""
import os
import shutil
import sys
import threading
import time
from http.server import ThreadingHTTPServer

sys.stdout.reconfigure(encoding='utf-8')

import requests

# ---- 先把路径挪到临时目录，绝不能碰真数据 ----
HERE = os.path.dirname(os.path.abspath(__file__))
TMP = os.path.join(HERE, '_tmp_tenant')
if os.path.exists(TMP):
    shutil.rmtree(TMP)
os.makedirs(os.path.join(TMP, 'data'), exist_ok=True)

import core
core.BASE_DIR = TMP
core.BOTS_FILE = os.path.join(TMP, 'bots.json')
core.DATA_DIR = os.path.join(TMP, 'data')

from manager import BotManager
from merchants import MerchantStore
from panel import PanelHandler

OK, BAD = [], []
ADMIN_PW = 'ADMINPW123'


def check(name, cond, extra=''):
    (OK if cond else BAD).append(name)
    print('  %s %s%s' % ('✅' if cond else '❌', name,
                         ('  → %s' % (extra,)) if extra else ''))


# ================= 造数据 =================
def mk_bot(bid, note, mid, coin_cfg=True):
    return {
        'id': bid, 'type': 'shop', 'note': note, 'token': '%d:FAKE' % (hash(bid) % 900 + 100),
        'username': 'bot_' + bid, 'name': note, 'bind_code': 'CODE' + bid.upper(),
        'admin_ids': [], 'admin_name': '', 'admin_username': '',
        'enabled': True, 'created': '2026-09-26 12:00', 'created_ts': int(time.time()),
        'duration': 'forever', 'expire_at': 0, 'expired_at': 0, 'mid': mid,
        'shop': {'enabled': True,
                 'trx_own': 'TBKw17Fxy5kBbev8MMZ16yVKuQTQfsETta' if coin_cfg else '',
                 'price': 3.5, 'energy': 65000, 'max': 10,
                 'provider': 'mock', 'provider_key': 'SECRET-KEY-' + bid,
                 'premium_key': 'PREMIUM-KEY-' + bid,
                 'contact': '@kefu_' + bid},
    }


core.save_json(core.BOTS_FILE, {'bots': [
    mk_bot('aaaa1111', 'A的商城', ''),      # 先无主，等下分给 A
    mk_bot('bbbb2222', 'B的商城', 'B_MID'),
    mk_bot('cccc3333', '管理员直管', ''),
]})

PanelHandler.mgr = BotManager({})
PanelHandler.password = ADMIN_PW
PanelHandler.merch = MerchantStore()
PanelHandler.token_secret = 'S' * 32
PanelHandler.page_cache = None

# 两个商户
mA, _ = PanelHandler.merch.add('张三', 'zhangsan')
mB, _ = PanelHandler.merch.add('李四', 'lisi')
# 先跳过「首次必须改密码」，测完那条单独测
PanelHandler.merch.set_pass(mA['mid'], 'zhangsan123', must_change=False)
PanelHandler.merch.set_pass(mB['mid'], 'lisi123456', must_change=False)
# B 的机器人归 B
PanelHandler.mgr.assign('bbbb2222', mB['mid'])
# A 的机器人分给 A（顺便验证清空机密）
PanelHandler.mgr.assign_bots(mA['mid'], ['aaaa1111'], clear_secrets=True)

httpd = ThreadingHTTPServer(('127.0.0.1', 0), PanelHandler)
PORT = httpd.server_address[1]
BASE = 'http://127.0.0.1:%d' % PORT
threading.Thread(target=httpd.serve_forever, daemon=True).start()
time.sleep(0.3)

H_ADMIN = {'X-Panel-Pass': ADMIN_PW}
TOK_A = TOK_B = ''


def post(path, body=None, headers=None):
    return requests.post(BASE + path, json=body or {}, headers=headers or {},
                         timeout=10)


def get(path, headers=None):
    return requests.get(BASE + path, headers=headers or {}, timeout=10)


print('=' * 64)
print('一、登录')
print('=' * 64)
r = post('/api/login', {'user': '', 'pass': ADMIN_PW})
check('管理员登录（用户名留空）', r.status_code == 200 and r.json().get('role') == 'admin',
      str(r.json())[:60])

r = post('/api/login', {'user': 'zhangsan', 'pass': 'zhangsan123'})
ja = r.json()
TOK_A = ja.get('token') or ''
check('商户 A 登录成功', r.status_code == 200 and ja.get('role') == 'merchant')
check('拿到了 token', bool(TOK_A))
check('商户 B 登录', post('/api/login', {'user': 'lisi', 'pass': 'lisi123456'}
                          ).json().get('role') == 'merchant')
TOK_B = post('/api/login', {'user': 'lisi', 'pass': 'lisi123456'}).json()['token']

r = post('/api/login', {'user': 'zhangsan', 'pass': 'WRONG'})
check('密码错 → 401', r.status_code == 401)
check('不说用户名存不存在', '账号或密码' in r.json().get('error', ''),
      r.json().get('error'))
r = post('/api/login', {'user': 'nobody_here', 'pass': 'x'})
check('不存在的用户名也是同一句话', '账号或密码' in r.json().get('error', ''))

HA = {'X-Panel-Token': TOK_A}
HB = {'X-Panel-Token': TOK_B}

print()
print('=' * 64)
print('二、机器人列表隔离')
print('=' * 64)
ja = get('/api/bots', H_ADMIN).json()
ids_admin = {b['id'] for b in ja['bots']}
check('管理员看到全部 3 台', ids_admin == {'aaaa1111', 'bbbb2222', 'cccc3333'},
      str(ids_admin))
check('响应里带 me.role', ja.get('me', {}).get('role') == 'admin')

ja = get('/api/bots', HA).json()
ids_a = {b['id'] for b in ja['bots']}
check('★ 商户 A 只看到自己的 1 台', ids_a == {'aaaa1111'}, str(ids_a))
check('★ A 看不到 B 的机器人', 'bbbb2222' not in ids_a)
check('★ A 看不到管理员的机器人', 'cccc3333' not in ids_a)
check('A 的身份是 merchant', ja.get('me', {}).get('role') == 'merchant')
check('A 的名字显示对', ja.get('me', {}).get('name') == '张三',
      ja.get('me', {}).get('name'))

jb = get('/api/bots', HB).json()
check('★ 商户 B 只看到自己的 1 台',
      {b['id'] for b in jb['bots']} == {'bbbb2222'})

print()
print('=' * 64)
print('三、越权（全部必须被挡住）')
print('=' * 64)
OTHER = 'bbbb2222'          # B 的机器人
MINE = 'aaaa1111'           # A 自己的

# 看别人配置 —— 这里返回的可是明文上游 key
r = get('/api/shop/%s/config' % OTHER, HA)
body = r.text
check('★ A 读不到 B 的配置', r.status_code == 404 and not r.json().get('ok'),
      'HTTP %s' % r.status_code)
check('★ 响应里没有 B 的 provider_key', 'SECRET-KEY-bbbb2222' not in body)
check('★ 响应里没有 B 的收款地址', 'TNDyeJsZyv7my43' not in body
      or OTHER != 'bbbb2222')

r = get('/api/shop/%s/payments' % OTHER, HA)
check('★ A 读不到 B 的流水', r.status_code == 404 and not r.json().get('ok'))

r = post('/api/shop/%s/providertest' % OTHER, {}, HA)
check('★ A 不能拿 B 的 key 去探测上游', r.status_code == 404)

r = post('/api/shop/%s/payment/FAKE/retry' % OTHER, {}, HA)
check('★ A 不能重发 B 的流水（会花 B 的钱）', r.status_code == 404)

r = post('/api/shop/%s/payment/FAKE/mark' % OTHER, {}, HA)
check('★ A 不能标记 B 的流水', r.status_code == 404)

# 改别人的配置 —— 最危险的：把收款地址改成自己的
r = post('/api/shop/%s/config' % OTHER,
         {'trx_own': 'TUaZ5nGofnSFpoYB5DSfqpZBZAAbmuWzcF'}, HA)
check('★ A 改不了 B 的收款地址', r.status_code == 404)
b_b = PanelHandler.mgr.find(OTHER)
check('★ B 的收款地址没被改动',
      b_b['shop'].get('trx_own') == 'TBKw17Fxy5kBbev8MMZ16yVKuQTQfsETta',
      str(b_b['shop'].get('trx_own'))[:20])

for act in ('delete', 'enable', 'disable', 'newcode', 'unbind', 'extend'):
    r = post('/api/bots/%s/%s' % (OTHER, act),
             {'duration': 'forever'} if act == 'extend' else {}, HA)
    check('★ A 不能对 B 的机器人做 %s' % act, r.status_code in (403, 404),
          'HTTP %s' % r.status_code)
check('★ B 的机器人还在', PanelHandler.mgr.find(OTHER) is not None)

r = post('/api/bots/%s/delete' % 'cccc3333', {}, HA)
check('★ A 不能删管理员的机器人', r.status_code in (403, 404))
check('★ 管理员的机器人还在', PanelHandler.mgr.find('cccc3333') is not None)

r = post('/api/bots', {'note': '偷加的', 'token': '1:FAKE', 'type': 'shop'}, HA)
check('★ 商户不能自己加机器人', r.status_code == 403, 'HTTP %s' % r.status_code)

r = get('/api/merchants', HA)
check('★ 商户看不到商户列表', r.status_code == 403)
r = post('/api/merchants', {'name': 'x', 'user': 'hacker'}, HA)
check('★ 商户不能建商户', r.status_code == 403)

r = get('/api/bots/%s/token' % OTHER, HA)
check('★ A 读不到 B 的机器人 token', r.status_code == 404)

print()
print('=' * 64)
print('四、自己的机器人该能用的都能用')
print('=' * 64)
r = get('/api/shop/%s/config' % MINE, HA)
check('A 读得到自己的配置', r.status_code == 200 and r.json().get('ok'))

r = post('/api/shop/%s/config' % MINE, {'price': 4.2, 'trx_own':
                                        'TUaZ5nGofnSFpoYB5DSfqpZBZAAbmuWzcF'}, HA)
check('★ A 改得了自己的价格和收款地址', r.json().get('ok') is True,
      str(r.json())[:70])
check('改收款地址会重置扫描基线（防白送能量）',
      r.json().get('baseline_reset') is True)
check('落盘了', PanelHandler.mgr.find(MINE)['shop']['price'] == 4.2)

r = get('/api/shop/%s/payments' % MINE, HA)
check('A 看得到自己的流水', r.status_code == 200 and r.json().get('ok'))

r = get('/api/bots/%s/token' % MINE, HA)
check('A 读得到自己的 token', r.status_code == 200 and r.json().get('token'),
      str(r.json().get('token'))[:16])

print()
print('=' * 64)
print('五、分配机器人时会清掉前主人的机密')
print('=' * 64)
# cccc3333 本来带着"管理员"的收款地址和 key，分给 B 看看
before = PanelHandler.mgr.find('cccc3333')['shop']
check('分配前确实有机密', bool(before.get('trx_own'))
      and bool(before.get('provider_key')))
r = post('/api/merchants/%s/bots' % mB['mid'],
         {'bids': ['bbbb2222', 'cccc3333'], 'clear_secrets': True}, H_ADMIN)
check('管理员能分配机器人', r.json().get('ok') is True, str(r.json()))
after = PanelHandler.mgr.find('cccc3333')['shop']
check('★ 收款地址被清空了', not after.get('trx_own'), str(after.get('trx_own')))
check('★ 上游 key 被清空了', not after.get('provider_key'))
check('★ 会员 key 被清空了', not after.get('premium_key'))
check('★ 客服联系方式也清空了', not after.get('contact'))
check('扫描基线被重置（否则会白送能量）',
      not (core.load_json(os.path.join(TMP, 'data', 'cccc3333.json'), {})
           .get('scan', {}).get('baseline')))
check('★ 返回里报告了清空数量（两台都带机密）',
      r.json().get('cleared') == 2, str(r.json()))

print()
print('=' * 64)
print('六、首次登录必须改密码')
print('=' * 64)
mC, pwC = PanelHandler.merch.add('王五', 'wangwu')
r = post('/api/login', {'user': 'wangwu', 'pass': pwC})
jc = r.json()
check('新商户能登录', r.status_code == 200)
check('标记了要改密码', jc.get('must_change') is True)
HC = {'X-Panel-Token': jc.get('token')}
r = get('/api/bots', HC)
check('★ 没改密码前，什么都干不了', r.status_code == 403
      and r.json().get('code') == 'must_change', 'HTTP %s' % r.status_code)
r = post('/api/merchants', {'name': 'x', 'user': 'yyy'}, HC)
check('★ 没改密码前也建不了商户', r.status_code == 403)

r = post('/api/me/password', {'old': pwC, 'new': 'abc'}, HC)
check('密码太短被拒', r.status_code == 200 and not r.json().get('ok'),
      str(r.json().get('error')))
r = post('/api/me/password', {'old': pwC, 'new': 'wangwu888'}, HC)
check('改密码成功', r.json().get('ok') is True, str(r.json())[:60])
new_tok = r.json().get('token')
check('返回了新 token', bool(new_tok))
check('★ 旧 token 立刻失效（sess_ver 生效）',
      get('/api/bots', HC).status_code == 401)
check('新 token 能用',
      get('/api/bots', {'X-Panel-Token': new_tok}).status_code == 200)
check('用新密码能重新登录',
      post('/api/login', {'user': 'wangwu', 'pass': 'wangwu888'}
           ).status_code == 200)

print()
print('=' * 64)
print('七、停用 / 重置 / 删除 / token 过期')
print('=' * 64)
post('/api/merchants/%s/disable' % mA['mid'], {}, H_ADMIN)
check('★ 停用后 A 的 token 立刻失效', get('/api/bots', HA).status_code == 401)
check('★ 停用 A 不影响 B', get('/api/bots', HB).status_code == 200)
check('★ 停用 A 不影响管理员', get('/api/bots', H_ADMIN).status_code == 200)
post('/api/merchants/%s/enable' % mA['mid'], {}, H_ADMIN)
check('启用后又能用了', get('/api/bots', HA).status_code == 200)

r = post('/api/merchants/%s/reset' % mA['mid'], {}, H_ADMIN)
newpw = r.json().get('pass')
check('能重置密码', bool(newpw), str(newpw))
check('★ 重置后旧 token 失效', get('/api/bots', HA).status_code == 401)
check('重置后要求改密码',
      post('/api/login', {'user': 'zhangsan', 'pass': newpw}
           ).json().get('must_change') is True)

# ★★ 重置出来的密码必须**真的能用**。
#   背景：`gen_code` 的字符集是「26 个大写字母 + 10 个数字」，
#   纯字母的概率 (26/36)^8 ≈ 7.7%；而 pass_ok 明确不收纯字母/纯数字。
#   老代码 `reset_pass` **忽略了 set_pass 的失败**，照样把生成的密码报出来
#   → 大约每重置 13 次就有 1 次，商户拿到一个**登不进去的密码**。
#   （这个 bug 以前一直表现为「_test_tenant 偶尔失败一次」，
#     被当成偶发放过去了很久；2026-09-29 才揪出来）
from merchants import pass_ok                            # noqa: E402
_bad = 0
_last = ''
for _ in range(100):
    _last, _ = PanelHandler.merch.reset_pass(mA['mid'])
    if not _last or not pass_ok(_last)[0]:
        _bad += 1
check('★★ 重置 100 次，生成的密码全都合格（不能是纯字母/纯数字）',
      _bad == 0, '不合格 %d 次' % _bad)
check('★ 而且最后那个密码真的能登录进去',
      PanelHandler.merch.verify('zhangsan', _last) is not None)

check('有机器人的商户不让删',
      post('/api/merchants/%s/delete' % mB['mid'], {}, H_ADMIN)
      .json().get('ok') is False)
check('★ 管理员的商户列表能看到',
      len(get('/api/merchants', H_ADMIN).json().get('merchants') or []) == 3)
check('★ 商户列表里没有密码哈希',
      'hash' not in str(get('/api/merchants', H_ADMIN).json()))

bad = 'x' * 20 + '.' + '1' + '.' + str(int(time.time()) + 9999) + '.' + 'y' * 32
check('乱签的 token 被拒',
      get('/api/bots', {'X-Panel-Token': bad}).status_code == 401)
check('没有凭据 → 401', get('/api/bots').status_code == 401)

print()
print('=' * 64)
print('八、管理员兼容性（现有脚本用的 X-Panel-Pass 不能坏）')
print('=' * 64)
check('管理员密码头照常能用',
      get('/api/bots', H_ADMIN).status_code == 200)
check('管理员能看所有机器人的配置',
      get('/api/shop/bbbb2222/config', H_ADMIN).status_code == 200)
# 前面分配时把 key 清空了，这里补一个再验证管理员看得见
_b = PanelHandler.mgr.find('bbbb2222')
_b['shop']['provider_key'] = 'SECRET-KEY-bbbb2222'
PanelHandler.mgr.save()
check('管理员的配置里能拿到明文 key（他要能维护）',
      'SECRET-KEY-bbbb2222'
      in get('/api/shop/bbbb2222/config', H_ADMIN).text)
check('/m 和 / 返回同一个页面',
      get('/m').status_code == 200 and get('/').status_code == 200)

# 收尾
httpd.shutdown()
shutil.rmtree(TMP, ignore_errors=True)

print()
print('=' * 64)
print('结果：%d 项通过，%d 项失败' % (len(OK), len(BAD)))
for b in BAD:
    print('  ❌', b)
print('=' * 64)
sys.exit(1 if BAD else 0)
