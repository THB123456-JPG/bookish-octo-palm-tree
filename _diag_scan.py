# -*- coding: utf-8 -*-
"""诊断：钱到链上了，为什么系统没发货？"""
import sys
import time

sys.stdout.reconfigure(encoding='utf-8')

import core
from tron import Tron, fmt_amount, fmt_time

BID = 'accd0cdc'
ADDR = 'TBKw17Fxy5kBbev8MMZ16yVKuQTQfsETta'

d = core.load_json(core.DATA_DIR + '/%s.json' % BID, {})
sc = d.get('scan') or {}
last_ts = int(sc.get('last_ts') or 0)
seen = sc.get('seen') or []

print('=' * 62)
print('① 扫描进度（程序自己记的）')
print('=' * 62)
print('  基准已建立 :', sc.get('baseline'))
print('  last_ts    :', last_ts,
      '→', fmt_time(last_ts) if last_ts else '(空)')
print('  已记录 txid:', len(seen), '条')
print('  流水条数   :', len(d.get('payments') or []))

NEW_TXID = '8bf6f1163c95240c5466835b87dec50df04a9a4cf87ab10e0c71a34cb3f063f8'
print()
print('  那笔 3.5 TRX 的 txid 在已记录列表里吗：', NEW_TXID in seen)

print()
print('=' * 62)
print('② 模拟一次扫描，看能不能发现它')
print('=' * 62)
t = Tron()
min_ts = (last_ts - 120000) if last_ts else None
print('  用 min_ts =', min_ts, '→', fmt_time(min_ts) if min_ts else '(不限)')
print()

for label, mt in (('带 min_ts（程序现在的用法）', min_ts),
                  ('不带 min_ts（全量查）', None)):
    try:
        txs = t.trx_transfers(ADDR, limit=30, min_ts=mt)
    except Exception as e:
        print('  %-28s ❌ 报错：%s' % (label, e))
        continue
    ids = [x['txid'] for x in txs]
    hit = NEW_TXID in ids
    print('  %-28s 返回 %d 笔，那笔在里面吗：%s'
          % (label, len(txs), '✅ 在' if hit else '❌ 不在'))
    for x in txs[:4]:
        mark = ' ★就是它' if x['txid'] == NEW_TXID else ''
        print('      %s  %s TRX  %s%s'
              % (fmt_time(x['ts']), fmt_amount(x['amount']),
                 x['txid'][:20], mark))
    print()

print('=' * 62)
print('③ 如果扫到了，按现在的配置应该发多少')
print('=' * 62)
from runners.shop import store as S


class _Fake:
    def __init__(self):
        self.bot = {'id': BID, 'shop': {}}
        self.mgr = type('M', (), {'save': lambda s: None})()

    def save_data(self):
        pass


st = S.ShopStore(_Fake())
cfg = st.cfg
print('  单价 :', cfg.get('price'), 'TRX =', cfg.get('energy'), '能量')
u, e, note = st.calc(3.5)
print('  收到 3.5 TRX → %d 个单位 / %s 能量' % (u, '{:,}'.format(e)))
print('  备注 :', note or '(无)')
print('  结论 :', '应该发货' if u > 0 else '❌ 不会发货')
