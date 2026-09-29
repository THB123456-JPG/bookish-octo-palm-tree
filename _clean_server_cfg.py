# -*- coding: utf-8 -*-
"""清掉服务器上从仓库模板带过来的占位数据

仓库里的 bots.json / merchants.json 是**脱敏后的模板**，里面留着
一条占位商户（用户名和密码哈希都是空的）。不清掉的话，商户列表里
会冒出一个名字叫「测试」、但根本登不进去的账号，看着莫名其妙。

★ 只打印「数量」，不打印任何内容 —— 这两个文件里是凭据类的东西。
"""
import json
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')
HERE = os.path.dirname(os.path.abspath(__file__))


def load(p, default):
    try:
        with open(p, encoding='utf-8') as f:
            d = json.load(f)
        return d if isinstance(d, dict) else default
    except (OSError, ValueError):
        return default


bots_p = os.path.join(HERE, 'bots.json')
merch_p = os.path.join(HERE, 'merchants.json')

b = load(bots_p, {})
bl = b.get('bots')
if not isinstance(bl, list):
    bl = []
print('机器人：%d 个' % len(bl))

m = load(merch_p, {})
ml = m.get('merchants')
if not isinstance(ml, list):
    ml = []
print('商户：%d 个' % len(ml))

changed = []
if ml:
    m['merchants'] = []
    with open(merch_p, 'w', encoding='utf-8') as f:
        json.dump(m, f, ensure_ascii=False, indent=2)
    changed.append('清掉了 %d 个占位商户' % len(ml))

# ★ bots.json：仓库模板里带着原项目的 5 个机器人（token 已经换成占位符了）。
#   不清掉的话面板会拿假 token 一直去连 Telegram、日志刷满报错，
#   列表里还挂着 5 个用不了的机器人。
#   ★★ 但**先确认每一个 token 都是占位符**再清 —— 万一里面有真的
#      （比如用户后来自己往服务器上加过），清掉就是事故。
PLACEHOLDER_MARKS = ('换成你自己的', '你的机器人token', '123456789:AA')
if not bl:
    print('bots.json 里没有机器人，规整一下就行')
else:
    real = []
    for bot in bl:
        tok = str(bot.get('token') or '')
        if tok and not any(mk in tok for mk in PLACEHOLDER_MARKS):
            # 只记名字，不打印 token
            real.append(str(bot.get('note') or bot.get('id') or '?'))
    if real:
        print('★ bots.json 里有【看起来是真的】token，**不动**：%s'
              % ', '.join(real))
        print('  （要是你确认这些没用，手动删或告诉我）')
        bl = None
    else:
        print('  5 个都是占位符 token，可以安全清掉')

if bl is not None and not bl:
    with open(bots_p, 'w', encoding='utf-8') as f:
        json.dump({'bots': []}, f, ensure_ascii=False, indent=2)
    changed.append('bots.json 规整为空列表')
elif bl:
    with open(bots_p, 'w', encoding='utf-8') as f:
        json.dump({'bots': []}, f, ensure_ascii=False, indent=2)
    changed.append('清掉了 %d 个占位机器人' % len(bl))

if changed:
    print('已处理：' + '；'.join(changed))
else:
    print('本来是干净的，没动')
