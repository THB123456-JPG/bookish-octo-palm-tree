# -*- coding: utf-8 -*-
"""验证商城机器人的三个交互修复：
   ① 客户发 /start 不再回「不认识的指令」
   ② 文案里的 TRON 地址 → TRX 地址
   ③ 联系客服改成跳转按钮，不打印联系方式
"""
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')

import core
from runners.shop import ShopRunner

OK, BAD = [], []


def check(name, cond, extra=''):
    (OK if cond else BAD).append(name)
    print('  %s %s%s' % ('✅' if cond else '❌', name,
                         ('  → %s' % (extra,)) if extra else ''))


class FakeAPI:
    def __init__(self):
        self.calls = []

    def call(self, method, **params):
        self.calls.append((method, params))
        return {'ok': True, 'result': {'message_id': 1}}

    def last_text(self):
        return self.calls[-1][1].get('text', '') if self.calls else ''

    def last_kb(self):
        return self.calls[-1][1].get('reply_markup') if self.calls else None


class FakeMgr:
    def save(self):
        pass

    def is_expired(self, b):
        return False


BID = '_uitest'
BOT = {
    'id': BID, 'token': '0:fake', 'note': '界面测试',
    'admin_ids': [111], 'owner_id': 111,
    'shop': {
        'enabled': True,
        'trx_own': 'TBKw17Fxy5kBbev8MMZ16yVKuQTQfsETta',
        'price': 3.5, 'energy': 65000, 'max': 10,
        'contact': '@mykefu',
        'provider': 'mock',
    },
}

r = ShopRunner(FakeMgr(), BOT)
r.api = FakeAPI()

CUSTOMER = {'id': 222, 'first_name': '客户'}
ADMIN = {'id': 111, 'first_name': '老板'}

print('=' * 60)
print('① 客户发 /start（这是最要命的：第一次进来就发这个）')
print('=' * 60)
r.api.calls.clear()
r.handle_user({'from': CUSTOMER, 'text': '/start'}, is_admin=False)
t = r.api.last_text()
check('有回复', bool(t))
check('不再说「不认识的指令」', '不认识的指令' not in t, t[:40])
check('能看到用法说明', '怎么用' in t)
check('客户看不到运营指令', '运营指令' not in t)
check('回复带了底部菜单', r.api.last_kb() is not None)

r.api.calls.clear()
r.handle_user({'from': CUSTOMER, 'text': '/help'}, is_admin=False)
check('客户发 /help 也正常', '怎么用' in r.api.last_text()
      and '运营指令' not in r.api.last_text())

print()
print('  管理员发同样的指令：')
r.api.calls.clear()
r.handle_user({'from': ADMIN, 'text': '/start'}, is_admin=True)
t = r.api.last_text()
check('管理员能看到运营指令', '运营指令' in t)
check('管理员也能看到用法', '怎么用' in t)

print()
print('=' * 60)
print('② 文案用词：TRON 地址 → TRX 地址')
print('=' * 60)
# 把客户能点到的每个入口都走一遍，看回复里有没有 TRON
seen = []
for btn, flow in (('⚡ 充能量', None), ('📊 我的记录', 'addr'),
                  ('💲 查手续费', 'fee')):
    r.api.calls.clear()
    r.on_button(CUSTOMER['id'], btn)
    seen.append((btn, r.api.last_text()))
for btn, txt in seen:
    check('%s 的回复里没有 TRON' % btn, 'TRON' not in txt,
          txt.replace('\n', ' ')[:46])

# 直接发地址那条路（会走校验失败的提示）
r.api.calls.clear()
r._sess[CUSTOMER['id']] = {'flow': 'fee', 'at': 9e9}
r.handle_user({'from': CUSTOMER, 'text': 'abc'}, is_admin=False)
check('地址校验失败的提示里没有 TRON', 'TRON' not in r.api.last_text(),
      r.api.last_text()[:46])

print()
print('=' * 60)
print('③ 联系客服 = 跳转按钮')
print('=' * 60)
r.api.calls.clear()
r.flow_contact(CUSTOMER['id'])
kb = r.api.last_kb()
check('发了按钮', bool(kb))
rows = (kb or {}).get('inline_keyboard') or []
check('按钮是单行', len(rows) == 1 and len(rows[0]) == 1)
btn = rows[0][0] if rows and rows[0] else {}
check('按钮类型是 url 跳转', 'url' in btn, str(btn))
check('跳转地址正确', btn.get('url') == 'https://t.me/mykefu', btn.get('url'))
check('正文不再打印 @mykefu', 'mykefu' not in r.api.last_text(),
      r.api.last_text().replace('\n', ' ')[:46])

print()
print('  换了填法（带 @ / 完整链接 / 没填）：')
for val, want in (('@abc', 'https://t.me/abc'),
                  ('abc', 'https://t.me/abc'),
                  ('https://t.me/xyz', 'https://t.me/xyz')):
    r.bot['shop']['contact'] = val
    r.api.calls.clear()
    r.flow_contact(CUSTOMER['id'])
    got = ((r.api.last_kb() or {}).get('inline_keyboard') or [[{}]])[0][0].get('url')
    check('contact=%-20r → %s' % (val, want), got == want, got)

r.bot['shop']['contact'] = ''
r.api.calls.clear()
r.flow_contact(CUSTOMER['id'])
check('没填客服时给出提示，而且不发空按钮',
      'inline_keyboard' not in (r.api.last_kb() or {})
      and '还没填' in r.api.last_text(),
      r.api.last_text()[:40])

print()
print('=' * 60)
print('④ ★★ 等输入时点了别的菜单，别把按钮文字当成地址')
print('=' * 60)
# 用户点「🔢 剩余笔数」→ 机器人开始等地址
r.api.calls.clear()
r.on_button(CUSTOMER['id'], ShopRunner.BTN_REMAIN)
check('进入了等地址状态',
      (r._sess.get(CUSTOMER['id']) or {}).get('flow') == 'remain',
      str(r._sess.get(CUSTOMER['id'])))

# 用户发现点错了，改点「📞 联系客服」
r.bot['shop']['contact'] = '@mykefu'
r.api.calls.clear()
r.handle_user({'from': CUSTOMER, 'text': ShopRunner.BTN_HELP}, is_admin=False)
t = r.api.last_text()
check('★★ 没有说「这不是有效的地址」（这是用户报的 bug）',
      '不是有效' not in t, t.replace('\n', ' ')[:56])
check('★★ 按「联系客服」正常处理了 —— 发了跳转按钮',
      r.api.last_kb() is not None,
      str(r.api.last_kb())[:60])
check('★ 等待状态也取消了（不会卡着）',
      CUSTOMER['id'] not in r._sess, str(r._sess.get(CUSTOMER['id'])))

# 换了别的菜单也一样
for btn in (ShopRunner.BTN_BUY, ShopRunner.BTN_RATE,
            ShopRunner.BTN_AUTO, ShopRunner.BTN_REC):
    r._set_sess(CUSTOMER['id'], 'remain')
    r.api.calls.clear()
    r.handle_user({'from': CUSTOMER, 'text': btn}, is_admin=False)
    check('★ 等待中改点「%s」也正常' % btn,
          '不是有效' not in r.api.last_text(),
          r.api.last_text().replace('\n', ' ')[:40])

# /命令 也一样
r._set_sess(CUSTOMER['id'], 'remain')
r.api.calls.clear()
r.handle_user({'from': CUSTOMER, 'text': '/help'}, is_admin=False)
check('★ 等待中发 /help 也正常', '不是有效' not in r.api.last_text(),
      r.api.last_text().replace('\n', ' ')[:40])

# ★★ 但真的打了一串乱码，还是要报错 —— 不能因为修这个把校验也关了
r._set_sess(CUSTOMER['id'], 'remain')
r.api.calls.clear()
r.handle_user({'from': CUSTOMER, 'text': 'abc胡说八道'}, is_admin=False)
check('★★ 真打错地址 → 照旧提示（校验没被关掉）',
      '不是有效' in r.api.last_text(),
      r.api.last_text().replace('\n', ' ')[:50])

# 会员那个「填用户名」的步骤同理
r._set_sess(CUSTOMER['id'], 'puser', month='3')
r.api.calls.clear()
r.handle_user({'from': CUSTOMER, 'text': ShopRunner.BTN_HELP}, is_admin=False)
check('★★ 等填用户名时点菜单，也按菜单处理',
      '用户名看着不对' not in r.api.last_text()
      and r.api.last_kb() is not None,
      r.api.last_text().replace('\n', ' ')[:44])

# 清理测试数据文件
p = os.path.join(core.DATA_DIR, '%s.json' % BID)
if os.path.exists(p):
    os.remove(p)

print()
print('=' * 60)
print('结果：%d 项通过，%d 项失败' % (len(OK), len(BAD)))
for b in BAD:
    print('  ❌', b)
print('=' * 60)
sys.exit(1 if BAD else 0)
