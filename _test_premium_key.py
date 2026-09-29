# -*- coding: utf-8 -*-
"""测新的会员 API Key：能用在哪几个域名、余额多少"""
import sys

import requests

sys.stdout.reconfigure(encoding='utf-8')

NEW_KEY = '你的APIKEY'     # 用户给的新 key（会员）
OLD_KEY = '你的APIKEY'     # 之前那个（能量）

H = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}

for tag, key in (('新 key（会员）', NEW_KEY), ('旧 key（能量）', OLD_KEY)):
    print('=' * 60)
    print(tag)
    print('=' * 60)
    for base, dom in (('https://web.apitrx.com', 'web (TRX计价)'),
                      ('https://gift.apitrx.com', 'gift (USDT计价)')):
        try:
            r = requests.get(base + '/balance', params={'apikey': key},
                             headers=H, timeout=20)
            t = (r.text or '')[:110].replace('\n', ' ')
            print('  %-16s HTTP %-3s %s' % (dom, r.status_code, t))
        except Exception as e:
            print('  %-16s 连不上：%s' % (dom, e))
    print()
