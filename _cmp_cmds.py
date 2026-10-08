# -*- coding: utf-8 -*-
"""把完整版和面板版里「用户能发的命令字面量」全部抽出来，对差集

只看记账相关的源码：
  完整版  services/ledger/ledger_commands.py + runtime.py 里记账那几段
  面板版  runners/ledger/*.py + core.py 的记账部分

做法：正则抓所有字符串字面量，挑出「像命令」的（纯中文、或 /xxx、或短英文），
再人工看差集。这样比我一行行读可靠。
"""
import os
import re
import sys

sys.stdout.reconfigure(encoding='utf-8')

ROOT = os.path.dirname(os.path.abspath(__file__))
LIT = re.compile(r"""['"]([^'"\n]{1,24})['"]""")

# 命令的样子：全是中文；或者 /xxx；或者 bj/z0 这种极短的英文
CN = re.compile(r'^[一-鿿]{1,12}$')
SLASH = re.compile(r'^/[a-z_]{2,16}$')
SHORT = re.compile(r'^[a-zA-Z]{1,3}\d?$')
NOISE = {'', ' ', '，', '。', '、', '：', '：%s', '%s', 'utf-8', 'rb', 'w',
         'a', 'r', 'id', 'ok', 'type', 'text', 'note', 'html', 'send'}


def cmds_of(path):
    got = set()
    with open(path, encoding='utf-8') as f:
        src = f.read()
    for m in LIT.finditer(src):
        s = m.group(1).strip()
        if s in NOISE:
            continue
        if CN.match(s) or SLASH.match(s) or SHORT.match(s):
            got.add(s)
    return got


FULL = [os.path.join(ROOT, '_full_ledger', 'services/ledger/ledger_commands.py'),
        os.path.join(ROOT, '_full_ledger', 'services/ledger/ledger_service.py'),
        os.path.join(ROOT, '_full_ledger', 'services/ledger/report_service.py')]
PANE = [os.path.join(ROOT, 'runners/ledger', x)
        for x in ('commands.py', 'runner.py', 'storage.py', 'bill_messages.py',
                  'reminders.py', 'group_admin.py', 'price.py', 'calculator.py',
                  'trc20.py', 'positions.py', 'text_utils.py')]

full, pane = set(), set()
for p in FULL:
    if os.path.exists(p):
        full |= cmds_of(p)
for p in PANE:
    if os.path.exists(p):
        pane |= cmds_of(p)

missing = sorted(full - pane)
print('=' * 66)
print('完整版有、面板版**没有**的字面量（%d 个）' % len(missing))
print('=' * 66)
for s in missing:
    print('  ', s)

print()
print('=' * 66)
print('面板版有、完整版没有的（%d 个，多半是面板加的新功能）' % len(pane - full))
print('=' * 66)
for s in sorted(pane - full):
    print('  ', s)
