# -*- coding: utf-8 -*-
"""生成身份证前 6 位 → 归属地的查表数据

跑法：python _build_idcode.py

★ 为什么要生成而不是直接带原始 JSON：
  `pcas-code.json` 有 1.9MB（还是层级嵌套的），而我们只要
  「六位码 → 省市区」这一张平表，压出来只有几十 KB。
★ 数据来源：modood/Administrative-divisions-of-China（民政部区划代码）
  下载地址（见 _build 里的 URL），生成完 **原始 JSON 就删掉**，只留产物。

产物：runners/ledger/assets/idcode.txt
      每行 `区划码<TAB>省 市 区`，按码排序（查找时用二分）
"""
import json
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, 'runners', 'ledger', 'assets', 'pcas-code.json')
OUT = os.path.join(HERE, 'runners', 'ledger', 'assets', 'idcode.txt')

# 这几个「市级」名字本身不是地名，拼进去会很怪（北京市市辖区东城区）
SKIP_CITY = {'市辖区', '县', '省直辖县级行政区划', '自治区直辖县级行政区划',
             '直辖县级行政区划'}


def display(prov, city, area):
    parts = [prov or '']
    if city and city not in SKIP_CITY:
        parts.append(city)
    if area:
        parts.append(area)
    text = ''.join(parts)
    # 「北京市北京市」这种重复去掉（有些数据源省市同名）
    if prov and city and city.startswith(prov) and len(city) > len(prov):
        text = city + (area or '')
    return text


def main():
    with open(SRC, encoding='utf-8') as f:
        data = json.load(f)
    rows = {}

    def walk(nodes, prov='', city=''):
        for node in nodes:
            code = str(node.get('code') or '')
            name = node.get('name') or ''
            kids = node.get('children') or []
            if len(code) == 2:
                rows[code] = display(name, '', '')
                walk(kids, name, '')
            elif len(code) == 4:
                rows[code] = display(prov, name, '')
                walk(kids, prov, name)
            elif len(code) == 6:
                rows[code] = display(prov, city, name)
                walk(kids, prov, city)      # 个别下面还有街道级，不再往下细分

    walk(data)
    with open(OUT, 'w', encoding='utf-8') as f:
        for code in sorted(rows):
            f.write('%s\t%s\n' % (code, rows[code]))
    print('生成 %s' % OUT)
    print('  共 %d 条（%d 位省 / %d 位市 / %d 位区）'
          % (len(rows),
             sum(1 for c in rows if len(c) == 2),
             sum(1 for c in rows if len(c) == 4),
             sum(1 for c in rows if len(c) == 6)))
    print('  大小 %.1f KB' % (os.path.getsize(OUT) / 1024.0))
    for probe in ('110101', '310104', '440305', '510107', '370102'):
        print('  %s → %s' % (probe, rows.get(probe, '（没有）')))


if __name__ == '__main__':
    main()
