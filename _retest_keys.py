# -*- coding: utf-8 -*-
"""把两个 key 在两个域名、两种参数编码下全测一遍，看清楚到底谁有效。

用「不存在的用户名」去打，这样即使 key 有效也不会真开通出去。
"""
import sys
import time

import requests

sys.stdout.reconfigure(encoding='utf-8')

KEYS = [
    ('新key 57a0e12e', '你的APIKEY'),
    ('旧key 15B2726A', '你的APIKEY'),
]
DOMAINS = [('web(TRX计价)', 'https://web.apitrx.com'),
           ('gift(USDT计价)', 'https://gift.apitrx.com')]
GHOST = '@zzz_nobody_here_9999'

s = requests.Session()
s.trust_env = False
s.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})


def call(base, path, payload, how):
    """返回 (状态, 内容)。403/429 说明被 Cloudflare 限流，不是 key 的问题"""
    try:
        if how == 'json':
            r = s.post(base + path, json=payload, timeout=25)
        else:
            r = s.post(base + path, data=payload, timeout=25)
    except Exception as e:
        return '连不上', str(e)[:60]
    t = (r.text or '').replace('\n', ' ')
    if r.status_code in (403, 429) or 'Attention Required' in t:
        return '限流', 'Cloudflare 挡了，这次不算'
    return r.status_code, t[:120]


def judge(txt):
    """把上游的报错翻译成人话"""
    if 'apikey错误' in txt or 'APIKEY' in txt.upper() and '错误' in txt:
        return '❌ key 在这个域名无效'
    if '权限未开启' in txt:
        return '✅ key 有效！只差「会员API权限」没开'
    if '用户不存在' in txt or '不存在' in txt:
        return '✅ key 有效（只是这个用户名不存在）'
    if '参数解析错误' in txt:
        return '⚠️ 参数格式不对，没走到 key 校验'
    if '"code":200' in txt:
        return '✅ 成功'
    return '?'


for kname, key in KEYS:
    print('=' * 74)
    print(kname)
    print('=' * 74)
    for dname, base in DOMAINS:
        for how in ('json', 'form'):
            st, txt = call(base, '/query',
                           {'apikey': key, 'username': GHOST}, how)
            print('  %-15s /query  %-4s → %s' % (dname, how, judge(txt)))
            print('        %s' % txt[:110])
            time.sleep(2.5)
        # 再试下单接口（同样用不存在的用户名，不会真开通）
        for how in ('json', 'form'):
            st, txt = call(base, '/premium',
                           {'apikey': key, 'username': GHOST, 'month': '3'},
                           how)
            print('  %-15s /premium %-4s → %s' % (dname, how, judge(txt)))
            print('        %s' % txt[:110])
            time.sleep(2.5)
        print()
    print()
