# -*- coding: utf-8 -*-
"""补抓会员订单查询等页面的文档（直连，别走代理）"""
import os
import re
import sys
import time

import requests

sys.stdout.reconfigure(encoding='utf-8')

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_apidoc')
os.makedirs(OUT, exist_ok=True)

UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                    'Chrome/131.0.0.0 Safari/537.36',
      'Accept-Language': 'zh-CN,zh;q=0.9'}

PAGES = {
    'premiumstatus': 'premiumstatus.html',   # 会员订单查询
    'premium': 'premium.html',               # 会员开通/星星
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
s.trust_env = False          # ★ 直连：走代理会被 Cloudflare 挡
s.headers.update(UA)

for name, page in PAGES.items():
    try:
        r = s.get('https://apitrx.com/pages/' + page, timeout=40)
    except Exception as e:
        print('%-14s 失败 %s' % (name, e))
        continue
    if r.status_code != 200:
        print('%-14s HTTP %s' % (name, r.status_code))
        continue
    t = strip_html(r.text)
    with open(os.path.join(OUT, name + '.txt'), 'w', encoding='utf-8') as f:
        f.write(t)
    print('%-14s OK %d 字符' % (name, len(t)))
    time.sleep(1.5)          # 拉开点间隔，别被限流
