# -*- coding: utf-8 -*-
"""扫一遍代码里有没有 Python 3.11+ 才有的东西

★ 为什么需要：本机是 Python 3.11，服务器（Ubuntu 22.04）是 **3.10**。
  用了 3.11 的新特性，本地跑得好好的，一上服务器就 ImportError ——
  已经踩过一次（`from datetime import UTC`，3.11 才有，
  服务器上整个记账机器人导入失败、面板起不来）。

  这个脚本把常见的 3.11+ 特性和 3.11 才有的标准库名字扫出来。
"""
from __future__ import annotations

import io
import os
import re
import sys

sys.stdout.reconfigure(encoding='utf-8')

SKIP_DIRS = {'build', '__pycache__', 'dist', '.git', '.venv', '_apidoc'}

# 名字 → 正则。都是 3.11 才进标准库 / 才有语法的
BANNED = [
    ('datetime.UTC（3.11+）', re.compile(r'\bUTC\b')),
    ('typing.Self（3.11+）', re.compile(r'\bSelf\b')),
    ('enum.StrEnum（3.11+）', re.compile(r'\bStrEnum\b')),
    ('asyncio.TaskGroup（3.11+）', re.compile(r'\bTaskGroup\b')),
    ('tomllib（3.11+）', re.compile(r'\btomllib\b')),
    ('ExceptionGroup（3.11+）', re.compile(r'(?<![\w.])ExceptionGroup\b')),
    ('except*（3.11+）', re.compile(r'\bexcept\s*\*')),
    ('typing.Never / assert_never（3.11+）', re.compile(r'\b(Never|assert_never)\b')),
    ('LiteralString（3.11+）', re.compile(r'\bLiteralString\b')),
    ('contextlib.chdir（3.11+）', re.compile(r'contextlib\.chdir')),
    ('typing.TypeVarTuple（3.11+）', re.compile(r'\bTypeVarTuple\b')),
    ('typing.Unpack（3.11+）', re.compile(r'\bUnpack\b')),
    ('hashlib.file_digest（3.11+）', re.compile(r'file_digest')),
    ('math.exp2 / cbrt（3.11+）', re.compile(r'math\.(exp2|cbrt)\b')),
    ('re 原子组 (?>…)（3.11+）', re.compile(r'\(\?>')),
    ('socket.create_connection(all_errors)（3.11+）', re.compile(r'all_errors')),
]

hits = {}
n_files = 0
for root, dirs, files in os.walk('.'):
    dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
    for fn in files:
        if not fn.endswith('.py') or fn.startswith('_chk_py310'):
            continue
        p = os.path.join(root, fn)
        try:
            lines = io.open(p, encoding='utf-8').read().splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        n_files += 1
        for i, line in enumerate(lines, 1):
            code = line.split('#')[0]          # 注释不算
            for name, ptn in BANNED:
                if ptn.search(code):
                    hits.setdefault(name, []).append(
                        '%s:%d  %s' % (p, i, line.strip()[:70]))

print('扫了 %d 个 .py 文件' % n_files)
print()
if not hits:
    print('✅ 没发现 Python 3.11+ 才有的东西 —— 服务器上的 3.10 能跑')
    sys.exit(0)

print('★ 发现这些东西在 Python 3.10 上会挂：')
for name, where in sorted(hits.items()):
    print('\n  【%s】%d 处' % (name, len(where)))
    for w in where[:12]:
        print('     %s' % w)
    if len(where) > 12:
        print('     …… 还有 %d 处' % (len(where) - 12))
sys.exit(1)
