# -*- coding: utf-8 -*-
"""把面板密码改成参数里给的那个（只改 config.json）

用法：python _set_password.py 新密码

★ 只动 password 一个字段，别的配置原样保留。
★ 用完自己删掉，别留在服务器上。
"""
import io
import json
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')

p = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
pw = sys.argv[1] if len(sys.argv) > 1 else ''

if not pw:
    print('★ 没给密码')
    sys.exit(1)
# ★ 面板密码必须纯 ASCII —— 它要走 HTTP 头，中文会让 requests 报
#   UnicodeEncodeError（主程序里也有这个判断，会自动换掉）
if not pw.isascii() or not pw.isprintable():
    print('★ 密码必须是纯 ASCII 可打印字符（不能有中文）')
    sys.exit(1)

with io.open(p, encoding='utf-8') as f:
    d = json.load(f)

print('改之前长度：%d' % len(str(d.get('password') or '')))
d['password'] = pw
with io.open(p, 'w', encoding='utf-8') as f:
    json.dump(d, f, ensure_ascii=False, indent=2)
print('已改成 %d 位' % len(pw))
