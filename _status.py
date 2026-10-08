# -*- coding: utf-8 -*-
"""一眼看清：钱到没到、能量发没发、上游还剩多少钱"""
import sys
import time

import requests

sys.stdout.reconfigure(encoding='utf-8')

import core
from tron import Tron, fmt_amount, fmt_time
from runners.shop.providers import get_provider

ADDR = 'TBKw17Fxy5kBbev8MMZ16yVKuQTQfsETta'      # 收款地址
BUYER = 'TUaZ5nGofnSFpoYB5DSfqpZBZAAbmuWzcF'     # 客户的转账地址（= 收能量的地址）
BID = 'accd0cdc'

print('=' * 64)
print('时间：', time.strftime('%Y-%m-%d %H:%M:%S'))
print('=' * 64)

# ---------- ① 上游余额 ----------
print()
print('【① 上游账户余额】')
try:
    bots = core.load_json(core.BOTS_FILE, {})
    bots = bots.get('bots') if isinstance(bots, dict) else bots
    bot = [b for b in (bots or []) if b.get('id') == BID][0]
    shop = bot.get('shop') or {}
    prov = get_provider(shop.get('provider'), shop.get('provider_key'))
    bal = prov.balance()
    cost = prov.quote(int(shop.get('energy') or 0), 1)[0]
    print('  %s' % prov.label)
    print('  余额 %s TRX （每单估算成本 %s TRX）'
          % (fmt_amount(bal), fmt_amount(cost)))
    if bal is not None and cost > 0:
        print('  → 大概还能发 %d 单' % int(bal / cost))
except Exception as e:
    print('  查不到：%s' % e)

# ---------- ② 链上：收到钱没有 ----------
print()
print('【② 收款地址最近收到什么】')
try:
    txs = Tron().trx_transfers(ADDR, limit=6)
    for x in txs:
        age = time.time() - int(x['ts']) / 1000.0
        print('  %s  +%s TRX  来自 %s'
              % (fmt_time(x['ts']), fmt_amount(x['amount']), x['from']))
        print('     %s前  txid %s'
              % ('%.0f 秒' % age if age < 3600 else '%.1f 小时' % (age / 3600),
                 x['txid'][:28]))
except Exception as e:
    print('  查不到：%s' % e)

# ---------- ③ 系统处理记录 ----------
print()
print('【③ 系统这边的处理结果】')
d = core.load_json(core.DATA_DIR + '/%s.json' % BID, {})
rows = d.get('payments') or []
for p in rows[-5:]:
    print('  %s  +%s TRX → %s 能量  【%s】'
          % (fmt_time(int(p.get('at') or 0) * 1000),
             fmt_amount(p.get('amount') or 0),
             '{:,}'.format(int(p.get('energy') or 0)),
             {'done': '已发货', 'failed': '发货失败',
              'skipped': '金额不足', 'pending': '待处理'
              }.get(p.get('status'), p.get('status'))))
    if p.get('note'):
        print('     %s' % p['note'][:70])
    if p.get('cost'):
        print('     上游实际扣费 %s TRX' % fmt_amount(p['cost']))

# ---------- ④ 买家的地址现在有多少能量 ----------
print()
print('【④ 客户地址的能量（这是最终结果）】')
for who, a in (('客户', BUYER), ('收款地址', ADDR)):
    try:
        r = requests.get('https://api.trongrid.io/v1/accounts/%s' % a,
                         timeout=20)
        j = r.json()
        row = (j.get('data') or [{}])[0]
        ar = row.get('account_resource') or {}
        frozen = row.get('frozenV2') or []
        # 别人委托给你的能量
        got = ar.get('acquired_delegated_frozen_balance_for_energy')
        # 委托给别人的（咱自己没这需求）
        gave = ar.get('delegated_frozen_balance_for_energy')
        win = ar.get('energy_window_size')
        print('  %s %s' % (who, a))
        print('     收到的委托能量 : %s' % (
            fmt_amount(int(got) / 1e6) + ' TRX 等值' if got else '无'))
        print('     能量窗口大小   : %s' % (win if win else '无'))
        print('     TRX 余额       : %s' % fmt_amount(
            int(row.get('balance') or 0) / 1e6))
        print('     USDT 余额      : %s' % fmt_amount(
            int((row.get('trc20') or [{}])[0].get(
                'TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t', 0)) / 1e6))
    except Exception as e:
        print('  %s 查不到：%s' % (who, e))
print()
