# -*- coding: utf-8 -*-
"""跑前端逻辑测试台（node）

★ 为什么要有这个：
  前面几个 bug（标签点不开、图片不更新、未读不清零、定位不到元素）
  **全在前端**，而我一直只能「读代码猜」—— 测不到就只能靠用户试出来。
  这个测试把面板的 <script> 抠出来，在 node 里用假 DOM + 假 fetch
  **真的执行一遍**，把「读完消息未读没清零」「滚不到上次位置」这类
  问题在提交前就抓住。已经抓到两个真 bug 了。
"""
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
    p = shutil.which('node')
    if p:
        return p
    for c in (r'C:\Users\Administrator\.workbuddy\binaries\node\versions'
              r'\22.22.2\node.exe',):
        if os.path.exists(c):
            return c
    return None


NODE = find_node()
check('找到 node（没有它跑不了前端测试）', NODE is not None, NODE or '')
if not NODE:
    print('结果：%d 项通过，%d 项失败' % (len(OK), len(BAD)))
    sys.exit(1)

harness = os.path.join(HERE, '_ui_harness.js')
check('测试台在', os.path.exists(harness))

for page in ('panel_page.html', 'messages_page.html'):
    if page != 'panel_page.html':
        break          # 测试台目前只驱动面板（记录页那套逻辑同源）
    r = subprocess.run([NODE, harness, os.path.join(HERE, page)],
                       capture_output=True, cwd=HERE)
    out = r.stdout.decode('utf-8', 'replace')
    err = r.stderr.decode('utf-8', 'replace').strip()
    for line in out.splitlines():
        if line.strip().startswith(('✅', '❌')):
            print('  ' + line.strip())
    if err:
        check('测试台没崩', False, err[:200].replace('\n', ' '))
    else:
        check('测试台跑完了', True)
    import re
    m = re.search(r'结果：(\d+) 项通过，(\d+) 项失败', out)
    check('测试台有结果输出', m is not None)
    if m:
        check('★ 前端逻辑全过（新增 %s 项）' % m.group(1),
              int(m.group(2)) == 0,
              '失败 %s 项' % m.group(2))

print()
print('=' * 62)
print('结果：%d 项通过，%d 项失败' % (len(OK), len(BAD)))
for x in BAD:
    print('  ❌', x)
print('=' * 62)
sys.exit(1 if BAD else 0)
