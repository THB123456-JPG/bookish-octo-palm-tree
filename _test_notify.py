# -*- coding: utf-8 -*-
"""验证：① 失败提示不再刷屏 ② 余额不够时不白试（也不刷日志）
   另外确认：失败告警只发给运营者，客户收不到
"""
import os
import sys
import time

sys.stdout.reconfigure(encoding='utf-8')

import os
import shutil

import core

# ★ 先把数据目录挪到临时目录 —— 别往**真实**的 data/ 里写假机器人
_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = os.path.join(_HERE, '_tmp_notify')
shutil.rmtree(_TMP, ignore_errors=True)
os.makedirs(os.path.join(_TMP, 'data'), exist_ok=True)
core.BASE_DIR = _TMP
core.DATA_DIR = os.path.join(_TMP, 'data')

from runners.shop import store as S
from runners.shop import pay as SP
from runners.shop import ShopRunner
from runners.shop.providers import MockProvider

OK, BAD = [], []
OWNER = 111
CUSTOMER = 222


def check(name, cond, extra=''):
    (OK if cond else BAD).append(name)
    print('  %s %s%s' % ('✅' if cond else '❌', name,
                         ('  → %s' % (extra,)) if extra else ''))


class FakeAPI:
    def __init__(self):
        self.calls = []

    def call(self, method, **p):
        self.calls.append((method, p))
        return {'ok': True, 'result': {'message_id': 1}}

    def texts(self):
        return [c[1].get('text', '') for c in self.calls]

    def sent_to(self):
        return [c[1].get('chat_id') for c in self.calls]


class FakeMgr:
    def save(self):
        pass

    def is_expired(self, b):
        return False


BID = '_notifytest'
BOT = {'id': BID, 'token': '0:fake', 'note': '通知测试',
       'admin_ids': [OWNER], 'owner_id': OWNER,
       'shop': {'enabled': True, 'trx_own': 'TBKw17Fxy5kBbev8MMZ16yVKuQTQfsETta',
                'price': 3.5, 'energy': 65000, 'max': 10,
                'group_ids': '999888', 'provider': 'mock'}}

r = ShopRunner(FakeMgr(), BOT)
r.api = FakeAPI()
st = r.store

print('=' * 62)
print('① 同一笔失败，不再每 5 分钟刷一次')
print('=' * 62)
p = st.add_payment('TXID-N1', 'TFromAAAA', 3.5, 1, 65000, S.P_PENDING)
r.api.calls.clear()
r.on_payment_failed(p, '上游余额不足')
first = len(r.api.calls)
check('第一次失败：发了告警', first > 0, '%d 条消息' % first)

r.api.calls.clear()
for i in range(5):                      # 模拟自动补发连续失败 5 次
    r.on_payment_failed(p, '上游余额不足')
check('★ 紧接着再失败 5 次：一条都不发（防刷屏）',
      len(r.api.calls) == 0, '%d 条消息' % len(r.api.calls))

# 把上次提醒时间往前拨过一个周期，应该又能提醒了
p['last_notice'] = int(time.time()) - r.FAIL_NOTICE_GAP - 10
r.api.calls.clear()
r.on_payment_failed(p, '上游余额不足')
check('过了 30 分钟：会再提醒一次', len(r.api.calls) > 0,
      '%d 条消息' % len(r.api.calls))

print()
print('  告警都发给了谁：')
owners = [x for x in r.api.sent_to() if x in (OWNER, 999888)]
others = [x for x in r.api.sent_to() if x not in (OWNER, 999888)]
check('★ 只发给绑定者私聊和通知群', len(others) == 0,
      '收件人 %s' % set(r.api.sent_to()))
check('客户（%d）一条都收不到' % CUSTOMER, CUSTOMER not in r.api.sent_to())

print()
print('=' * 62)
print('② 余额不够时不去白试上游')
print('=' * 62)


class TollProvider(MockProvider):
    """余额可控 + 记录被调用了几次"""

    def __init__(self, bal):
        super().__init__('')
        self.bal = bal
        self.orders = 0

    def balance(self):
        return self.bal

    def order(self, address, energy, hours=1, trace=''):
        self.orders += 1
        return 'TOLL-%d' % self.orders, {'amount': 1.56, 'balance': self.bal}


class FakeRunner:
    def __init__(self, prov):
        self.bot = {'id': BID, 'shop': BOT['shop']}
        self.data = {}
        self.mgr = FakeMgr()
        self._prov = prov
        self.failed = []

    def note(self):
        return '余额门控'

    def save_data(self):
        pass

    def provider(self):
        return self._prov

    def on_payment_seen(self, p):
        pass

    def on_payment_done(self, p):
        pass

    def on_payment_failed(self, p, e):
        self.failed.append(e)

    def on_payment_skipped(self, p):
        pass


prov = TollProvider(0.5)                # 余额 0.5，不够一单
fr = FakeRunner(prov)
s2 = S.ShopStore(fr)
w = SP.PayWatcher(fr, s2, tron=object())

pay = s2.add_payment('TXID-N2', 'TFromBBBB', 3.5, 1, 65000,
                     S.P_FAILED, '发货失败：余额不足，请先给上游充值 TRX')
pay['at'] = int(time.time()) - 4000
n = w.retry_pending()
check('★ 余额不够：一笔都不试，也不打上游', n == 0 and prov.orders == 0,
      '补发 %d 笔，调用上游 %d 次' % (n, prov.orders))

prov.bal = 10.0                          # 充了值
pay['at'] = int(time.time()) - 4000
n = w.retry_pending()
check('★ 充了值：自动补发成功', n == 1 and pay['status'] == S.P_DONE,
      '补发 %d 笔，状态 %s' % (n, pay['status']))
check('补发成功后记下了上游实际扣费', pay.get('cost') == 1.56,
      str(pay.get('cost')))

print()
print('=' * 62)
print('③ 实测成本优先于官方报价')
print('=' * 62)
s2.add_payment('TXID-N3', 'TFromCCCC', 3.5, 1, 65000, S.P_DONE,
               '上游单号 x')
(s2.payments[-1])['cost'] = 2.5          # 假装上游实际扣了 2.5
check('有实测记录时用实测值', abs(w._unit_cost() - 2.5) < 0.001,
      '估成 %s TRX' % w._unit_cost())
check('上游报价(Mock)是 1.95，实测更高就该用实测',
      w._unit_cost() > 1.95, '%s > 1.95' % w._unit_cost())

# 清理
shutil.rmtree(_TMP, ignore_errors=True)

print()
print('=' * 62)
print('结果：%d 项通过，%d 项失败' % (len(OK), len(BAD)))
for b in BAD:
    print('  ❌', b)
print('=' * 62)
sys.exit(1 if BAD else 0)
