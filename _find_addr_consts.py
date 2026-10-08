# -*- coding: utf-8 -*-
"""找出代码里「写成常量的 TRON 地址」

★ 为什么需要：脱敏脚本分不清「密钥」和「公开常量」。
  USDT 官方合约地址出现在运行日志里（扫链时打印过），就被当成真地址，
  从 usdt.py 里替换掉了 —— 服务器上 USDT 助手从此永远查出余额 0（踩过）。

  这里把**模块级常量形式**的地址列出来（`XXX = 'T...'`），
  这些基本都是公开常量，要进脱敏脚本的白名单，绝对不能被替换。

★ 只打印「变量名 + 文件:行号」，**不打印地址本身** ——
  虽然多半是公开的，但万一是别人私人的地址就不该往屏幕上放。
"""
import io
import os
import re
import sys

sys.stdout.reconfigure(encoding='utf-8')

SKIP_DIRS = {'__pycache__', 'build', 'dist', '.git', '.venv', '_apidoc'}
ADDR = r'T[1-9A-HJ-NP-Za-km-z]{33}'
# 模块级常量：全大写名字 = 'T...'
CONST = re.compile(r'^\s*([A-Z][A-Z0-9_]{2,})\s*=\s*[\'"](%s)[\'"]' % ADDR)

found = []
for root, dirs, files in os.walk('.'):
    dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
    for fn in files:
        if not fn.endswith('.py'):
            continue
        p = os.path.join(root, fn)
        try:
            lines = io.open(p, encoding='utf-8').read().splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for i, line in enumerate(lines, 1):
            m = CONST.match(line)
            if m:
                found.append((p, i, m.group(1), len(m.group(2))))

print('代码里写成常量的 TRON 地址：%d 处' % len(found))
print()
for p, i, name, n in found:
    print('  %-28s 第 %-4d 行   变量名 %s' % (p, i, name))
print()
print('★ 这些都是公开常量（比如 USDT 官方合约），')
print('  必须加进 _push_github.py 的「不许替换」白名单。')
