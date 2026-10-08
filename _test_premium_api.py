# -*- coding: utf-8 -*-
"""验证会员 Key 能不能用：查用户名是否符合开通条件（免费接口）"""
import json
import sys

import requests

sys.stdout.reconfigure(encoding='utf-8')

KEY = '你的APIKEY'
BASE = 'https://web.apitrx.com'

s = requests.Session()
s.trust_env = False          # 直连，走代理会被 Cloudflare 挡
s.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})


def call(path, payload, how):
    try:
        if how == 'json':
            r = s.post(BASE + path, json=payload, timeout=25)
        else:
            r = s.post(BASE + path, data=payload, timeout=25)
    except Exception as e:
        print('   %-6s 连不上：%s' % (how, e))
        return None
    body = (r.text or '')[:200].replace('\n', ' ')
    if 'Attention Required' in r.text or r.status_code == 403:
        print('   %-6s 被 Cloudflare 挡了' % how)
        return None
    print('   %-6s HTTP %s → %s' % (how, r.status_code, body))
    try:
        return r.json()
    except Exception:
        return None


print('=' * 62)
print('查用户名是否符合开通条件（不花钱，只读）')
print('=' * 62)
for u in ('@pms66', 'pms66'):
    print()
    print('用户名：', u)
    for how in ('json', 'form'):
        j = call('/query', {'apikey': KEY, 'username': u}, how)
        if j is not None:
            print('        code=%s message=%s' % (j.get('code'), j.get('message')))
            d = j.get('data') or {}
            if d:
                print('        昵称：%s' % d.get('nickname'))
            break
