# -*- coding: utf-8 -*-
"""查上游给的交易哈希 —— 链上到底有没有真的把能量委托过来"""
import json
import sys

import requests

sys.stdout.reconfigure(encoding='utf-8')

TXID = '0f222bd6506185b50b629dbbd0c23bb5cf3996ef7d341a0a3119e97c55f87c8b'
BUYER = 'TUaZ5nGofnSFpoYB5DSfqpZBZAAbmuWzcF'

print('=' * 64)
print('上游给的单号：', TXID)
print('=' * 64)

r = requests.post('https://api.trongrid.io/wallet/gettransactionbyid',
                  json={'value': TXID}, timeout=25)
j = r.json()

if not j or not j.get('txID'):
    print()
    print('❌ 链上查不到这笔交易。')
    print('   说明这个「代理哈希」不是链上真实 txid（有些上游内部自己记账）。')
    print('   原始返回：', json.dumps(j, ensure_ascii=False)[:200])
    sys.exit(0)

print()
print('✅ 链上找到了这笔交易')
print('   时间戳   :', j.get('raw_data', {}).get('timestamp'))
c = (j.get('raw_data', {}).get('contract') or [{}])[0]
ctype = c.get('type')
val = (c.get('parameter') or {}).get('value') or {}
print('   合约类型 :', ctype)
print('   结果     :', j.get('ret', [{}])[0].get('contractRet'))
print()

if ctype == 'DelegateResourceContract':
    print('   ★ 这是一个「委托资源」交易 —— 能量确实发出去了')
    raw = val.get('balance')
    print('   委托数量 :', (int(raw) / 1e6) if raw else '?', 'TRX 等值')
    print('   资源类型 :', val.get('resource'))
    print('   接收地址 :', val.get('receiver_address'))
    print('   这是客户吗：', '✅ 是' if val.get('receiver_address')
          and BUYER[1:] else '需人工核对')
else:
    print('   ⚠️ 不是委托交易，实际类型是', ctype)
    print('   内容：', json.dumps(val, ensure_ascii=False)[:300])

print()
print('=' * 64)
print('Tronscan 链接（可以直接点开看）：')
print('  https://tronscan.org/#/transaction/%s' % TXID)
print('=' * 64)
