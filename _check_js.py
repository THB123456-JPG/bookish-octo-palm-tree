# -*- coding: utf-8 -*-
"""把 panel_page.html 里的 <script> 抽出来交给 node --check 校验语法"""
import os
import re
import subprocess
import sys
import tempfile

sys.stdout.reconfigure(encoding='utf-8')

HERE = os.path.dirname(os.path.abspath(__file__))
html = open(os.path.join(HERE, 'panel_page.html'), encoding='utf-8').read()

blocks = re.findall(r'(?s)<script[^>]*>(.*?)</script>', html)
if not blocks:
    print('没找到 <script> 块')
    sys.exit(1)

js = '\n;\n'.join(blocks)
tmp = os.path.join(tempfile.gettempdir(), '_panel_check.js')
with open(tmp, 'w', encoding='utf-8') as f:
    f.write(js)

print('抽出 %d 个 script 块，共 %d 字符' % (len(blocks), len(js)))
r = subprocess.run(['node', '--check', tmp], capture_output=True, text=True)
if r.returncode == 0:
    print('✅ JS 语法没问题')
else:
    print('❌ JS 语法有错：')
    print(r.stdout)
    print(r.stderr)
sys.exit(r.returncode)
