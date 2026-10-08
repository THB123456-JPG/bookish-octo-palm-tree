# -*- coding: utf-8 -*-
"""探笔数接口 —— 全部用【非法 count】，保证只会报错、不会真下单扣钱"""
import sys

import requests

sys.stdout.reconfigure(encoding='utf-8')

KEY = '你的APIKEY'      # 能量的 web key
ADDR = 'TBKw17Fxy5kBbev8MMZ16yVKuQTQfsETta'

s = requests.Session()
s.trust_env = False
s.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})

print('=' * 70)
print('笔数接口探测（count 一律给非法值，只验证接口通不通、key 认不认）')
print('=' * 70)

# 全部都带 count=1 / 0（低于文档写的最低 2 笔）→ 不可能真下单
cases = [
    ('count=1（低于最低 2）', {'add': ADDR, 'count': '1'}),
    ('count=1 + autoType=1 智能笔数', {'add': ADDR, 'count': '1',
                                       'autoType': '1'}),
    ('count=0', {'add': ADDR, 'count': '0'}),
    ('地址乱填 + count=1', {'add': 'not-an-address', 'count': '1'}),
    ('带 gift 的 key 试试', {'add': ADDR, 'count': '1'}),
]
for i, (name, extra) in enumerate(cases):
    p = {'apikey': KEY}
    p.update(extra)
    if name.startswith('带 gift'):
        p['apikey'] = '你的APIKEY'
    try:
        r = s.get('https://web.apitrx.com/auto', params=p, timeout=25)
        t = (r.text or '')[:160].replace('\n', ' ')
        print('  %-28s HTTP %-4s %s' % (name, r.status_code, t))
    except Exception as e:
        print('  %-28s 连不上：%s' % (name, e))

print()
print('=' * 70)
print('参考：笔数价格公示（在价格页里）')
print('=' * 70)
