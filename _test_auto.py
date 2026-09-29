# -*- coding: utf-8 -*-
"""笔数套餐（智能笔数）：下单、收款、剩余查询、菜单"""
import os
import sys
import time

sys.stdout.reconfigure(encoding='utf-8')

import os
import shutil

import core

# ★ 先把数据目录挪到临时目录 —— 别往**真实**的 data/ 里写假机器人。
#   崩一次就会留下脏文件，下一次跑读到上次的数据，断言莫名其妙地挂。
_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = os.path.join(_HERE, '_tmp_auto')
shutil.rmtree(_TMP, ignore_errors=True)
os.makedirs(os.path.join(_TMP, 'data'), exist_ok=True)
core.BASE_DIR = _TMP
core.DATA_DIR = os.path.join(_TMP, 'data')

from core import TgError
from runners.shop import store as S
from runners.shop import pay as SP
from runners.shop import ShopRunner
from runners.shop.providers import MockProvider, ApitrxProvider
from tron import fmt_amount

OK, BAD = [], []
BID = '_autotest'
ADDR = 'TUaZ5nGofnSFpoYB5DSfqpZBZAAbmuWzcF'
CFG = {'enabled': True, 'trx_own': 'TBKw17Fxy5kBbev8MMZ16yVKuQTQfsETta',
       'price': 3.5, 'energy': 65000, 'max': 10, 'provider': 'mock',
       'auto_enabled': True, 'auto_type': 1,
       'auto_prices': {'10': 30.0, '30': 85.0, '50': 140.0, '300': 750.0}}
# ★ auto_prices 默认是空的，档位完全由配置决定，不会被默认值掺进来


def check(name, cond, extra=''):
    (OK if cond else BAD).append(name)
    print('  %s %s%s' % ('✅' if cond else '❌', name,
                         ('  → %s' % (extra,)) if extra else ''))


class FakeMgr:
    def save(self):
        pass

    def is_expired(self, b):
        return False


class FakeAPI:
    def __init__(self):
        self.calls = []

    def call(self, method, **p):
        self.calls.append((method, p))
        return {'ok': True, 'result': {'message_id': 1}}

    def last_text(self):
        return self.calls[-1][1].get('text', '') if self.calls else ''

    def last_kb(self):
        return self.calls[-1][1].get('reply_markup') if self.calls else None


class FakeRunner:
    def __init__(self, cfg=None):
        self.bot = {'id': BID, 'token': '0:fake', 'note': '笔数测试',
                    'admin_ids': [111], 'owner_id': 111,
                    'shop': dict(cfg or CFG)}
        self.data = {}
        self.mgr = FakeMgr()
        self._prov = MockProvider('')
        self.failed, self.result = [], []

    def note(self):
        return '笔数测试'

    def save_data(self):
        pass

    def provider(self):
        return self._prov

    def on_payment_seen(self, p):
        pass

    def on_payment_done(self, p):
        pass

    def on_payment_failed(self, p, e):
        self.failed.append((p, e))

    def on_payment_skipped(self, p):
        pass

    def on_premium_paid(self, o):
        pass

    def on_premium_result(self, o):
        self.result.append(o)


def tx(txid, amount, frm=ADDR, coin='TRX'):
    return {'txid': txid, 'from': frm, 'amount': amount, 'coin': coin,
            'ts': int(time.time() * 1000)}


print('=' * 64)
print('一、上游接口的基本校验')
print('=' * 64)
p = ApitrxProvider('dummy-key')
for bad, why in (('', '空地址'), ('abc', '乱填'), ('0x123', '不是波场地址')):
    try:
        p.auto_buy(bad, 10)
        check('坏地址被拦(%s)' % why, False, '居然没报错')
    except TgError as e:
        check('坏地址被拦(%s)' % why, '地址' in str(e))
for bad_n in (0, 1, -5):
    try:
        p.auto_buy(ADDR, bad_n)
        check('笔数 %s 被拦（最低 2）' % bad_n, False, '居然没报错')
    except TgError as e:
        check('笔数 %s 被拦（最少 2）' % bad_n, '最少' in str(e))
try:
    p.auto_buy(ADDR, 10, auto_type=99)
    check('非法 autoType 被拦', False, '居然没报错')
except TgError as e:
    check('非法 autoType 被拦', 'autoType' in str(e))
check('autoType 默认是 1（智能笔数）', ApitrxProvider.AUTO_TYPE[1].startswith('智能'))

print()
print('  只有 web 域名有笔数（gift 那个 key 做不了）：')
print('     ', ApitrxProvider.AUTO_TYPE)

print()
print('=' * 64)
print('二、订单生成')
print('=' * 64)
r = ShopRunner(FakeMgr(), {'id': BID, 'token': '0:fake', 'note': '笔数UI',
                           'admin_ids': [111], 'owner_id': 111,
                           'shop': dict(CFG)})
r.api = FakeAPI()
r.data.clear()          # 清掉可能残留的旧数据，免得上一次跑剩的订单干扰
CUST = 222

check('档位价读得对', r.store.auto_price(30) == 85.0)
check('没配的档返回 0', r.store.auto_price(77) == 0.0)

tiers = r.store.auto_tiers()
check('★ 档位从配置来，不写死在代码里', len(tiers) == 4,
      str([c for c, _ in tiers]))
check('★ 支持 300 笔这种大档', 300 in [c for c, _ in tiers],
      str([c for c, _ in tiers]))
check('档位按笔数从小到大排', [c for c, _ in tiers] == [10, 30, 50, 300],
      str([c for c, _ in tiers]))

r.api.calls.clear()
r.flow_auto(CUST)
kb = (r.api.last_kb() or {}).get('inline_keyboard') or []
check('列出全部档位', len(kb) == 4, '%d 个' % len(kb))
check('按钮文案带笔数和价格', '10 笔' in kb[0][0]['text']
      and '300 笔' in kb[3][0]['text'], kb[3][0]['text'])
check('回调数据带档位', kb[0][0]['callback_data'] == 'ac:10')

# 只配一档也行
r.bot['shop']['auto_prices'] = {'300': 750.0}
check('只配一档也正常', r.store.auto_tiers() == [(300, 750.0)],
      str(r.store.auto_tiers()))
r.bot['shop']['auto_prices'] = {'10': 30.0, '30': 85.0,
                                '50': 140.0, '300': 750.0}
# 无效档位会被过滤掉
r.bot['shop']['auto_prices'] = {'1': 5.0, 'abc': 9.0, '0': 1.0, '10': 30.0}
check('无效档位（<2 或非数字）被过滤',
      r.store.auto_tiers() == [(10, 30.0)], str(r.store.auto_tiers()))
r.bot['shop']['auto_prices'] = {'10': 30.0, '30': 85.0,
                                '50': 140.0, '300': 750.0}

r.api.calls.clear()
r.on_callback({'id': 'cb1', 'from': {'id': CUST},
               'message': {'message_id': 9}, 'data': 'ac:10'})
check('选了档位后要地址', '地址' in r.api.last_text(),
      r.api.last_text().replace('\n', ' ')[:40])
check('会话记下了档位', r._sess.get(CUST, {}).get('count') == '10')

r.api.calls.clear()
r.start_auto_order(CUST, 'not-an-address', '10')
check('❌ 地址不合法时不下单', '不是有效' in r.api.last_text())
check('没有生成订单', not r.store.premium_recent(1))

r.api.calls.clear()
r.start_auto_order(CUST, ADDR, '10')
_t = r.api.last_text()
check('生成订单', '订单已生成' in _t, _t.replace('\n', ' ')[:40])
o = r.store.premium_recent(1)[0]
check('订单类型是 auto', o.get('kind') == 'auto', str(o.get('kind')))
check('订单记了地址', o.get('username') == ADDR)
check('订单记了笔数', str(o.get('month')) == '10')
check('★ 笔数是 TRX 计价', (o.get('coin') or '').upper() == 'TRX',
      str(o.get('coin')))
check('★ 金额溢价不超过 0.1',
      30.0 <= float(o['amount']) <= 30.1001,
      '%.6f（标价 30）' % float(o['amount']))
check('提示用登记的地址转', '必须' in _t and '地址' in _t)

print()
print('=' * 64)
print('三、付款 → 买笔数')
print('=' * 64)
r2 = FakeRunner()
st2 = S.ShopStore(r2)
w2 = SP.PayWatcher(r2, st2, tron=object())
o2 = st2.add_premium(333, ADDR, 10, 30.05, coin='TRX',
                     pay_addr=ADDR, kind='auto')

w2.handle_one(tx('A-PAY', 30.05))
check('★ TRX 付笔数订单 → 买成功', o2['status'] == S.PR_DONE, o2['status'])
check('★ 没有误发能量',
      all(int(x.get('units') or 0) == 0 for x in st2.payments),
      str([x.get('units') for x in st2.payments]))
check('记了上游单号', bool(o2.get('provider_oid')),
      str(o2.get('provider_oid')))
check('通知里说的是笔数，不是会员', len(r2.result) == 1
      and r2.result[0].get('kind') == 'auto')

print()
print('=' * 64)
print('四、剩余笔数查询')
print('=' * 64)
prov = MockProvider('')
info = prov.auto_remain(ADDR)
check('查得到笔数信息', info is not None)
check('剩余笔数读得对', info['remain'] == 7, str(info['remain']))
check('累计买过读得对', info['bought'] == 10, str(info['bought']))
check('已用读得对', info['used'] == 3, str(info['used']))

r.api.calls.clear()
r.flow_remain(CUST)
check('点剩余笔数会要地址', '地址' in r.api.last_text())
r.api.calls.clear()
r.show_remain(CUST, ADDR)
_t = r.api.last_text()
check('显示剩余笔数', '剩余' in _t and '7' in _t,
      _t.replace('\n', ' ')[:50])
check('显示累计和已用', '已用' in _t)

r.api.calls.clear()
r.show_remain(CUST, 'bad-address')
check('坏地址被拦', '不是有效' in r.api.last_text())

print()
print('=' * 64)
print('五、菜单开关')
print('=' * 64)
_all = lambda m: [b['text'] for row in m['keyboard'] for b in row]
check('开着的时候有笔数按钮',
      r.BTN_AUTO in _all(r.menu()) and r.BTN_REMAIN in _all(r.menu()))
r.bot['shop']['auto_enabled'] = False
check('关掉之后两个按钮都消失',
      r.BTN_AUTO not in _all(r.menu())
      and r.BTN_REMAIN not in _all(r.menu()))
r.api.calls.clear()
r.flow_auto(CUST)
check('关掉时点按钮给提示', '暂未开放' in r.api.last_text())

shutil.rmtree(_TMP, ignore_errors=True)      # 临时目录整个清掉

print()
print('=' * 64)
print('结果：%d 项通过，%d 项失败' % (len(OK), len(BAD)))
for b in BAD:
    print('  ❌', b)
print('=' * 64)
sys.exit(1 if BAD else 0)
