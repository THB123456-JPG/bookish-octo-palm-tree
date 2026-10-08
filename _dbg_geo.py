# -*- coding: utf-8 -*-
"""试几个 IP 归属地接口，看哪个能用、能出中文城市"""
import sys

import requests

sys.stdout.reconfigure(encoding='utf-8')
IP = '114.114.114.114'

TESTS = [
    ('pconline', 'http://whois.pconline.com.cn/ipJson.jsp',
     {'ip': IP, 'json': 'true'}, 'gbk'),
    ('ip-api', 'http://ip-api.com/json/%s' % IP,
     {'lang': 'zh-CN'}, None),
    ('ip.sb', 'https://api.ip.sb/geoip/%s' % IP, {}, None),
    ('ipinfo', 'https://ipinfo.io/%s/json' % IP, {}, None),
]

for name, url, params, enc in TESTS:
    try:
        r = requests.get(url, params=params, timeout=8)
        if enc:
            r.encoding = enc
        print('=== %s ===' % name)
        print('   HTTP %s  %s' % (r.status_code, (r.text or '')[:220]))
    except Exception as e:
        print('=== %s ===' % name)
        print('   ❌ %s: %s' % (type(e).__name__, str(e)[:110]))
    print()
