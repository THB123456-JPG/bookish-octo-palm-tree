# -*- coding: utf-8 -*-
"""列出 panel_page.html 的 HTML 结构（只看 <script> 之前的部分）"""
import sys

sys.stdout.reconfigure(encoding='utf-8')
L = open('panel_page.html', encoding='utf-8').read().splitlines()
for i, l in enumerate(L):
    t = l.strip()
    if t.startswith('<script'):
        print('%4d: <script>  （后面都是 JS，共 %d 行）' % (i + 1, len(L) - i))
        break
    if not t or t.startswith('//'):
        continue
    if t[:1] in '<' or t.startswith('</'):
        print('%4d: %s' % (i + 1, t[:96]))
