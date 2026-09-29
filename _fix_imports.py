# -*- coding: utf-8 -*-
"""把搬过来的模块里的导入路径改成本项目的（ledger.xxx）"""
import os
import re
import sys

sys.stdout.reconfigure(encoding='utf-8')

D = r'C:\Users\Administrator\Desktop\TG客服多开版\ledger'

SUBS = [
    (r'from storage\.repositories\.ledger_storage import',
     'from ledger.storage import'),
    (r'from services\.ledger\.ledger_commands import', 'from ledger.commands import'),
    (r'from services\.calculator import', 'from ledger.calculator import'),
    (r'from services\.ledger\.message_identity import', 'from ledger.identity import'),
    (r'from utils\.text_utils import', 'from ledger.text_utils import'),
    (r'from services\.price\.price_service import', 'from ledger.price import'),
    (r'from services\.trc20\.verify_service import', 'from ledger.trc20 import'),
    (r'from services\.ledger\.bill_messages import', 'from ledger.bill_messages import'),
    (r'from services\.ledger\.cutoff_reminders import', 'from ledger.reminders import'),
]

for fn in sorted(os.listdir(D)):
    if not fn.endswith('.py'):
        continue
    p = os.path.join(D, fn)
    t0 = open(p, encoding='utf-8').read()
    t = t0
    for a, b in SUBS:
        t = re.sub(a, b, t)
    if t != t0:
        open(p, 'w', encoding='utf-8').write(t)
        print('改了导入:', fn)

print()
print('剩下还没改的跨包导入：')
for fn in sorted(os.listdir(D)):
    if not fn.endswith('.py'):
        continue
    p = os.path.join(D, fn)
    for i, line in enumerate(open(p, encoding='utf-8'), 1):
        s = line.strip()
        if re.match(r'^(from|import)\s+(services|storage|utils|config|handlers)\b', s):
            print('  %s:%d  %s' % (fn, i, s))

print()
print('PTB / 第三方依赖（要处理的）：')
for fn in sorted(os.listdir(D)):
    if not fn.endswith('.py'):
        continue
    p = os.path.join(D, fn)
    for i, line in enumerate(open(p, encoding='utf-8'), 1):
        s = line.strip()
        if re.match(r'^(from|import)\s+(telegram|httpx|asyncio)\b', s):
            print('  %s:%d  %s' % (fn, i, s))
