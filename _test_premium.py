# -*- coding: utf-8 -*-
"""商城会员功能：金额分配、收款识别顺序、开通、防重复扣费"""
import os
import sys
import time

sys.stdout.reconfigure(encoding='utf-8')

import os
import shutil

import core

# ★ 必须先把数据目录挪到临时目录 —— 否则测试会往**真实**的 data/ 里写
#   假机器人（固定 id `_premtest`）。崩一次就留下脏文件，下一次跑
#   会读到上次的订单，断言莫名其妙地挂掉（踩过）。
_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = os.path.join(_HERE, '_tmp_prem')
shutil.rmtree(_TMP, ignore_errors=True)
os.makedirs(os.path.join(_TMP, 'data'), exist_ok=True)
core.BASE_DIR = _TMP
core.DATA_DIR = os.path.join(_TMP, 'data')

from core import TgError
from runners.shop import store as S
from runners.shop import pay as SP
import runners.shop.runner as SH
from runners.shop import ShopRunner
from runners.shop.providers import (ApitrxProvider, MockProvider, UncertainError,
                            norm_username)
from tron import fmt_amount

OK, BAD = [], []
BID = '_premtest'
CFG = {'enabled': True, 'trx_own': 'TBKw17Fxy5kBbev8MMZ16yVKuQTQfsETta',
       'price': 3.5, 'energy': 65000, 'max': 10, 'provider': 'mock',
       'premium_enabled': True,
       'premium_prices': {'3': 55.0, '6': 75.0, '12': 135.0}}


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

    def texts(self):
        return [c[1].get('text', '') for c in self.calls]


class PProvider(MockProvider):
    """可控：第 N 次开通抛什么异常"""

    def __init__(self, mode='ok'):
        super().__init__('')
        self.mode = mode
        self.opens = 0

    def premium_open(self, username, month, callback_url=''):
        self.opens += 1
        if self.mode == 'uncertain':
            raise UncertainError('模拟超时，上游可能已经开通了')
        if self.mode == 'definite':
            # 必须是 TgError 才算「上游明确说没成」；
            # 普通 Exception 会被当成「结果不明」保守处理
            raise TgError('模拟上游明确拒绝')
        return 'MOCKP-%d' % self.opens, {'orderId': 'MOCKP-%d' % self.opens,
                                         'status': 1, 'amount': 40.0,
                                         'remark': '开通成功'}


class FakeRunner:
    def __init__(self, prov, cfg=None):
        self.bot = {'id': BID, 'token': '0:fake', 'note': '会员测试',
                    'admin_ids': [111], 'owner_id': 111,
                    'shop': dict(cfg or CFG)}
        self.data = {}
        self.mgr = FakeMgr()
        self._prov = prov
        self.paid, self.result, self.failed = [], [], []

    def note(self):
        return '会员测试'

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
        self.paid.append(o)

    def on_premium_result(self, o):
        self.result.append(o)


def mk(mode='ok', cfg=None):
    r = FakeRunner(PProvider(mode), cfg)
    st = S.ShopStore(r)
    w = SP.PayWatcher(r, st, tron=object())
    return r, st, w


def tx(txid, amount, frm='TFromAAAA', coin='TRX'):
    return {'txid': txid, 'from': frm, 'amount': amount, 'coin': coin,
            'ts': int(time.time() * 1000)}


print('=' * 64)
print('〇、配置合并（嵌套字典不能被污染）')
print('=' * 64)
_m = S.merge_default({'premium_prices': {'3': 15.5}})
check('★ 嵌套的 premium_prices 只含套餐键',
      set(_m['premium_prices'].keys()) == {'3', '6', '12'},
      str(sorted(_m['premium_prices'].keys())))
check('缺的档位补上默认值', _m['premium_prices']['6'] == 20.5,
      str(_m['premium_prices']['6']))
check('已有的值不被覆盖', _m['premium_prices']['3'] == 15.5)
check('顶层该有的还是有', _m['price'] == 3.5 and 'provider' in _m)
check('顶层额外字段保留', S.merge_default({'abc': 1})['abc'] == 1)

print()
print('=' * 64)
print('一、用户名归一化')
print('=' * 64)
for raw, want in (('@pms66', 'pms66'), ('pms66', 'pms66'),
                  ('https://t.me/pms66', 'pms66'), ('t.me/pms66', 'pms66'),
                  ('  @Pms66/', 'Pms66')):
    check('%-22r → %s' % (raw, want), norm_username(raw) == want,
          norm_username(raw))

print()
print('=' * 64)
print('二、订单金额分配')
print('=' * 64)
r, st, w = mk()
amts = set()
bad = []
for i in range(50):
    a = st.alloc_premium_amount(15.0)
    amts.add(a)
    if not (15.0 <= a <= 15.100001):
        bad.append(a)
check('★ 溢价不超过 0.1（用户明确要求，不能出现 15.79 那种）',
      not bad, '跑出 %s' % bad[:3])
check('零头能用来区分同价订单', len(amts) >= 45, '%d 个不同值' % len(amts))
check('标价 15 的订单金额落在 15.00~15.10',
      all(15.0 <= x <= 15.100001 for x in amts),
      '范围 %.4f~%.4f' % (min(amts), max(amts)))

# 已挂着的金额不会被重复分配
r2, st2, _ = mk()
o1 = st2.add_premium(1, 'aaa', 3, 15.05, coin='USDT')
got = {st2.alloc_premium_amount(15.0) for _ in range(50)}
check('不会分配出已被占用的金额', 15.05 not in got)

o = st.add_premium(9001, 'pms66', 3, 55.123456, coin='TRX')
check('订单号从 P1001 开始', o['oid'] == 'P1001', o['oid'])
check('用户名去掉了 @', st.add_premium(9001, '@abcde', 3, 55.2)['username']
      == 'abcde')
check('能按订单号取回', st.premium_by_oid('P1001') is o)
check('套餐价读得对', st.premium_price(6) == 75.0)
check('没配的档位返回 0', st.premium_price(9) == 0.0)

print()
print('=' * 64)
print('三、收款识别顺序（最关键）')
print('=' * 64)
r, st, w = mk()
o = st.add_premium(9001, 'pms66', 3, 55.123456, coin='TRX')
exact = o['amount']
print('  待付款订单 %s，精确金额 %s TRX' % (o['oid'], exact))

# ① 精确金额 → 走会员
r, st, w = mk()
o = st.add_premium(9001, 'pms66', 3, 55.123456, coin='TRX')
w.handle_one(tx('TX-EXACT', o['amount']))
check('★ 精确金额 → 当成会员付款', o['status'] == S.PR_DONE, o['status'])
check('★ 没有发能量（units 是 0）',
      all(int(p.get('units') or 0) == 0 for p in st.payments),
      str([p.get('units') for p in st.payments]))
check('记了上游单号', bool(o.get('provider_oid')), str(o.get('provider_oid')))

# ② 接近但不等 → 停下来等人工，绝不发能量
r, st, w = mk()
o = st.add_premium(9001, 'pms66', 3, 55.123456, coin='TRX')
w.handle_one(tx('TX-NEAR', 55.0))       # 少了 0.12，像交易所扣费
check('★ 金额贴近会员订单 → 不发能量', o['status'] == S.PR_CREATED, o['status'])
check('★ 这笔被挂成待处理',
      st.payments and st.payments[-1]['status'] == S.P_PENDING,
      st.payments[-1]['status'] if st.payments else '无')
check('流水关联到了那个会员订单',
      st.payments[-1].get('poid') == o['oid'])
check('运营者收到了提醒', len(r.failed) == 1)

# ③ 正常买能量 → 走能量
r, st, w = mk()
st.add_premium(9001, 'pms66', 3, 55.123456, coin='TRX')
w.handle_one(tx('TX-ENERGY', 7.0))      # 7 TRX = 2 个单位
check('★ 普通金额 → 正常发能量',
      st.payments[-1]['units'] == 2 and st.payments[-1]['status'] == S.P_DONE,
      'units=%s status=%s' % (st.payments[-1]['units'],
                              st.payments[-1]['status']))

# ④ 没有会员订单时，任何金额都走能量
r, st, w = mk()
w.handle_one(tx('TX-NOPREM', 55.0))
check('没有待付款会员单时，55 TRX 照样发能量',
      st.payments[-1]['units'] == 10 and st.payments[-1]['status'] == S.P_DONE,
      'units=%s（55/3.5=15，被 max=10 截断）' % st.payments[-1]['units'])

print()
print('=' * 64)
print('三B、币种隔离（USDT 买会员 / TRX 买能量，互不干扰）')
print('=' * 64)
# USDT 精确金额 → 开会员
r, st, w = mk()
o = st.add_premium(9001, 'pms66', 3, 15.123456, coin='USDT')
w.handle_one(tx('U-EXACT', o['amount'], coin='USDT'))
check('★ USDT 精确金额 → 开会员', o['status'] == S.PR_DONE, o['status'])
check('★ 没有发能量', all(int(p.get('units') or 0) == 0 for p in st.payments),
      str([p.get('units') for p in st.payments]))

# ★ 同样金额、但客户转的是 TRX → 绝不能被当成会员付款
r, st, w = mk()
o = st.add_premium(9001, 'pms66', 3, 15.123456, coin='USDT')
w.handle_one(tx('T-SAME', o['amount'], coin='TRX'))
check('★ 金额一样但转的是 TRX → 不认成会员付款',
      o['status'] == S.PR_CREATED, o['status'])
check('★ 而是按能量处理（发给转账地址）',
      st.payments[-1]['units'] > 0 and st.payments[-1]['status'] == S.P_DONE,
      'units=%s' % st.payments[-1]['units'])

# USDT 没对上任何订单 → 挂人工，绝不发能量
r, st, w = mk()
w.handle_one(tx('U-NOMATCH', 99.0, coin='USDT'))
check('★ 收到 USDT 但没有对应订单 → 挂待处理，不发能量',
      st.payments[-1]['status'] == S.P_PENDING
      and int(st.payments[-1]['units'] or 0) == 0,
      st.payments[-1]['status'])
check('提示了原因', 'USDT' in st.payments[-1]['note'],
      st.payments[-1]['note'][:40])
check('运营者收到提醒', len(r.failed) == 1)

# USDT 金额贴近订单（被扣手续费）→ 挂人工
r, st, w = mk()
o = st.add_premium(9001, 'pms66', 3, 15.5, coin='USDT')
w.handle_one(tx('U-NEAR', 15.3, coin='USDT'))
check('★ USDT 金额贴近但不等 → 挂人工', o['status'] == S.PR_CREATED
      and st.payments[-1]['status'] == S.P_PENDING)

print()
print('=' * 64)
print('三C、同价订单靠「登记地址」分辨（这才是去掉大溢价的本钱）')
print('=' * 64)
ADDR_A = 'TUaZ5nGofnSFpoYB5DSfqpZBZAAbmuWzcF'
ADDR_B = 'TAwRPqxJGad4cZruAUm1iGTcWfVDQoVyR8'   # 别用收款地址，那会被当自己转自己
ADDR_C = 'TGrT3i3nkGR3PYnHhbnju9tRbNHPLkRgdx'

r, st, w = mk()
oA = st.add_premium(111, 'usera', 3, 15.0, coin='USDT', pay_addr=ADDR_A)
oB = st.add_premium(222, 'userb', 3, 15.0, coin='USDT', pay_addr=ADDR_B)
check('两单同价挂着', oA['amount'] == oB['amount'] == 15.0)

w.handle_one(tx('U-A', 15.0, frm=ADDR_A, coin='USDT'))
check('★ 从 A 的地址转 → 认 A 的单', oA['status'] == S.PR_DONE
      and oB['status'] == S.PR_CREATED,
      'A=%s B=%s' % (oA['status'], oB['status']))

w.handle_one(tx('U-B', 15.0, frm=ADDR_B, coin='USDT'))
check('★ 从 B 的地址转 → 认 B 的单', oB['status'] == S.PR_DONE,
      oB['status'])

# 陌生地址转同价 → 两单都已处理完，认不出来就挂人工
r, st, w = mk()
oA = st.add_premium(111, 'usera', 3, 15.0, coin='USDT', pay_addr=ADDR_A)
oB = st.add_premium(222, 'userb', 3, 15.0, coin='USDT', pay_addr=ADDR_B)
w.handle_one(tx('U-C', 15.0, frm=ADDR_C, coin='USDT'))
check('★ 陌生地址 + 多单同价 → 不瞎认，挂人工',
      oA['status'] == S.PR_CREATED and oB['status'] == S.PR_CREATED
      and st.payments[-1]['status'] == S.P_PENDING,
      'A=%s B=%s 流水=%s' % (oA['status'], oB['status'],
                             st.payments[-1]['status']))

# 只有一单时，地址对不上也认（客户换了钱包也不至于卡死）
r, st, w = mk()
oA = st.add_premium(111, 'usera', 3, 15.0, coin='USDT', pay_addr=ADDR_A)
w.handle_one(tx('U-D', 15.0, frm=ADDR_C, coin='USDT'))
check('只有一单时，换个钱包转也认（不至于卡住客户）',
      oA['status'] == S.PR_DONE, oA['status'])

print()
print('=' * 64)
print('四、会员开通的防重复扣费')
print('=' * 64)
r, st, w = mk('uncertain')
o = st.add_premium(9001, 'pms66', 6, 75.2, coin='TRX')
w.handle_one(tx('TX-UNC', o['amount']))
check('结果不明 → 订单不标记成功', o['status'] == S.PR_PAID, o['status'])
check('结果不明 → 留下 dispatched 标记', bool(o.get('dispatched')))
before = r._prov.opens
r.pay_guard = None
n = w.retry_premiums()
check('★ 不会自动重开（否则重复扣费 + 多开一个月）',
      r._prov.opens == before and n == 0,
      '开通调用 %d→%d 次' % (before, r._prov.opens))

# 人工确认后强制重开
r._prov.mode = 'ok'
p = st.by_txid('TX-UNC')
w.deliver_premium(p, o, manual=True)
check('人工确认后能重开成功', o['status'] == S.PR_DONE, o['status'])
check('成功后清掉 dispatched', not o.get('dispatched'))
check('记下了上游实际扣费', o.get('cost') == 40.0, str(o.get('cost')))

before = r._prov.opens
w.deliver_premium(p, o, manual=True)
check('★ 已开通的再点也不会重复开', r._prov.opens == before,
      '开通调用 %d 次' % r._prov.opens)

print()
print('=' * 64)
print('五、明确失败可以补开 · 超时作废')
print('=' * 64)
r, st, w = mk('definite')
o = st.add_premium(9001, 'pms66', 12, 135.5, coin='TRX')
w.handle_one(tx('TX-DEF', o['amount']))
check('上游明确失败 → 状态是 failed', o['status'] == S.PR_FAILED, o['status'])
check('明确失败 → 没有 dispatched（钱没扣，可补开）',
      not o.get('dispatched'))
o['paid_at'] = int(time.time() * 1000) - 400000
r._prov.mode = 'ok'
n = w.retry_premiums()
check('★ 明确失败的能自动补开', n == 1 and o['status'] == S.PR_DONE,
      '补开 %d 单，状态 %s' % (n, o['status']))

r, st, w = mk()
st.add_premium(9001, 'pms66', 3, 55.9)
st.add_premium(9001, 'abcde', 6, 75.9)
st.premiums[0]['created'] = int(time.time()) - S.PREMIUM_EXPIRE_MIN * 60 - 60
n = st.premium_expire_stale()
check('超时没付款的订单被作废', n == 1
      and st.premiums[0]['status'] == S.PR_EXPIRED, '作废 %d 单' % n)
check('★ 作废后金额释放，不再参与匹配',
      st.premium_match_amount(55.9) is None)
check('没过期的还留着', st.premiums[1]['status'] == S.PR_CREATED)

print()
print('=' * 64)
print('六、机器人交互')
print('=' * 64)
r = ShopRunner(FakeMgr(), {'id': BID, 'token': '0:fake', 'note': '会员UI',
                           'admin_ids': [111], 'owner_id': 111,
                           'shop': dict(CFG)})
r.api = FakeAPI()
CUST = 222

r.api.calls.clear()
r.flow_premium(CUST)
kb = r.api.last_kb() or {}
rows = kb.get('inline_keyboard') or []
check('会员菜单列出了套餐按钮', len(rows) == 3, '%d 个按钮' % len(rows))
check('按钮文案带价格', '55' in rows[0][0]['text'], rows[0][0]['text'])
check('按钮回调带月份', rows[0][0]['callback_data'] == 'pm:3')

r.api.calls.clear()
r.on_callback({'id': 'cb1', 'from': {'id': CUST},
               'message': {'message_id': 9}, 'data': 'pm:3'})
check('选了套餐之后要问用户名', '用户名' in r.api.last_text(),
      r.api.last_text().replace('\n', ' ')[:40])
check('会话记下了月份', r._sess.get(CUST, {}).get('month') == 3)

r.api.calls.clear()
r.handle_session(CUST, '@pms66', {'id': CUST})
check('输入用户名后接着问付款地址', '地址' in r.api.last_text(),
      r.api.last_text().replace('\n', ' ')[:40])

r.api.calls.clear()
r.start_premium_order(CUST, 'pms66', 3, 'not-a-tron-address')
check('❌ 地址不合法时被拦住，不生成订单',
      '不是有效' in r.api.last_text() and not r.store.premium_recent(1),
      r.api.last_text()[:30])

# 重新走一遍完整流程：套餐 → 用户名 → 地址
r.api.calls.clear()
r.flow_premium(CUST)
r.on_callback({'id': 'cb2', 'from': {'id': CUST},
               'message': {'message_id': 9}, 'data': 'pm:3'})
r.handle_session(CUST, '@pms66', {'id': CUST})
r.api.calls.clear()
r.handle_session(CUST, 'TUaZ5nGofnSFpoYB5DSfqpZBZAAbmuWzcF', {'id': CUST})
_t = r.api.last_text()
check('填了合法地址才生成订单', '订单已生成' in _t,
      _t.replace('\n', ' ')[:40])
_o2 = r.store.premium_recent(1)[0]
check('订单记下了客户登记的付款地址',
      _o2.get('pay_addr') == 'TUaZ5nGofnSFpoYB5DSfqpZBZAAbmuWzcF',
      str(_o2.get('pay_addr'))[:20])
check('订单里显示金额', fmt_amount(_o2['amount']) in _t)
check('★ 订单金额溢价不超过 0.1',
      55.0 <= float(_o2['amount']) <= 55.1001,
      '%.6f（标价 55）' % float(_o2['amount']))
check('提醒必须用登记的地址转', '必须用你登记的地址' in _t)
check('提示金额一分不差', '一分不差' in _t)

r.api.calls.clear()
r.start_premium_order(CUST, 'ab', 3, 'TUaZ5nGofnSFpoYB5DSfqpZBZAAbmuWzcF')
check('太短的用户名被拦住', '用户名看着不对' in r.api.last_text(),
      r.api.last_text()[:30])

r.api.calls.clear()
r.flow_premium(CUST)
r.bot['shop']['premium_enabled'] = False
r.api.calls.clear()
r.flow_premium(CUST)
check('会员没开时提示未开放', '未开放' in r.api.last_text()
      or '暂未开放' in r.api.last_text(), r.api.last_text()[:30])

r.bot['shop']['premium_enabled'] = True
r.api.calls.clear()
r.on_button(CUST, r.BTN_PREM)
check('点「开通会员」按钮能进流程', bool(r.api.calls))
check('菜单里有会员按钮（开着的时候）',
      any(r.BTN_PREM == b['text']
          for row in r.menu()['keyboard'] for b in row))

r.bot['shop']['premium_enabled'] = False
check('会员关掉后菜单里不显示该按钮',
      not any(r.BTN_PREM == b['text']
              for row in r.menu()['keyboard'] for b in row))

print()
print('=' * 64)
print('七、USDT 计价模式（gift 域名 + 独立 key）')
print('=' * 64)
_orig_u2t = SH.usdt_to_trx
SH.usdt_to_trx = lambda u: round(float(u) / 0.32, 6)   # 钉死汇率，测试不联网

r = ShopRunner(FakeMgr(), {
    'id': BID, 'token': '0:fake', 'note': 'USDT模式',
    'admin_ids': [111], 'owner_id': 111,
    'shop': dict(CFG, premium_key='fake-gift-key',
                 premium_prices={'3': 15.0, '6': 20.0, '12': 36.0})})
r.api = FakeAPI()

usdt, pay, coin = r.premium_quote(3)
check('USDT 模式：标价 = 应付金额', usdt == 15.0 and pay == 15.0,
      '%s / %s' % (usdt, pay))
check('★ USDT 模式：客户也付 USDT（不用换汇）', coin == 'USDT', coin)
check('会员 key 传到了上游对象',
      r.provider().premium_key == 'fake-gift-key')
check('会员计价方式认成 usdt', r.provider().premium_kind() == 'usdt')

r.api.calls.clear()
r.flow_premium(CUST)
_kb = (r.api.last_kb() or {}).get('inline_keyboard') or []
check('按钮显示 USDT', '15 USDT' in _kb[0][0]['text'], _kb[0][0]['text'])
check('菜单里没有 TRX 换算的干扰', '约' not in r.api.last_text())

r2 = ShopRunner(FakeMgr(), {
    'id': BID, 'token': '0:fake', 'note': 'TRX模式',
    'admin_ids': [111], 'owner_id': 111, 'shop': dict(CFG)})
u2, p2, c2 = r2.premium_quote(3)
check('TRX 模式：标价就是 TRX、币种 TRX',
      u2 == 0.0 and p2 == 55.0 and c2 == 'TRX',
      'usdt=%s pay=%s coin=%s' % (u2, p2, c2))
check('TRX 模式认成 trx', r2.provider().premium_kind() == 'trx')

print()
print('  订单里要写清转什么币：')
r.api.calls.clear()
_o = r.store.add_premium(CUST, 'pms66', 3, 15.5, usdt=15.5, coin='USDT')
r.show_premium_order(CUST, _o, {'nickname': '测试'})
_t = r.api.last_text()
check('订单写了 USDT', 'USDT' in _t and '15.5' in _t)
check('★ 明确提醒必须选 TRC20 链（选错链钱会丢）',
      'TRC20' in _t, _t.replace('\n', ' ')[:60])

SH.usdt_to_trx = _orig_u2t

print()
print('  会员请求打去哪个域名 / 用哪个 key：')


class SpyProv(ApitrxProvider):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.seen = []

    def _call(self, path, params, method='GET', base=None, key=None,
              readonly=False):
        # readonly 是「可以放心重试的只读接口」标记 —— 假上游不重试，记下来就行
        self.seen.append((path, method, base, key, readonly))
        if path == '/getenergy':
            return {'txid': 'FAKE-TX', 'amount': 1.56, 'balance': 9.0}
        return {'username': 'abcde', 'nickname': '', 'photo': ''}


sp = SpyProv('webkey', 'giftkey')
sp.premium_query('@abcde')
check('填了会员 key → 走 gift 域名', sp.seen[-1][2] == sp.GIFT,
      str(sp.seen[-1][2]))
check('填了会员 key → 用会员 key', sp.seen[-1][3] == 'giftkey')
check('会员接口用 POST', sp.seen[-1][1] == 'POST')

sp2 = SpyProv('webkey')
sp2.premium_query('@abcde')
check('没填会员 key → 退回默认域名（web）', sp2.seen[-1][2] is None)
check('没填会员 key → 用能量那个 key',
      sp2.seen[-1][3] is None)

sp3 = SpyProv('webkey', 'giftkey')
sp3.order('TBKw17Fxy5kBbev8MMZ16yVKuQTQfsETta', 65000, 1)
check('★ 能量接口始终走 web 域名（不受会员 key 影响）',
      sp3.seen[-1][2] is None and sp3.seen[-1][1] == 'GET',
      'base=%s method=%s' % (sp3.seen[-1][2], sp3.seen[-1][1]))

shutil.rmtree(_TMP, ignore_errors=True)      # 临时目录整个清掉（连 .json 带库）

print()
print('=' * 64)
print('结果：%d 项通过，%d 项失败' % (len(OK), len(BAD)))
for b in BAD:
    print('  ❌', b)
print('=' * 64)
sys.exit(1 if BAD else 0)
