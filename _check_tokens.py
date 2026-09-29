# -*- coding: utf-8 -*-
"""检查每个机器人的 token 还有没有效

★ 日志里一直刷「401 Unauthorized」就是 token 被废了 ——
  在 BotFather 里 Revoke 过、或者复制的时候少了一段。
  这种机器人谁也连不上，面板上看着「已启动」其实一直在报错。

★ 只打印「有效 / 无效」和**之前记录的用户名**，
  **绝不打印 token 本身**（那是凭据）。
"""
import io
import json
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, '.')

import requests                                     # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
d = json.load(io.open(os.path.join(HERE, 'bots.json'), encoding='utf-8'))

bad, ok = [], []
for b in d.get('bots') or []:
    tok = str(b.get('token') or '')
    note = b.get('note') or b.get('id')
    # ★ token 废了就连不上 getMe、拿不到实时用户名 ——
    #   用 bots.json 里**上次记下的**那个，帮用户认出来是哪个机器人
    was = b.get('username') or '(没记过)'
    if '换成你自己的' in tok or not tok or ':' not in tok:
        bad.append((note, b.get('id'), was, '还是个占位符没换'))
        continue
    try:
        r = requests.post('https://api.telegram.org/bot%s/getMe' % tok,
                          timeout=20).json()
    except Exception as e:
        bad.append((note, b.get('id'), was, '连不上：%s' % type(e).__name__))
        continue
    if r.get('ok'):
        ok.append((note, b.get('id'), r['result'].get('username') or ''))
    else:
        bad.append((note, b.get('id'), was,
                    r.get('description') or 'code=%s' % r.get('error_code')))

print('token 有效：%d 个' % len(ok))
for note, bid, u in ok:
    print('   ✅ %-12s @%s' % (note, u))
print()
print('token 有问题：%d 个' % len(bad))
for note, bid, was, why in bad:
    print('   ❌ 备注「%s」  @%s  (ID %s)' % (note, was, bid))
    print('        原因：%s' % why)
if bad:
    print()
    print('★ 这些机器人在面板上看着「已启动」，其实一个消息都收不到。')
    print('  @用户名是 bots.json 里**上次记下的**（token 废了就拿不到实时的），')
    print('  拿它去 BotFather 里对照着找。然后二选一：')
    print('    · 面板上点那行的「🔑 Token」换个新的')
    print('    · 不用了就在面板上删掉')
