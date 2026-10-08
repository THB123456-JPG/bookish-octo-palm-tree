# -*- coding: utf-8 -*-
"""新 key 是不是 gift 域名的（USDT 计价）？gift 那边要 form 编码"""
import sys

import requests

sys.stdout.reconfigure(encoding='utf-8')

NEW_KEY = '你的APIKEY'
OLD_KEY = '你的APIKEY'
GHOST = '@zzz_this_user_does_not_exist_9999'

s = requests.Session()
s.trust_env = False
s.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})

print('=' * 70)
print('gift 域名（USDT 计价）用 form 编码再试')
print('=' * 70)
for tag, key in (('新key', NEW_KEY), ('旧key', OLD_KEY)):
    for path, pl in (('/premium', {'apikey': key, 'username': GHOST,
                                   'month': '3'}),
                     ('/balance', {'apikey': key})):
        try:
            r = s.post('https://gift.apitrx.com' + path, data=pl, timeout=25)
            print('  %-6s gift %-9s HTTP %-3s %s'
                  % (tag, path, r.status_code,
                     (r.text or '')[:130].replace('\n', ' ')))
        except Exception as e:
            print('  %-6s gift %-9s 连不上：%s' % (tag, path, e))

print()
print('=' * 70)
print('gift 域名 GET 方式（有些接口是 GET）')
print('=' * 70)
for tag, key in (('新key', NEW_KEY), ('旧key', OLD_KEY)):
    try:
        r = s.get('https://gift.apitrx.com/balance',
                  params={'apikey': key}, timeout=25)
        print('  %-6s HTTP %-3s %s'
              % (tag, r.status_code, (r.text or '')[:130].replace('\n', ' ')))
    except Exception as e:
        print('  %-6s 连不上：%s' % (tag, e))
