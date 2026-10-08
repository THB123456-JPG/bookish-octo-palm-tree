# -*- coding: utf-8 -*-
"""判断会员 Key 到底能不能用。

用「不存在的用户名」去打下单接口 —— 这样即使 key 有效也不会真的开通出去，
返回的报错信息能区分是 key 的问题还是用户名的问题。
"""
import sys

import requests

sys.stdout.reconfigure(encoding='utf-8')

NEW_KEY = '你的APIKEY'
OLD_KEY = '你的APIKEY'
GHOST = '@zzz_this_user_does_not_exist_9999'

s = requests.Session()
s.trust_env = False
s.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})


def post(base, path, payload, tag):
    try:
        r = s.post(base + path, json=payload, timeout=25)
    except Exception as e:
        print('  %-34s 连不上：%s' % (tag, e))
        return
    if r.status_code == 403 or 'Attention Required' in (r.text or ''):
        print('  %-34s 被 Cloudflare 挡（等下再试）' % tag)
        return
    print('  %-34s HTTP %-3s %s'
          % (tag, r.status_code, (r.text or '')[:150].replace('\n', ' ')))


print('=' * 70)
print('用不存在的用户名打「开通会员」接口 —— 不会真开通，只看它报什么错')
print('=' * 70)
for tag, key in (('新key', NEW_KEY), ('旧key', OLD_KEY)):
    post('https://web.apitrx.com', '/premium',
         {'apikey': key, 'username': GHOST, 'month': '3'},
         '%s → web(TRX) /premium' % tag)

print()
print('=' * 70)
print('同样的 key 打「查询用户名」接口')
print('=' * 70)
for tag, key in (('新key', NEW_KEY), ('旧key', OLD_KEY)):
    post('https://web.apitrx.com', '/query',
         {'apikey': key, 'username': GHOST},
         '%s → web(TRX) /query' % tag)

print()
print('=' * 70)
print('gift 域名（USDT 计价）也试一下')
print('=' * 70)
for tag, key in (('新key', NEW_KEY), ('旧key', OLD_KEY)):
    post('https://gift.apitrx.com', '/premium',
         {'apikey': key, 'username': GHOST, 'month': '3'},
         '%s → gift(USDT) /premium' % tag)
