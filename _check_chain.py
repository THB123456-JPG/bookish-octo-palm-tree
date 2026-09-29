# -*- coding: utf-8 -*-
"""查收款地址最近的 TRX 转入 —— 诊断「我转了钱系统怎么没反应」"""
import sys
import time

sys.stdout.reconfigure(encoding='utf-8')

from tron import Tron, fmt_amount, fmt_time

ADDR = 'TBKw17Fxy5kBbev8MMZ16yVKuQTQfsETta'

print('=' * 62)
print('收款地址：', ADDR)
print('当前时间：', time.strftime('%Y-%m-%d %H:%M:%S'))
print('=' * 62)

t = Tron()
try:
    txs = t.trx_transfers(ADDR, limit=10)
except Exception as e:
    print('❌ 查链上失败：%s' % e)
    sys.exit(1)

if not txs:
    print('链上查不到任何转入记录。')
    sys.exit(0)

print('最近 %d 笔 TRX 转入（新→旧）：' % len(txs))
print()
for x in txs:
    age = time.time() - (int(x.get('ts') or 0) / 1000.0)
    print('  %s  +%s TRX' % (fmt_time(x.get('ts')), fmt_amount(x.get('amount') or 0)))
    print('     来自 %s' % x.get('from'))
    print('     txid %s' % x.get('txid'))
    print('     %s前' % ('%.0f 秒' % age if age < 3600
                         else '%.1f 小时' % (age / 3600)))
    print()

newest = txs[0]
print('=' * 62)
print('★ 最新一笔：%s TRX，来自 %s'
      % (fmt_amount(newest.get('amount') or 0), newest.get('from')))
print('=' * 62)
