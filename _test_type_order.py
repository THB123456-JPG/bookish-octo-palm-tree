# -*- coding: utf-8 -*-
"""机器人类型的展示顺序必须三处一致

用户 2026-09-28 指定：**记账 → 客服 → USDT → 商城**

顺序写在三个地方，改一处漏一处就会出现「下拉是记账排第一、
筛选条还是客服排第一」这种诡异情况：
  ① `manager.RUNNERS`（后端注册表，panel 的 types 接口按它出）
  ② `panel_page.html` 的 `<select id="type">`
  ③ `panel_page.html` 的 `TYPE_TABS`

前端那两处由 `_ui_harness.js` 管；这里管后端那张表 + 三处一致性。
"""
import io
import os
import re
import sys

sys.stdout.reconfigure(encoding='utf-8')

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

OK, BAD = [], []
WANT = ['ledger', 'kefu', 'usdt', 'shop']


def check(name, cond, extra=''):
    (OK if cond else BAD).append(name)
    print('  %s %s%s' % ('✅' if cond else '❌', name,
                         ('  → %s' % (extra,)) if extra else ''))


print('一、后端 manager.RUNNERS 的顺序')
import manager                                     # noqa: E402

order = list(manager.RUNNERS.keys())
check('★★ RUNNERS 顺序 = 记账 → 客服 → USDT → 商城',
      order == WANT, ' → '.join(order))
check('★ 每个类型都有中文名',
      all(len(v) >= 2 and v[1] for v in manager.RUNNERS.values()),
      str([v[1] for v in manager.RUNNERS.values()]))
check('★ 兜底类型还是客服（老数据没 type 的按客服算，别跟着顺序改）',
      manager.DEFAULT_TYPE == 'kefu', manager.DEFAULT_TYPE)
check('★ 四种类型一个都没少', set(order) == set(WANT), str(sorted(order)))

print()
print('二、前端两处的顺序（和后端对得上吗）')
html = io.open(os.path.join(HERE, 'panel_page.html'), encoding='utf-8').read()

m = re.search(r'<select id="type">(.*?)</select>', html, re.S)
check('找得到添加机器人的下拉', m is not None)
sel = re.findall(r'value="([a-z]+)"', m.group(1)) if m else []
check('★★ 下拉顺序一致', sel == WANT, ' → '.join(sel))

m2 = re.search(r'var TYPE_TABS = \[(.*?)\];', html, re.S)
check('找得到 TYPE_TABS', m2 is not None)
tabs = re.findall(r"v:\s*'([a-z]+)'", m2.group(1)) if m2 else []
check('★★ 筛选条顺序一致', tabs == WANT, ' → '.join(tabs))

check('★★★ 三处完全一致（这是最容易漏的地方）',
      order == sel == tabs,
      'RUNNERS=%s / 下拉=%s / TABS=%s'
      % (order, sel, tabs))
check('★ 默认选中项跟着第一个走（不是写死的 kefu）',
      "var CUR_TYPE = TYPE_TABS[0].v;" in html)

print()
print('=' * 58)
print('结果：%d 项通过，%d 项失败' % (len(OK), len(BAD)))
for x in BAD:
    print('  ❌', x)
print('=' * 58)
sys.exit(1 if BAD else 0)
