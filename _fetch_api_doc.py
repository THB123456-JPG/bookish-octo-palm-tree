# -*- coding: utf-8 -*-
"""把 apitrx.com 的 API 文档抓下来，抽成纯文本存到 _apidoc/ 目录"""
import os
import re
import sys
import time

import requests

sys.stdout.reconfigure(encoding='utf-8')

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_apidoc')
os.makedirs(OUT, exist_ok=True)

UA = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                  '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9',
}
PROXY = {'http': 'http://127.0.0.1:7890', 'https': 'http://127.0.0.1:7890'}

PAGES = {
    'api': 'api.html',            # 首页/获取key
    'energy': 'energy.html',      # 能量下单
    'query': 'query.html',        # 订单查询
    'estimation': 'estimation.html',  # 自动预估
    'price': 'price.html',        # 查询价格
    'balance': 'balance.html',    # 查询余额
    'active': 'active.html',      # 激活地址
    'auto': 'auto.html',          # 笔数下单
    'autolist': 'autolist.html',
    'autorecord': 'autorecord.html',
    'speed': 'speed.html',        # 速充
    'premium': 'premium.html',    # 会员
    'apiprice': 'apiprice.html',
    'other': 'other.html',
}


def strip_html(html):
    h = re.sub(r'(?s)<script.*?</script>', ' ', html)
    h = re.sub(r'(?s)<style.*?</style>', ' ', h)
    h = re.sub(r'(?s)<nav.*?</nav>', ' ', h)
    h = re.sub(r'(?s)<head.*?</head>', ' ', h)
    h = re.sub(r'(?s)<footer.*?</footer>', ' ', h)
    h = re.sub(r'<br\s*/?>', '\n', h)
    h = re.sub(r'</(p|div|li|tr|h[1-6]|pre|code)>', '\n', h)
    h = re.sub(r'<[^>]+>', ' ', h)
    for a, b in (('&nbsp;', ' '), ('&lt;', '<'), ('&gt;', '>'),
                 ('&amp;', '&'), ('&quot;', '"'), ('&#39;', "'")):
        h = h.replace(a, b)
    h = re.sub(r'[ \t]+', ' ', h)
    h = re.sub(r'\n\s*\n+', '\n', h)
    return h.strip()


s = requests.Session()
s.proxies.update(PROXY)
s.headers.update(UA)

for name, page in PAGES.items():
    url = 'https://apitrx.com/pages/' + page
    try:
        r = s.get(url, timeout=40)
    except Exception as e:
        print('%-12s 请求失败 %s' % (name, e))
        continue
    if r.status_code != 200 or 'Attention Required' in r.text:
        print('%-12s HTTP %s / 被挡' % (name, r.status_code))
        continue
    text = strip_html(r.text)
    # 去掉 VuePress 的导航残渣
    for junk in ('跳至主要內容', 'open in new window', 'Last Updated'):
        text = text.replace(junk, '')
    path = os.path.join(OUT, name + '.txt')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)
    print('%-12s OK  %d 字符 → %s' % (name, len(text), os.path.basename(path)))
    time.sleep(0.5)

print()
print('存到:', OUT)
