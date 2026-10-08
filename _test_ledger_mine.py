# -*- coding: utf-8 -*-
"""记账机器人 · 「/我」——自己的加账明细

群里发 /我 → 只统计**自己**记的账（别人一条都不能露）
私聊发 /我 → 先选群（可多选、可全选）→ 统计所选群里自己的明细

★ 最要紧的一条：**只统计发指令的那个人**。串了就是把人家的账给别人看了。
"""
import os
import shutil
import sys
import time

sys.stdout.reconfigure(encoding='utf-8')

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = os.path.join(HERE, '_tmp_mine')
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(os.path.join(TMP, 'data'), exist_ok=True)

import core                                    # noqa: E402
core.BASE_DIR = TMP
core.DATA_DIR = os.path.join(TMP, 'data')

from runners.ledger import LedgerRunner        # noqa: E402

OK, BAD = [], []


def check(name, cond, extra=''):
    (OK if cond else BAD).append(name)
    print('  %s %s%s' % ('✅' if cond else '❌', name,
                         ('  → %s' % (extra,)) if extra else ''))


class FakeAPI:
    def __init__(self):
        self.calls = []

    def call(self, method, **p):
        self.calls.append((method, p))
        return {'message_id': len(self.calls)}

    def call_file(self, method, field, fn, obj, **p):
        self.calls.append((method, p))
        return {'message_id': len(self.calls)}

    def last(self):
        return self.calls[-1][1] if self.calls else {}

    def all_text(self):
        return '\n'.join(c[1].get('text') or '' for c in self.calls)

    def last_kb(self):
        return (self.last().get('reply_markup') or {}).get('inline_keyboard') or []


class FakeMgr:
    def save(self):
        pass

    def is_expired(self, b):
        return False


G1 = -1001111111111
G2 = -1002222222222
BOSS = 111          # 主人
OP = 222            # 操作员（只在 G1）
USER = 333          # 普通群成员
OTHER = 444         # 另一个普通群成员
BOT = {'id': 'minetest', 'token': '1:FAKE', 'note': '我的测试',
       'admin_ids': [BOSS], 'owner_id': BOSS, 'enabled': True}

_LIVE = []


def mk():
    r = LedgerRunner(FakeMgr(), dict(BOT))
    r.api = FakeAPI()
    _LIVE.append(r)
    return r


def mk_with_op():
    """建 runner 并把 OP 设成 G1 的操作员 ——
    ★ 权限是**按群**授的，不设的话私聊 /我 会被正确地拒掉"""
    r = mk()
    r.store.add_operator(G1, OP, 'u222', '操作员', BOSS)
    return r


def cleanup():
    for r in _LIVE:
        try:
            r.close_db()
        except Exception:
            pass
    _LIVE.clear()
    shutil.rmtree(TMP, ignore_errors=True)


_MID = [1000]        # ★ 消息 id 必须**全局唯一**：账本对「同一个群的同一条
                     #   消息」有防重复（ledger_entry_messages 主键），
                     #   各小节都从 1 开始编的话，第 4 节的 (群,1) 在第 1 节
                     #   就用过了 → 那笔账**根本没记进去**，测试白跑


def upd(text, uid, chat_id, chat_type='supergroup', mid=None, uname='',
        reply=None):
    _MID[0] += 1
    mid = _MID[0]
    m = {'update_id': mid, 'message': {
        'message_id': mid, 'date': int(time.time()),
        'chat': {'id': chat_id, 'type': chat_type,
                 'title': '群%s' % chat_id},
        'from': {'id': uid, 'first_name': '用户%d' % (uid % 100),
                 'username': uname or ('u%d' % uid), 'is_bot': False},
        'text': text}}
    if reply:
        m['message']['reply_to_message'] = reply
    return m


def cq(data, uid=OP, mid=9):
    return {'id': 'cb1', 'data': data, 'from': {'id': uid},
            'message': {'message_id': mid,
                        'chat': {'id': uid, 'type': 'private'}}}


print('=' * 62)
print('一、★ 群里发 /我 —— 只统计**自己**的账')
print('=' * 62)
r = mk()
# 三个人在同一个群里各记各的账
r.handle(upd('+100 甲的账', USER, G1, mid=1))
r.handle(upd('+200 乙的账', OTHER, G1, mid=2))
r.handle(upd('+300 甲的第二笔', USER, G1, mid=3))
r.api.calls.clear()
r.handle(upd('/我', USER, G1, mid=4))
t = r.api.all_text()
check('★ 群里发 /我 有反应', bool(r.api.calls), '%d 次调用' % len(r.api.calls))
check('★★ 只看到自己的两笔（甲）', '甲的账' in t and '甲的第二笔' in t,
      t[:110].replace('\n', ' '))
check('★★★ **看不到别人的**（乙的那笔一条都不能露）',
      '乙的账' not in t and '200' not in t, t[:110].replace('\n', ' '))
check('★ 抬头带昵称和用户名',
      ('用户33' in t or 'u333' in t), t[:60].replace('\n', ' '))
check('★ 有小计', '加分 +' in t and '共 2 笔' in t, t[-60:].replace('\n', ' '))
# ★★ 用户 2026-09-29 要求：明细里要能看到**怎么算出来的**
check('★★ 带计算过程（金额/汇率=U，跟账单里一个格式）',
      '100/1=100U' in t, t[:120].replace('\n', ' '))
check('★★ 每条后面**不再挂操作人昵称**（抬头已经写了，重复啰嗦）',
      t.count('用户33') <= 1 and t.count('u333') <= 1,
      '用户33 出现 %d 次' % t.count('用户33'))

print()
print('=' * 62)
print('一之二、★★ 每条后面**不能再挂名字**（用户截图里点名的）')
print('=' * 62)
# 账本有个自动规则：**没写备注时会把发言人的名字填进 note**（见
# _entry_attribution）。所以「名字」会以 note 的形式出现 —— 光藏
# operator_name 没用，得按「note == 操作人名就藏掉」来判。
r = mk()
r.handle(upd('+100', USER, G1, mid=1))          # 不带备注 → note 自动填名字
r.api.calls.clear()
r.handle(upd('/我', USER, G1, mid=2))
t = r.api.all_text()
check('★★★ 自动填的名字**不显示**（备注 == 操作人名 → 藏掉）',
      t.count('用户33') <= 1 and t.count('u333') <= 1,
      '「用户33」出现 %d 次 → %s'
      % (t.count('用户33'), t[:100].replace('\n', ' ')))
check('★ 但计算过程还在', '/1=' in t, t[:110].replace('\n', ' '))
# 真备注要留着
r.handle(upd('+200 张三的账', USER, G1, mid=3))
r.api.calls.clear()
r.handle(upd('/我', USER, G1, mid=4))
t = r.api.all_text()
check('★★ 用户自己写的备注**要留着**（那是信息，不是昵称）',
      '张三的账' in t, t[:130].replace('\n', ' '))
# 回复某人记的账：note 变成被回复的人 —— 也要留着（那是有意义的归属）
r.handle(upd('+50', USER, G1, mid=5,
             reply={'message_id': 99, 'text': 'x',
                    'from': {'id': 888, 'first_name': '李', 'last_name': '四',
                             'username': 'lisi'}}))
r.api.calls.clear()
r.handle(upd('/我', USER, G1, mid=6))
t = r.api.all_text()
check('★ 回复记账的归属名（李 四）也留着', '李 四' in t or 'lisi' in t,
      t[-140:].replace('\n', ' '))

print()
print('=' * 62)
print('二、★ 换个人发 /我 → 只看得到他自己的')
print('=' * 62)
r.api.calls.clear()
r.handle(upd('/我', OTHER, G1, mid=5))
t = r.api.all_text()
check('★★ 乙只看到自己那笔', '乙的账' in t and '甲的账' not in t,
      t[:90].replace('\n', ' '))
check('★ 而且只有 1 笔', '共 1 笔' in t, t[-40:].replace('\n', ' '))

print()
print('=' * 62)
print('三、★ 没记过账的人发 /我')
print('=' * 62)
r.api.calls.clear()
r.handle(upd('/我', 999, G1, mid=6))
t = r.api.all_text()
check('★ 明确说「还没有你的记账记录」', '还没有你的记账记录' in t,
      t[:60].replace('\n', ' '))

print()
print('=' * 62)
print('四、★ 私聊 /我 —— 先弹群选择（可多选、可全选）')
print('=' * 62)
r = mk_with_op()
# 在两个群里都产生记录（也让机器人记住这两个群）
r.handle(upd('+100 一号群的账', OP, G1, mid=1))
r.handle(upd('+500 二号群的账', OP, G2, mid=2))
r.api.calls.clear()
r.handle(upd('/我', OP, OP, chat_type='private', mid=3))
t = r.api.all_text()
kb = r.api.last_kb()
labels = [b['text'] for row in kb for b in row]
check('★ 私聊发 /我 弹出群列表', bool(kb), '%d 个按钮' % len(labels))
check('★ 两个群都在，带方框（未选中）',
      sum(1 for x in labels if x.startswith('□')) == 2, str(labels))
check('★★ 有「全选」（用户特别要求的）',
      any('全选' in x for x in labels), str(labels))
check('★ 有下一步/取消', any('下一步' in x for x in labels)
      and any('取消' in x for x in labels), str(labels))
check('★ 还没统计就先别发明细', '一号群的账' not in t, t[:50])

print()
print('=' * 62)
print('五、★ 勾选 / 全选 / 清空')
print('=' * 62)
r.api.calls.clear()
r.on_callback(cq('mine:toggle:%d' % G1, uid=OP))
kb = r.api.last_kb()
check('★ 勾一下变成打勾（√）',
      any(b['text'].startswith('√') for row in kb for b in row),
      str([b['text'] for row in kb for b in row]))
r.api.calls.clear()
r.on_callback(cq('mine:all', uid=OP))
kb = r.api.last_kb()
check('★★ 点「全选」两个群都打上勾',
      sum(1 for row in kb for b in row if b['text'].startswith('√')) == 2,
      str([b['text'] for row in kb for b in row]))
check('★ 提示里说清了选了几个', '已选 2 个群' in r.api.all_text(),
      r.api.all_text()[:40].replace('\n', ' '))
r.api.calls.clear()
r.on_callback(cq('mine:none', uid=OP))
kb = r.api.last_kb()
check('★ 点「清空」全不选',
      not any(b['text'].startswith('√') for row in kb for b in row))

print()
print('=' * 62)
print('六、★ 统计：只算选中的群')
print('=' * 62)
r.api.calls.clear()
r.on_callback(cq('mine:toggle:%d' % G1, uid=OP))     # 只选一号群
r.api.calls.clear()
r.on_callback(cq('mine:next', uid=OP))
t = r.api.all_text()
check('★★ 只统计选中的那个群（一号群）', '一号群的账' in t,
      t[:110].replace('\n', ' '))
check('★★ 没选的群**一条都不出现**（二号群）', '二号群的账' not in t,
      t[:110].replace('\n', ' '))
check('★ 结果里带群名', '群%d' % G1 in t, t[:90].replace('\n', ' '))
check('★ 有合计', '合计' in t, t[-60:].replace('\n', ' '))

print()
print('=' * 62)
print('七、★ 两个群都选 → 两份明细 + 总计')
print('=' * 62)
r.api.calls.clear()
r.handle(upd('/我', OP, OP, chat_type='private', mid=4))
r.api.calls.clear()
r.on_callback(cq('mine:all', uid=OP))
r.api.calls.clear()
r.on_callback(cq('mine:next', uid=OP))
t = r.api.all_text()
check('★★ 两个群的明细都在', '一号群的账' in t and '二号群的账' in t,
      t[:140].replace('\n', ' '))
check('★ 每个群一段（带群名）',
      t.count('【') >= 2, '【 出现 %d 次' % t.count('【'))
check('★ 最后有总计', '合计 2 笔' in t, t[-70:].replace('\n', ' '))

print()
print('=' * 62)
print('八、★ 没选群就点统计 / 取消')
print('=' * 62)
r.api.calls.clear()
r.handle(upd('/我', OP, OP, chat_type='private', mid=5))
r.api.calls.clear()
r.on_callback(cq('mine:next', uid=OP))
check('★ 一个都没选 → 提示至少选一个',
      '至少选一个' in r.api.all_text(), r.api.all_text()[:40])
r.api.calls.clear()
r.on_callback(cq('mine:cancel', uid=OP))
check('★ 取消能取消掉', '已取消' in r.api.all_text(), r.api.all_text()[:30])
check('★ 取消后菜单状态清掉了', OP not in r._mine)

print()
print('=' * 62)
print('九、★ 权限：主人 / 操作员 / 普通人')
print('=' * 62)
r = mk()
r.handle(upd('+100 操作员的账', OP, G1, mid=1))
# 把 OP 设成 G1 的操作员
r.store.add_operator(G1, OP, 'u222', '操作员', 1)
r.api.calls.clear()
r.handle(upd('/我', OP, OP, chat_type='private', mid=2))
check('★ 操作员能用（权限是按群授的，私聊里要认）',
      '要统计哪些群' in r.api.all_text() or bool(r.api.last_kb()),
      r.api.all_text()[:40].replace('\n', ' '))
r.api.calls.clear()
r.handle(upd('/我', 999, 999, chat_type='private', mid=3))
check('★★ 普通人私聊 /我 → 被拒',
      '只有机器人主人或操作员' in r.api.all_text(),
      r.api.all_text()[:40])
check('★ 而且没弹出群列表', not r.api.last_kb())
r.api.calls.clear()
r.handle(upd('/我', BOSS, BOSS, chat_type='private', mid=4))
check('★ 机器人主人能用', bool(r.api.last_kb()),
      '%d 个按钮' % len(r.api.last_kb()))

print()
print('=' * 62)
print('十、★ 别把别的功能搞坏')
print('=' * 62)
r = mk()
G3 = -1003333333333          # ★ 单独开一个群 —— 各小节共用同一个库，
                             #   用 G1 的话前面几节的账会累加进来
r.api.calls.clear()
r.handle(upd('+100 正常记账', USER, G3, mid=1))
check('★ 正常记账照旧', len(r.store.entries(G3)) == 1,
      '%d 笔' % len(r.store.entries(G3)))
r.api.calls.clear()
r.handle(upd('账单', USER, G3, mid=2))
check('★ 「账单」还是全群账单（不是 /我 那种）',
      '账单' in r.api.all_text(), r.api.all_text()[:40].replace('\n', ' '))
r.api.calls.clear()
r.handle(upd('我', USER, G3, mid=3))       # 只发一个「我」字
check('★ 只发「我」不算 /我（别把正常聊天吃掉）',
      '加账明细' not in r.api.all_text(), r.api.all_text()[:40].replace('\n', ' '))
r.api.calls.clear()
r.on_callback(cq('nope:x', uid=OP))
check('★ 不认识的按钮前缀 → 不处理', not r.api.calls)

cleanup()
print()
print('=' * 62)
print('结果：%d 项通过，%d 项失败' % (len(OK), len(BAD)))
for b in BAD:
    print('  ❌', b)
print('=' * 62)
sys.exit(1 if BAD else 0)
