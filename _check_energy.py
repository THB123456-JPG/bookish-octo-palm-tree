# -*- coding: utf-8 -*-
"""查一个 TRON 地址现在有多少可用能量（客户最关心的就是这个）"""
import json
import sys

import requests

sys.stdout.reconfigure(encoding='utf-8')

BUYER = 'TUaZ5nGofnSFpoYB5DSfqpZBZAAbmuWzcF'
ADDR = 'TBKw17Fxy5kBbev8MMZ16yVKuQTQfsETta'


def dump(label, addr):
    print('=' * 64)
    print('%s  %s' % (label, addr))
    print('=' * 64)
    for path, name in ((('/v1/accounts/%s/resources' % addr), 'resources'),
                       (('/v1/accounts/%s' % addr), 'account')):
        try:
            r = requests.get('https://api.trongrid.io' + path, timeout=25)
            j = r.json()
        except Exception as e:
            print('  %-10s 失败：%s' % (name, e))
            continue
        print()
        print('  --- %s ---' % path)
        if name == 'resources':
            print(json.dumps(j, ensure_ascii=False, indent=4)[:1200])
        else:
            row = (j.get('data') or [{}])[0]
            print('  balance(TRX)      :', int(row.get('balance') or 0) / 1e6)
            ar = row.get('account_resource') or {}
            for k, v in ar.items():
                if isinstance(v, (int, float, str, bool)):
                    print('  %-18s: %s' % (k, v))
            fz = row.get('frozenV2')
            if fz:
                print('  frozenV2          :', json.dumps(fz,
                                                          ensure_ascii=False)[:300])


dump('客户（转账的地址 = 收能量的地址）', BUYER)
print()
dump('你的收款地址', ADDR)
