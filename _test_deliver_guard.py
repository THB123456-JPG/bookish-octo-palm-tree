# -*- coding: utf-8 -*-
"""验证「防重复扣费」保护 + ApiTrx 适配器（不花一分钱）"""
import os
import re
import shutil
import sys
import time

sys.stdout.reconfigure(encoding='utf-8')

import core

# ★ 先把数据目录挪到临时目录 —— 别往**真实**的 data/ 里写假机器人
_HERE = os.path.dirname(os.path.abspath(__file__))
_TMP = os.path.join(_HERE, '_tmp_guard')
shutil.rmtree(_TMP, ignore_errors=True)
os.makedirs(os.path.join(_TMP, 'data'), exist_ok=True)
core.set_base_dir(_TMP)
core.DATA_DIR = os.path.join(_TMP, 'data')

from core import TgError
from runners.shop import store as S
from runners.shop import pay as SP
from runners.shop.providers import (ApitrxProvider, MockProvider, UncertainError,
                            get_provider, provider_list)

OK = []
BAD = []


def check(name, cond, extra=''):
    (OK if cond else BAD).append(name)
    print('  %s %s%s' % ('✅' if cond else '❌', name,
                         ('  → %s' % (extra,)) if extra else ''))


# ================= 假 runner / 假 store =================
class FakeMgr:
    def save(self):
        pass


class FakeRunner:
    def __init__(self, provider, cfg=None):
        self.bot = {'id': 'test', 'shop': dict(cfg or {})}
        self.data = {}
        self.mgr = FakeMgr()
        self._prov = provider
        self.seen, self.done, self.failed, self.skipped = [], [], [], []

    def note(self):
        return '测试'

    def save_data(self):
        pass

    def provider(self):
        return self._prov

    def on_payment_seen(self, p):
        self.seen.append(p)

    def on_payment_done(self, p):
        self.done.append(p)

    def on_payment_failed(self, p, e):
        self.failed.append((p, e))

    def on_payment_skipped(self, p):
        self.skipped.append(p)


class FlakyProvider(MockProvider):
    """可以指定第 N 次调用抛什么异常"""

    def __init__(self, mode):
        super().__init__('')
        self.mode = mode
        self.calls = 0

    def order(self, address, energy, hours=1, trace=''):
        self.calls += 1
        if self.mode == 'uncertain':
            raise UncertainError('模拟超时，上游可能已经扣费')
        if self.mode == 'definite':
            raise TgError('模拟上游明确失败（地址非法）')
        if self.mode == 'lowbal':
            raise TgError('模拟余额不足，请先给上游充值 TRX')
        if self.mode == 'ok':
            return 'FAKE-TXID-001', {'amount': 1.1, 'balance': 5.0}
        if self.mode == 'first_uncertain_then_ok':
            if self.calls == 1:
                raise UncertainError('模拟第一次超时')
            return 'FAKE-TXID-002', {'amount': 1.1, 'balance': 5.0}
        raise AssertionError('不该走到这里')


def mk(mode, cfg=None):
    r = FakeRunner(FlakyProvider(mode), cfg)
    st = S.ShopStore(r)
    w = SP.PayWatcher(r, st, tron=object())
    return r, st, w


BASE_CFG = {'trx_own': 'TBKw17Fxy5kBbev8MMZ16yVKuQTQfsETta',
            'price': 3.5, 'energy': 32000, 'max': 10, 'enabled': True}


print('=' * 64)
print('一、上游适配器本身')
print('=' * 64)
p = ApitrxProvider('你的APIKEY')
check('apitrx 已注册进 PROVIDERS',
      any(x['value'] == 'apitrx' for x in provider_list()))
check('填了 key 才算配置好', p.configured())
check('没填 key 就是没配置', not ApitrxProvider('').configured())

trx65, _ = p.quote(65000, 1)
check('65000/1小时的估算价 = 1.1 TRX', abs(trx65 - 1.1) < 0.001, '%s' % trx65)
trx32, _ = p.quote(32000, 1)
check('32000 按最低档保守估算，不超过 1.1', trx32 <= 1.1 + 0.001, '%s' % trx32)
trx_h, _ = p.quote(65000, 24)
check('24 小时 = 4.8 TRX', abs(trx_h - 4.8) < 0.001, '%s' % trx_h)

for bad_addr in ('', 'abc', '0x123', 'TNDyeJsZyv7my43PK6aXFxmD929qTZnDc'):
    try:
        p.order(bad_addr, 32000, 1)
        check('坏地址被拦住(%r)' % bad_addr, False, '居然没报错')
    except TgError as e:
        check('坏地址被拦住(%r)' % (bad_addr or '空'), '地址' in str(e))

try:
    p.order('TBKw17Fxy5kBbev8MMZ16yVKuQTQfsETta', 1000, 1)
    check('低于 32000 能量被拦住', False, '居然没报错')
except TgError as e:
    check('低于 32000 能量被拦住', '最低' in str(e), str(e)[:40])

print()
print('  真实调一次上游余额接口（只读，不花钱）：')
# ★★ 没有真 Key 就**跳过**，不要判失败。
#    上面那个 Key 是硬编码的真密钥，**传 GitHub 时会被脱敏清掉**
#    （变成占位符）—— 那时候这条真调用必然失败，但**代码是好的**。
#    公开仓库里挂一条红会让人以为有 bug（而且会让「跑测试」变成噪音）。
#    所以：认得出「没配 Key」就明说跳过。
#    ★ 判据要用「**像不像一个真 Key**」，不能用 `p.configured()` ——
#      那个只判「非空」，而脱敏后的占位符 `你的APIKEY` 也是非空，
#      照样会真去调接口、照样挂。
_LOOKS_REAL = bool(re.fullmatch(
    r'[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', p.api_key or ''))
if not p.configured() or not _LOOKS_REAL:
    print('  ⏭ 跳过：没有真实的上游 Key（公开仓库里是占位符）。'
          '想跑这条得自己填一个真的。')
else:
    try:
        bal = p.balance()
        check('能读到上游余额', bal is not None, '%s TRX' % bal)
        ok2, msg = p.self_test()
        print('     自检：%s %s' % ('✅' if ok2 else '❌', msg))
    except Exception as e:
        check('能读到上游余额', False, str(e))

print()
print('=' * 64)
print('二、防重复扣费（关键）')
print('=' * 64)

# --- 场景1：上游结果不明 ---
r, st, w = mk('uncertain', BASE_CFG)
pay = st.add_payment('TXID-A', 'TFromAddrAAA', 3.5, 1, 32000, S.P_PENDING)
w.deliver(pay, 'TFromAddrAAA', 32000)
check('结果不明 → 状态是 pending（不是 failed）',
      pay['status'] == S.P_PENDING, pay['status'])
check('结果不明 → 留下了 dispatched 标记', bool(pay.get('dispatched')))
check('结果不明 → 调用方收到了告警', len(r.failed) == 1)
check('结果不明 → 没有计入已发货', len(r.done) == 0)

before = r._prov.calls
w.deliver(pay, 'TFromAddrAAA', 32000)
check('★ 自动重发被拦住（没有第二次调上游）',
      r._prov.calls == before, '调用次数 %d→%d' % (before, r._prov.calls))

n = w.retry_pending()
check('★ 自动补发也跳过它', n == 0 and r._prov.calls == before,
      '补发 %d 笔，调用次数 %d' % (n, r._prov.calls))

# --- 场景2：人工核对后强制重发，这时候才允许再打上游 ---
r._prov.mode = 'ok'
w.deliver(pay, 'TFromAddrAAA', 32000, manual=True)
check('人工确认后能重发成功', pay['status'] == S.P_DONE, pay['status'])
check('成功后清掉了 dispatched 标记', not pay.get('dispatched'))
check('成功后记了账（地址档案 +1 笔）',
      st.addrs.get('TFromAddrAAA', {}).get('count') == 1)

# --- 场景3：已经发货的，再点重发也不会重复扣钱 ---
before = r._prov.calls
w.deliver(pay, 'TFromAddrAAA', 32000, manual=True)
check('★ 已发货的流水不会重复扣费',
      r._prov.calls == before, '调用次数 %d' % r._prov.calls)

# --- 场景4：上游明确失败 → 可以安全重发 ---
r, st, w = mk('definite', BASE_CFG)
pay = st.add_payment('TXID-B', 'TFromAddrBBB', 3.5, 1, 32000, S.P_PENDING)
w.deliver(pay, 'TFromAddrBBB', 32000)
check('明确失败 → 状态是 failed', pay['status'] == S.P_FAILED, pay['status'])
check('明确失败 → 没有 dispatched 标记（钱没扣，可重发）',
      not pay.get('dispatched'))
# 第一次失败已经用掉 tries=1，第二档退避是 300 秒，把时间往前拨够
pay['at'] = int(time.time()) - 400
r._prov.mode = 'ok'
n = w.retry_pending()
check('★ 明确失败的可以自动补发（充了值就能自动补单）',
      n == 1 and pay['status'] == S.P_DONE,
      '补发 %d 笔，状态 %s' % (n, pay['status']))

# --- 场景4b：试够 3 次就不再自动打上游 ---
r, st, w = mk('definite', BASE_CFG)
pay = st.add_payment('TXID-D', 'TFromAddrDDD', 3.5, 1, 32000, S.P_PENDING)
for i in range(4):
    pay['at'] = int(time.time()) - 4000
    w.deliver(pay, 'TFromAddrDDD', 32000)
c_before = r._prov.calls
pay['at'] = int(time.time()) - 4000
n = w.retry_pending()
check('★ 重试 3 次后不再自动打上游（避免反复骚扰）',
      n == 0 and r._prov.calls == c_before,
      'tries=%s 补发 %d 笔' % (pay.get('tries'), n))

# --- 场景4c：余额不足是「可恢复」失败，不能被 3 次上限卡死 ---
r, st, w = mk('lowbal', BASE_CFG)
pay = st.add_payment('TXID-E', 'TFromAddrEEE', 3.5, 1, 65000, S.P_PENDING)
for i in range(5):                      # 连续失败 5 次，早超过普通上限了
    pay['at'] = int(time.time()) - 4000
    w.deliver(pay, 'TFromAddrEEE', 65000)
check('余额不足的失败被识别成可恢复', '余额不足' in (pay.get('note') or ''),
      (pay.get('note') or '')[:34])
before = r._prov.calls
pay['at'] = int(time.time()) - 4000
n = w.retry_pending()
check('★ 余额不足不会被 3 次上限卡死（充了值还能自动补发）',
      n == 1 and r._prov.calls == before + 1,
      'tries=%s 补发 %d 笔' % (pay.get('tries'), n))

# --- 场景5：第一次不明、第二次成功（模拟「其实上游发了但我们没收到」）---
r, st, w = mk('first_uncertain_then_ok', BASE_CFG)
pay = st.add_payment('TXID-C', 'TFromAddrCCC', 7.0, 2, 64000, S.P_PENDING)
w.deliver(pay, 'TFromAddrCCC', 64000)
c1 = r._prov.calls
n = w.retry_pending()
check('★ 不确定的单子不会被自动重发（否则就是双倍成本）',
      r._prov.calls == c1 and n == 0)

print()
print('=' * 64)
print('三、倍数换算边界')
print('=' * 64)
r, st, w = mk('ok', BASE_CFG)
check('3.5 TRX → 1 个单位 / 32000 能量', st.calc(3.5)[:2] == (1, 32000),
      '%s' % (st.calc(3.5),))
check('7 TRX → 2 个单位 / 64000 能量', st.calc(7)[:2] == (2, 64000),
      '%s' % (st.calc(7),))
check('0.000001 TRX → 不发', st.calc(0.000001)[0] == 0, st.calc(0.000001)[2])
check('0 TRX → 不发', st.calc(0)[0] == 0, st.calc(0)[2])
u, e, note = st.calc(350.0)          # 100 个单位，超过 max=10
check('超过最高倍数被截断到 10 个单位', u == 10,
      'units=%s note=%s' % (u, note[:30]))
check('截断时给出提示', bool(note))
check('6.9 TRX 只算 1 个单位（不四舍五入）', st.calc(6.9)[0] == 1,
      '%s' % (st.calc(6.9),))

print()
shutil.rmtree(_TMP, ignore_errors=True)      # 临时目录整个清掉
print('=' * 64)
print('结果：%d 项通过，%d 项失败' % (len(OK), len(BAD)))
if BAD:
    print('失败项：')
    for b in BAD:
        print('  ❌', b)
print('=' * 64)
sys.exit(1 if BAD else 0)
