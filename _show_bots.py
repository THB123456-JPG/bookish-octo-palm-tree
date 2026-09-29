# -*- coding: utf-8 -*-
"""列一下本机机器人的类型 / 开关状态（排查用）

★ 只打印排查需要的字段，**绝不打印 token** —— 那是凭据。
"""
import json
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')

p = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'bots.json')
try:
    d = json.load(open(p, encoding='utf-8'))
except (OSError, ValueError) as e:
    print('读不到 bots.json：%s' % e)
    sys.exit(1)

bots = d.get('bots') or []
print('共 %d 个机器人' % len(bots))
print()
print('%-10s %-14s %-8s %-6s %-8s %-8s' % (
    'id', '备注', '类型', '启用', '记录开关', '已绑定'))
print('-' * 62)
for b in bots:
    arc = b.get('archive') or {}
    print('%-10s %-14s %-8s %-6s %-8s %-8s' % (
        b.get('id') or '?',
        (b.get('note') or '')[:12],
        b.get('type') or '?',
        '是' if b.get('enabled') else '否',
        '开' if arc.get('enabled') else '关',
        '是' if b.get('admin_ids') else '否'))
print()
print('★ 消息记录只对 type=ledger（记账机器人）开放 —— 其它类型不显示群消息')
