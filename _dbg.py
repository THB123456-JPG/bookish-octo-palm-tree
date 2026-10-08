# -*- coding: utf-8 -*-
import os
import shutil
import sys

sys.stdout.reconfigure(encoding='utf-8')

TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_tmp_dbg')
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP)

from runners.ledger.commands import Actor, handle_text
from runners.ledger.storage import LedgerStore

st = LedgerStore(os.path.join(TMP, 'x.sqlite3'))
BOSS = Actor(111, 'boss', '群主')
MEMBER = Actor(222, 'zhangsan', '张三')
CHAT = -1001234567890


def T(text, actor=None, reply_user=None):
    return handle_text(store=st, chat_id=CHAT, actor=actor or BOSS, text=text,
                       owner_ids={111}, reply_user=reply_user,
                       message_id=1, reply_message_id=2)


T('+100 张三')
T('+200 李四')
T('下发 50')

print('entries 实际字段：')
for x in st.entries(CHAT):
    print('  kind=%-8r amount=%-12r note=%r' % (x.kind, x.amount, x.note))

print()
print('=' * 50)
print('「账单」实际返回：')
print('=' * 50)
print(T('账单').text)
print()
print('=' * 50)
print('「添加权限」（普通人）实际返回：')
print('=' * 50)
print(T('添加权限', actor=MEMBER, reply_user=MEMBER).text)

shutil.rmtree(TMP, ignore_errors=True)
