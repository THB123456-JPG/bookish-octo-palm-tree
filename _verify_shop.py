# -*- coding: utf-8 -*-
"""收尾验证：商城机器人是否就绪、面板自检接口是否通"""
import sys

import requests

sys.stdout.reconfigure(encoding='utf-8')

H = {'X-Panel-Pass': '换成你自己的'}
PANEL = 'http://127.0.0.1:8080'

print('=' * 58)
print('1) 机器人列表')
print('=' * 58)
d = requests.get(PANEL + '/api/bots', headers=H, timeout=15).json()
shop = None
for b in d.get('bots') or []:
    print('  %-8s %-10s %-12s 状态=%-8s %s'
          % (b.get('id'), b.get('type_name') or b.get('type'),
             b.get('note'), b.get('status'),
             ('待处理 %s 笔' % b.get('stat')) if b.get('stat') else ''))
    if b.get('type') == 'shop':
        shop = b
if not shop:
    print('  ❌ 没有商城机器人')
    sys.exit(1)
print()
print('  商城机器人的状态字段：')
for k in ('id', 'note', 'username', 'enabled', 'status', 'expire_at', 'stat'):
    if k in shop:
        print('    %-10s = %s' % (k, shop[k]))

print()
print('=' * 58)
print('2) 商城配置（不回显 key）')
print('=' * 58)
d = requests.get('%s/api/shop/%s/config' % (PANEL, shop['id']),
                 headers=H, timeout=15).json()
c = d.get('config') or {}
print('  上游        :', c.get('provider'))
print('  key 已填    :', bool((c.get('provider_key') or '').strip()))
print('  单价        :', c.get('price'), 'TRX =', c.get('energy'), '能量')
print('  最高倍数    :', c.get('max'))
print('  总开关      :', c.get('enabled'))
print('  收款地址    :', c.get('trx_own'))
_kf = (c.get('contact') or '').strip()
if _kf:
    _u = _kf.lstrip('@').strip()
    _url = _kf if _kf.startswith('http') else 'https://t.me/%s' % _u
    print('  客服联系方式:', _kf, '→ 客户点按钮跳到', _url)
else:
    print('  客服联系方式: ⚠️ 没填，客户点「联系客服」只会看到一句提示')
print('  对外可用    :', d.get('ready'))
print()
print('  会员功能    :', '✅ 开' if c.get('premium_enabled')
      else '❌ 关（面板里能开）')
_pp = c.get('premium_prices') or {}
print('  会员价格    :', ', '.join('%s个月 = %s TRX' % (k, v)
                                   for k, v in sorted(_pp.items())))
print('  可选上游    :', ', '.join('%s(%s)' % (x['value'], x['label'])
                                 for x in (d.get('providers') or [])))

print()
print('=' * 58)
print('3) 上游自检（面板按钮走的接口）')
print('=' * 58)
r = requests.post('%s/api/shop/%s/providertest' % (PANEL, shop['id']),
                  headers=H, json={}, timeout=40)
j = r.json()
print('  HTTP %s → %s %s' % (r.status_code, '✅' if j.get('pass') else '❌',
                              j.get('msg') or j.get('error')))

print()
print('=' * 58)
print('4) 流水扫描状态')
print('=' * 58)
r = requests.get('%s/api/shop/%s/payments' % (PANEL, shop['id']),
                 headers=H, timeout=20)
j = r.json()
sc = j.get('scan') or {}
print('  收款地址    :', sc.get('pay_address'))
print('  扫描基线    :', '已建立' if sc.get('baseline') else '未建立')
print('  已记录 txid :', sc.get('seen_count'))
print('  今日汇总    :', j.get('today'))
print('  待处理      :', j.get('attention'))
rows = j.get('payments') or []
print('  最近流水    :')
for p in rows[:6]:
    print('    %s  %s TRX → %s 能量  %s  %s'
          % (p.get('at_str'), p.get('amount'), p.get('energy'),
             p.get('status_label'), (p.get('from') or '')[:14]))
if not rows:
    print('    （还没有流水）')
