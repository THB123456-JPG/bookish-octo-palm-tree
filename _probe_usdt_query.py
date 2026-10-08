# -*- coding: utf-8 -*-
"""实地测一个地址的 USDT 查询（本机和服务器上都能跑）

用法：python _test_usdt_query.py [地址]
排查「明明有余额，机器人却报 0」用的。
"""
import sys
import traceback

sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, '.')

import runners.usdt as usdt                                    # noqa: E402

ADDR = sys.argv[1] if len(sys.argv) > 1 else 'TFMsoNbmzTEU7toDB8LvUhicPHhVQN1KWf'

print('地址：%s' % ADDR)
print('-' * 56)

t = usdt.Tron()
print('USDT 合约常量：%s' % t.USDT)
print('trust_env（True=会走系统代理）：%s' % getattr(t.session, 'trust_env', '?'))
print()

# ---- ① 拉原始响应，看服务端到底回了什么 ----
print('① 原始 HTTP 响应')
try:
    r = t.session.get('%s/v1/accounts/%s' % (t.BASE, ADDR), timeout=25)
    print('   HTTP %s' % r.status_code)
    print('   响应头 content-type：%s' % r.headers.get('content-type'))
    body = r.text
    print('   前 400 字：%s' % body[:400].replace('\n', ' '))
except Exception as e:
    print('   ★ 请求就失败了：%s: %s' % (type(e).__name__, e))
print()

# ---- ② 走代码里的 balance() ----
print('② Tron.balance()')
try:
    u, x = t.balance(ADDR)
    print('   USDT=%r  TRX=%r' % (u, x))
    print('   格式化后：USDT=%s  TRX=%s' % (usdt.fmt_amount(u), usdt.fmt_amount(x)))
except Exception as e:
    print('   ★ 抛异常：%s: %s' % (type(e).__name__, e))
    traceback.print_exc()
print()

# ---- ③ 转账记录 ----
print('③ Tron.transfers(limit=5)')
try:
    txs = t.transfers(ADDR, limit=5)
    print('   拿到 %d 条' % len(txs))
    for t2 in txs[:3]:
        print('     %s  %s USDT  %s → %s'
              % (t2.get('ts'), t2.get('amount'),
                 str(t2.get('from'))[:12], str(t2.get('to'))[:12]))
except Exception as e:
    print('   ★ 抛异常：%s: %s' % (type(e).__name__, e))
    traceback.print_exc()
