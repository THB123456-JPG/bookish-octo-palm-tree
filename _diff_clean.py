# -*- coding: utf-8 -*-
"""对比「本地项目」和「已脱敏的干净副本」，找出脱敏脚本误伤的地方

★ 为什么要这个：脱敏脚本是按「真值清单」在所有文本文件里做替换的，
  分不清「这是密钥」和「这是公开常量」。USDT 官方合约地址出现在
  运行日志里，就被当成真地址，从 usdt.py 里替换掉了 ——
  服务器上 USDT 助手从此永远查出来余额是 0（踩过）。
  这个脚本把**所有**被改掉的地方列出来，别只盯着发现的那一个。
"""
import difflib
import io
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')

LOCAL = r'C:\Users\Administrator\Desktop\TG客服多开版'
CLEAN = r'C:\Users\Administrator\Desktop\_github_upload\TG客服多开版'
SKIP_DIRS = {'__pycache__', 'build', 'dist', '.git', '.venv', '_apidoc'}

# 只关心代码和页面 —— 配置/日志/data 本来就是要脱敏的，变了正常
WATCH_EXT = {'.py', '.html', '.js', '.spec', '.sh', '.md', '.txt'}


def read(p):
    try:
        return io.open(p, encoding='utf-8').read().splitlines()
    except (OSError, UnicodeDecodeError):
        return None


bad = []
for root, dirs, files in os.walk(LOCAL):
    dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
    rel = os.path.relpath(root, LOCAL)
    for fn in files:
        if os.path.splitext(fn)[1].lower() not in WATCH_EXT:
            continue
        if fn in ('运行日志.txt', 'config.json', 'bots.json',
                  'merchants.json', '_push_github.py'):
            continue          # 这些本来就该被改
        a = read(os.path.join(root, fn))
        b = read(os.path.join(CLEAN, rel, fn))
        if a is None or b is None:
            continue
        if a == b:
            continue
        # ★★ 只统计「哪一行被改了」，**绝不打印行内容** ——
        #    diff 的「删除侧」就是本地的原始代码，里面有真 token / API Key。
        #    把这个打到屏幕上等于把凭据又泄露一遍。
        rows = []
        sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == 'equal':
                continue
            rows.append((tag, i1 + 1, i2, j1 + 1, j2))
        if rows:
            bad.append((os.path.join(rel, fn), len(a), rows))

print('被改动过的代码/页面文件：%d 个' % len(bad))
print()
for f, n_lines, rows in bad:
    print('=== %s（原 %d 行，%d 处改动）===' % (f, n_lines, len(rows)))
    for tag, i1, i2, j1, j2 in rows[:10]:
        what = {'replace': '改了', 'delete': '删了', 'insert': '加了'}[tag]
        loc = ('第 %d 行' % i1) if i1 == i2 else ('第 %d–%d 行' % (i1, i2))
        print('   %s %s' % (loc, what))
    if len(rows) > 10:
        print('   …… 还有 %d 处' % (len(rows) - 10))
    print()
