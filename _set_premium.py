# -*- coding: utf-8 -*-
"""配好会员：gift 域名的 key + USDT 定价（先不启用，等充了值再开）"""
import sys

import requests

sys.stdout.reconfigure(encoding='utf-8')

H = {'X-Panel-Pass': '换成你自己的'}
BASE = 'http://127.0.0.1:8080/api/shop/accd0cdc'
GIFT_KEY = '你的APIKEY'

r = requests.post(BASE + '/config', headers=H, json={
    'premium_key': GIFT_KEY,
    'premium_prices': {'3': 15.5, '6': 20.5, '12': 36.5},
}, timeout=25)
print('保存：', r.json())

c = requests.get(BASE + '/config', headers=H, timeout=25).json()['config']
print()
_k = (c.get('premium_key') or '').strip()
print('会员 key  :', '已填（长度 %d）' % len(_k))
print('计价方式  :', 'USDT（gift 域名，独立账户）' if _k else 'TRX（和能量共用）')
print('会员功能  :', '✅ 开' if c.get('premium_enabled') else '❌ 关（等充值后再开）')
print('套餐定价  :', ', '.join('%s个月 = %s USDT'
                              % (k, v) for k, v in
                              sorted((c.get('premium_prices') or {}).items())))
