# -*- coding: utf-8 -*-
"""前端 JS 语法检查

★ 为什么单独一个测试：
  面板和记录页的 JS 一旦有语法错误，**整个页面直接全废** ——
   连登录都进不去，而且浏览器只在控制台报错，界面上看不出原因。
   比「某个按钮点不开」严重得多，值得每次跑一遍。
   用 node --check 做真正的解析（不是数括号那种糊弄）。
"""
import io
import os
import shutil
import subprocess
import sys

sys.stdout.reconfigure(encoding='utf-8')

HERE = os.path.dirname(os.path.abspath(__file__))

OK, BAD = [], []


def check(name, cond, extra=''):
    (OK if cond else BAD).append(name)
    print('  %s %s%s' % ('✅' if cond else '❌', name,
                         ('  → %s' % (extra,)) if extra else ''))


def find_node():
    for c in ('node', 'node.exe'):
        p = shutil.which(c)
        if p:
            return p
    # 兜底：常见的便携版位置
    for p in (r'C:\Users\Administrator\.workbuddy\binaries\node\versions'
              r'\22.22.2\node.exe',):
        if os.path.exists(p):
            return p
    return None


NODE = find_node()
print('node:', NODE or '（找不到）')
print()
# ★ 找不到 node 就**直接算失败**，不"跳过" ——
#   跳过写多了，哪天 node 没了就再也测不出 JS 语法错了
check('找到 node（没有它测不了 JS 语法）', NODE is not None, NODE or '')
print()

FILES = ('panel_page.html', 'messages_page.html')

for f in FILES:
    path = os.path.join(HERE, f)
    src = io.open(path, encoding='utf-8').read()
    i, j = src.find('<script>'), src.rfind('</script>')
    check('%s 有 <script> 块' % f, i >= 0 and j > i)
    if i < 0 or j <= i:
        continue
    js = src[i + len('<script>'):j]
    check('  %s <script> 不是空的' % f, len(js.strip()) > 500,
          '%d 字节' % len(js))

    # ★ 别自己写括号配对检查：JS 里有正则字面量（比如 esc() 的 /[&<>"']/g），
    #   手写的扫描器会把正则当成字符串，然后全是误报。交给 node 真解析。
    if NODE:
        tmp = os.path.join(HERE, '_tmp_jscheck.js')
        io.open(tmp, 'w', encoding='utf-8').write(js)
        r = subprocess.run([NODE, '--check', tmp], capture_output=True)
        if os.path.exists(tmp):
            os.remove(tmp)
        err = r.stderr.decode('utf-8', 'replace').strip()
        check('  ★ %s 用 node 真解析一遍' % f, r.returncode == 0,
              err[:300].replace('\n', ' '))

    # 两个页面都有标签/消息相关的关键函数，别删空了
    if f == 'panel_page.html':
        for fn in ('switchTab', 'renderTypeTabs', 'showType', 'msgLoadBots',
                   'msgOpenBot', 'msgOpenChat', 'msgGoto', 'msgTick',
                   'muteChat', 'pinBot', 'pinChat', 'editNote',
                   'firstUnreadKey', 'stopEv', 'apiHeaders'):
            check('  有 function %s' % fn, ('function %s' % fn) in js)

print()
print('=' * 62)
print('结果：%d 项通过，%d 项失败' % (len(OK), len(BAD)))
for x in BAD:
    print('  ❌', x)
print('=' * 62)
sys.exit(1 if BAD else 0)
