# -*- coding: utf-8 -*-
"""记账机器人「群管理」测试 —— 清理消息 / @全体 / 广播 / 入群欢迎

不联网、不真起线程：FakeAPI 记下每次调用，用真 sqlite（临时目录）。
"""
import json
import os
import shutil
import sys
import time

sys.stdout.reconfigure(encoding='utf-8')

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = os.path.join(HERE, '_tmp_lgr_group')
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(os.path.join(TMP, 'data'), exist_ok=True)

import core
core.BASE_DIR = TMP
core.DATA_DIR = os.path.join(TMP, 'data')

from runners.ledger import group_admin as G
from runners.ledger.positions import GroupPositions
from runners.ledger import LedgerRunner

OK, BAD = [], []


def check(name, cond, extra=''):
    (OK if cond else BAD).append(name)
    print('  %s %s%s' % ('✅' if cond else '❌', name,
                         ('  → %s' % (extra,)) if extra else ''))


class FakeAPI:
    """记下每次调用；可以安排某些方法抛错"""
    def __init__(self):
        self.calls = []
        self.fail = {}          # {method: 异常文本}
        self.deleted = []       # 收集被删的 id
        # getChatMember 的默认返回：是管理员且有删消息权限
        self.member = {'status': 'administrator', 'can_delete_messages': True}

    def call(self, method, **p):
        self.calls.append((method, p))
        if method in self.fail:
            raise core.TgError(self.fail[method])
        if method == 'getChatMember':
            return dict(self.member)
        if method == 'deleteMessages':
            ids = p.get('message_ids')
            self.deleted.extend(json.loads(ids) if isinstance(ids, str) else ids)
        return {'message_id': len(self.calls)}

    def call_file(self, method, field, fn, obj, **p):
        self.calls.append((method, p))
        return {'message_id': len(self.calls)}

    def methods(self):
        return [c[0] for c in self.calls]

    def last(self):
        return self.calls[-1][1] if self.calls else {}

    def last_of(self, method):
        for m, p in reversed(self.calls):
            if m == method:
                return p
        return {}

    def reset(self):
        self.calls, self.deleted = [], []


class FakeMgr:
    def save(self):
        pass

    def is_expired(self, b):
        return False


CHAT = -1001234567890
BOSS = 111
MEMBER = 222
BOT_ID = 999
BOT = {'id': 'ledgergrp', 'token': '1:FAKE', 'note': '记账测试',
       'admin_ids': [BOSS], 'owner_id': BOSS, 'enabled': True}


_seq = [0]
_LIVE = []          # 建过的 runner，收尾时要先关库


def mk(bot=None):
    """★ 每次都换一个 bot id —— 记账的库是按 bot id 命名的，
       不换的话各段测试共用同一个 sqlite，前面写的群/群主会串到后面"""
    _seq[0] += 1
    b = dict(bot or BOT)
    if bot is None:
        b['id'] = 'ledgergrp%d' % _seq[0]
    r = LedgerRunner(FakeMgr(), b)
    r.api = FakeAPI()
    r.me = {'id': BOT_ID, 'username': 'testbot'}
    _LIVE.append(r)
    return r


def cleanup():
    """★ 必须先关 sqlite 再删目录 —— Windows 上文件被占着删不掉，
       而 rmtree(ignore_errors=True) 会把失败**静默吞掉**，
       结果就是每跑一次留一个临时目录"""
    for r in _LIVE:
        try:
            r.close_db()
        except Exception:
            pass
    _LIVE.clear()
    shutil.rmtree(TMP, ignore_errors=True)


def upd(text, uid=MEMBER, chat_type='supergroup', chat_id=CHAT, mid=1,
        extra=None):
    m = {'message_id': mid, 'date': int(time.time()),
         'chat': {'id': chat_id, 'type': chat_type, 'title': '测试群'},
         'from': {'id': uid, 'first_name': '张', 'last_name': '三',
                  'username': 'zhangsan'},
         'text': text}
    if extra:
        m.update(extra)
    return {'update_id': mid, 'message': m}


print('=' * 62)
print('一、纯函数：权限判断')
print('=' * 62)
check('群主能删', G.can_delete_group_messages({'status': 'creator'}, 'supergroup'))
check('超级群管理员有 can_delete_messages 才能删',
      G.can_delete_group_messages(
          {'status': 'administrator', 'can_delete_messages': True}, 'supergroup'))
check('★ 超级群管理员没这个权限就不能删',
      G.can_delete_group_messages(
          {'status': 'administrator', 'can_delete_messages': False}, 'supergroup')
      is False)
check('普通群(group)管理员不用额外权限',
      G.can_delete_group_messages({'status': 'administrator'}, 'group'))
check('普通成员不能删', G.can_delete_group_messages({'status': 'member'}, 'group') is False)
check('拿不到成员信息 → False', G.can_delete_group_messages(None, 'group') is False)

print()
print('=' * 62)
print('二、纯函数：文本处理')
print('=' * 62)
check('「通知所有人」不带内容', G.extract_notify_all_text('通知所有人') == '')
check('「通知所有人 开会」取到内容',
      G.extract_notify_all_text('通知所有人 开会') == '开会')
check('@全体 也认', G.extract_notify_all_text('@全体 速回') == '速回')
check('/at_all 也认', G.extract_notify_all_text('/at_all  hi') == 'hi')
check('无关文本 → 空', G.extract_notify_all_text('+100') == '')
check('分块 50 一组', [len(x) for x in G.chunked(list(range(120)), 50)] == [50, 50, 20])
check('bounded_lines 超长会截断',
      '……内容较多' in G.bounded_lines(['x' * 100] * 50, limit=300))

print()
print('=' * 62)
print('三、纯函数：@ 人（有用户名 / 没用户名两条路）')
print('=' * 62)
m1 = G.mention_html({'user_id': 1, 'username': 'abc', 'display_name': '甲'})
check('有用户名 → @abc', m1 == '@abc', m1)
m2 = G.mention_html({'user_id': 42, 'username': '', 'display_name': '乙<b>'})
check('没用户名 → tg://user?id= 隐藏链接', 'tg://user?id=42' in m2, m2)
check('★ 名字里的 < 被转义了（否则整条消息被 TG 拒收）', '<b>' not in m2, m2)
m3 = G.mention_html({'user_id': 1, 'username': '@abc', 'display_name': '甲'})
check('用户名带 @ 不会变成 @@', m3 == '@abc', m3)
check('新成员 @ 用 t.me 链接',
      't.me/zhang' in G.member_mention_html(
          {'id': 7, 'first_name': '张', 'username': 'zhang'}))

print()
print('=' * 62)
print('四、删消息：能跳过删不掉的，不整批失败')
print('=' * 62)
api = FakeAPI()
G.delete_batch(api, CHAT, [9, 8, 7], 'supergroup', BOT_ID)
check('一次删 3 条', sorted(api.deleted) == [7, 8, 9], str(api.deleted))

api = FakeAPI()
api.fail['deleteMessages'] = "400 Bad Request: message can't be deleted"
api.fail['getChatMember'] = 'ignored'      # 权限查询失败会被吞掉
G.delete_batch(api, CHAT, [5], 'supergroup', BOT_ID)
check('★ 单条删不掉不会抛异常（48 小时限制）', True)

api = FakeAPI()
api.fail['deleteMessages'] = '400 Bad Request: message to delete not found'
G.delete_batch(api, CHAT, [1, 2, 3, 4], 'supergroup', None)
check('多条的会二分拆开重试',
      api.methods().count('deleteMessages') >= 3,
      '%d 次' % api.methods().count('deleteMessages'))

api = FakeAPI()
api.fail['deleteMessages'] = '400 Bad Request: CHAT_WRITE_FORBIDDEN'
try:
    G.delete_batch(api, CHAT, [1, 2], 'supergroup', BOT_ID)
    check('⚠️ 真错误应该抛出去（不该被当成「跳过」）', False)
except core.TgError:
    check('★ 真错误（不是删不掉）会抛出去', True)

print()
print('=' * 62)
print('五、删消息：整段 range + 中途来新消息')
print('=' * 62)
api = FakeAPI()
G.delete_range(api, CHAT, 1, 250, 'supergroup', BOT_ID)
check('250 条全删到', len(api.deleted) == 250, str(len(api.deleted)))
check('从大到小删', api.deleted[0] == 250 and api.deleted[-1] == 1, str(api.deleted[:3]))

api = FakeAPI()
calls = {'n': 0}


def latest_growing():
    """第一次问返回 250，之后返回 300 —— 模拟删到一半群里又来消息"""
    calls['n'] += 1
    return 250 if calls['n'] == 1 else 300


G.delete_range(api, CHAT, 1, 250, 'supergroup', BOT_ID, latest=latest_growing)
check('★ 清理期间新来的消息也会被带上', 300 in api.deleted,
      '最大删到 %s' % max(api.deleted))

api = FakeAPI()
seen = []
G.delete_range(api, CHAT, 1, 250, 'supergroup', BOT_ID,
               on_progress=seen.append, stop=lambda: len(seen) >= 1)
check('stop() 之后会停下来', len(seen) <= 1 and len(api.deleted) <= 100,
      '删了 %d 条，进度回调 %d 次' % (len(api.deleted), len(seen)))

print()
print('=' * 62)
print('六、群消息位置库')
print('=' * 62)
p = GroupPositions(os.path.join(TMP, 'pos.sqlite3'))
p.record(CHAT, 100, '群A')
check('记下了 100', p.latest(CHAT) == 100)
p.record(CHAT, 50, '群A')
check('小 id 不会把记录改小', p.latest(CHAT) == 100)
p.record(CHAT, 300, '群A')
check('大 id 会更新', p.latest(CHAT) == 300)
check('没见过的群返回 0', p.latest(-1) == 0)
p.record(12345, 99, '私聊')
check('★ 正数 id（私聊）不记录', p.latest(12345) == 0)
p.forget(CHAT)
check('forget 之后清零', p.latest(CHAT) == 0)
p.close()

print()
print('=' * 62)
print('七、入群 / 退群 / 群升级')
print('=' * 62)
def upd_full(m, uid=1):
    return {'update_id': uid, 'message': m}


r = mk()
r.api.reset()
r.handle(upd_full({
    'message_id': 1, 'chat': {'id': CHAT, 'type': 'supergroup', 'title': '测试群'},
    'from': {'id': MEMBER, 'first_name': '张'},
    'new_chat_members': [{'id': 555, 'first_name': '新', 'username': 'newbie',
                          'is_bot': False}]}))
t = r.api.last_of('sendMessage').get('text') or ''
check('新人进群有欢迎', '欢迎' in t and 'newbie' in t, t[:50])

r = mk()
r.api.reset()
r.handle(upd_full({
    'message_id': 1, 'chat': {'id': CHAT, 'type': 'supergroup'},
    'from': {'id': MEMBER, 'first_name': '张'},
    'new_chat_members': [{'id': BOT_ID, 'first_name': 'bot', 'is_bot': True}]}))
check('★ 机器人自己被拉进群时不欢迎自己',
      '欢迎' not in (r.api.last_of('sendMessage').get('text') or ''))
check('★ 拉机器人的人成了群主人',
      r.store.get_chat_owner_id(CHAT) == MEMBER,
      str(r.store.get_chat_owner_id(CHAT)))

r = mk()
r.api.reset()
r.handle(upd_full({
    'message_id': 1, 'chat': {'id': CHAT, 'type': 'supergroup'},
    'from': {'id': MEMBER, 'first_name': '张'},
    'left_chat_member': {'id': 555, 'first_name': '走', 'username': 'gone'}}))
check('有人退群有送别', '离开' in (r.api.last_of('sendMessage').get('text') or ''))

r = mk()
r.store.remember_bot_chat(CHAT, '老群', 'supergroup')
r.handle(upd_full({
    'message_id': 1, 'chat': {'id': CHAT, 'type': 'supergroup'},
    'from': {'id': MEMBER, 'first_name': '张'},
    'migrate_to_chat_id': -1009999}))
rows = [dict(x) for x in r.store.list_active_bot_groups()]
check('★ 群升级后老 id 不再活跃',
      CHAT not in [int(x['chat_id']) for x in rows],
      str([int(x['chat_id']) for x in rows]))

print()
print('=' * 62)
print('八、机器人被拉进群 / 踢出群（my_chat_member）')
print('=' * 62)
r = mk()
r.api.reset()
r.handle({'update_id': 1, 'my_chat_member': {
    'chat': {'id': CHAT, 'type': 'supergroup', 'title': '新群'},
    'from': {'id': BOSS, 'first_name': '老板'},
    'old_chat_member': {'status': 'left'},
    'new_chat_member': {'status': 'member'}}})
check('被拉进群 → 发欢迎',
      '已加入本群' in (r.api.last_of('sendMessage').get('text') or ''),
      (r.api.last_of('sendMessage').get('text') or '')[:40])
check('★ 拉了机器人的人成为群主人', r.store.get_chat_owner_id(CHAT) == BOSS)
check('群被记进活跃列表',
      CHAT in [int(x['chat_id']) for x in r.store.list_active_bot_groups()])
kb = r.api.last_of('sendMessage').get('reply_markup') or {}
check('欢迎语带「使用说明」按钮',
      any(b.get('callback_data') == 'ledger:help'
          for row in (kb.get('inline_keyboard') or []) for b in row), str(kb))

r.api.reset()
r._welcome_at[CHAT] = time.monotonic()
r.handle({'update_id': 2, 'my_chat_member': {
    'chat': {'id': CHAT, 'type': 'supergroup', 'title': '新群'},
    'from': {'id': BOSS, 'first_name': '老板'},
    'old_chat_member': {'status': 'left'},
    'new_chat_member': {'status': 'member'}}})
check('★ 反复拉进拉出不会刷屏（5分钟冷却）',
      'sendMessage' not in r.api.methods(), str(r.api.methods()))

r.api.reset()
r.handle({'update_id': 3, 'my_chat_member': {
    'chat': {'id': CHAT, 'type': 'supergroup', 'title': '新群'},
    'from': {'id': BOSS, 'first_name': '老板'},
    'old_chat_member': {'status': 'member'},
    'new_chat_member': {'status': 'kicked'}}})
check('被踢出群后不再广播给它',
      CHAT not in [int(x['chat_id']) for x in r.store.list_active_bot_groups()])

r.api.reset()
r.handle({'update_id': 4, 'my_chat_member': {
    'chat': {k: v for k, v in [('id', CHAT), ('type', 'supergroup'),
                               ('title', '群')]},
    'from': {'id': BOSS},
    'old_chat_member': {'status': 'administrator'},
    'new_chat_member': {'status': 'administrator'}}})
check('只是管理员权限变了不发欢迎（不是新入群）',
      'sendMessage' not in r.api.methods(), str(r.api.methods()))

print()
print('=' * 62)
print('九、@全体')
print('=' * 62)
r = mk()
for i in range(3):
    r.handle(upd('+10 甲%d' % i, uid=1000 + i, mid=i + 1))
r.api.reset()
r.handle(upd('通知所有人 今晚结算', uid=BOSS, mid=20))
t = r.api.last_of('sendMessage').get('text') or ''
check('@全体发出去了', '通知所有人' in t and '今晚结算' in t, t[:60])
check('★ 用 HTML（不然 @ 不生效）',
      r.api.last_of('sendMessage').get('parse_mode') == 'HTML')

r.api.reset()
r.handle(upd('通知所有人 再来一次', uid=BOSS, mid=21))
t = r.api.last_of('sendMessage').get('text') or ''
check('★ 5 分钟冷却生效', '冷却' in t, t[:40])

r.api.reset()
r.handle(upd('通知所有人 我要发', uid=MEMBER, mid=22))
t = r.api.last_of('sendMessage').get('text') or ''
check('★ 普通成员不能用 @全体', '无权限' in t, t[:40])

print()
print('=' * 62)
print('十、群成员统计')
print('=' * 62)
r = mk()
r.handle(upd('+10 甲', uid=1001, mid=1))
r.api.reset()
r.handle(upd('群成员', mid=2))
t = r.api.last_of('sendMessage').get('text') or ''
check('成员统计出得来', '缓存人数' in t, t[:60])

print()
print('=' * 62)
print('十一、清理消息（群里直接 /del）')
print('=' * 62)
r = mk()
r.handle(upd('+10 甲', mid=1))
r.handle(upd('+10 乙', mid=2))       # 顺便把位置刷到 2
r.api.reset()
r.handle(upd('/del', uid=BOSS, mid=3))
deadline = time.time() + 5
while CHAT in r._cleanup_busy and time.time() < deadline:
    time.sleep(0.1)
check('★ /del 真的删了消息', len(r.api.deleted) >= 2, '删了 %d 条' % len(r.api.deleted))
check('删到的最新 id 记下来了', r._cleanup_done.get(CHAT, 0) >= 2,
      str(r._cleanup_done.get(CHAT)))
check('★ 收消息的线程没被卡住（清理走后台线程）', r.api.methods().count('sendMessage') < 5)

r = mk()
r.handle(upd('+10 甲', mid=1))
r.api.reset()
r.handle(upd('/del', uid=MEMBER, mid=2))
check('★ 普通成员不能清理', '只有群主' in (r.api.last_of('sendMessage').get('text') or ''))

print()
print('=' * 62)
print('十二、清理菜单（私聊 /del）')
print('=' * 62)
r = mk()
r.handle(upd_full({
    'message_id': 1, 'date': int(time.time()),
    'chat': {'id': CHAT, 'type': 'supergroup', 'title': '群A'},
    'from': {'id': BOSS, 'first_name': '老板'}, 'text': '+10 甲'}))
r.api.reset()
r.handle(upd_full({
    'message_id': 2, 'date': int(time.time()),
    'chat': {'id': BOSS, 'type': 'private'},
    'from': {'id': BOSS, 'first_name': '老板'}, 'text': '/del'}))
deadline = time.time() + 5
while BOSS not in r._cl and time.time() < deadline:
    time.sleep(0.1)
check('私聊 /del 出菜单', BOSS in r._cl, str(list(r._cl)))
if BOSS in r._cl:
    kb = r.api.last_of('editMessageText').get('reply_markup') or {}
    check('菜单列出群 + 有下一步',
          any('下一步' in b.get('text', '')
              for row in (kb.get('inline_keyboard') or []) for b in row), str(kb)[:80])
    token = r._cl[BOSS]['token']
    r.api.reset()
    r.cleanup_callback('cb', BOSS, r._cl[BOSS]['mid'],
                       'cleanup:%s:toggle:%d' % (token, CHAT))
    check('勾选群生效', CHAT in r._cl[BOSS]['selected'])
    r.api.reset()
    r.cleanup_callback('cb', BOSS, r._cl[BOSS]['mid'], 'cleanup:%s:next' % token)
    check('下一步进确认页', r._cl[BOSS]['stage'] == 'confirm')
    r.api.reset()
    r.cleanup_callback('cb', BOSS, r._cl[BOSS]['mid'], 'cleanup:%s:confirm' % token)
    # ★ 等「真的删了」再断言 —— 光等菜单状态清掉不行，
    #   菜单是同步清的，那时后台线程可能还没开始删
    deadline = time.time() + 5
    while not r.api.deleted and time.time() < deadline:
        time.sleep(0.1)
    check('★ 确认后真的开始删', len(r.api.deleted) >= 1, '删了 %d 条' % len(r.api.deleted))
    check('菜单状态清掉了', BOSS not in r._cl)

r = mk()
r.api.reset()
r.handle(upd_full({
    'message_id': 2, 'date': int(time.time()),
    'chat': {'id': MEMBER, 'type': 'private'},
    'from': {'id': MEMBER, 'first_name': '路人'}, 'text': '/del'}))
check('★ 非主人私聊 /del 被拒', '只有机器人主人' in
      (r.api.last_of('sendMessage').get('text') or ''), str(r.api.methods()))

print()
print('=' * 62)
print('十三、广播（私聊多步流程）')
print('=' * 62)
r = mk()
r.store.remember_bot_chat(CHAT, '群A', 'supergroup')
r.store.remember_bot_chat(-100777, '群B', 'supergroup')
r.api.reset()
r.handle(upd_full({
    'message_id': 1, 'date': int(time.time()),
    'chat': {'id': BOSS, 'type': 'private'},
    'from': {'id': BOSS, 'first_name': '老板'}, 'text': '广播'}))
check('广播菜单出来', BOSS in r._bc, str(list(r._bc)))
kb = r.api.last_of('sendMessage').get('reply_markup') or {}
check('列出两个群',
      len([b for row in kb['inline_keyboard'] for b in row
           if b['callback_data'].startswith('broadcast:toggle')]) == 2, str(kb)[:80])

r.broadcast_callback('cb', BOSS, 1, 'broadcast:toggle:%d' % CHAT)
check('勾选一个群', CHAT in r._bc[BOSS]['selected'])
r.broadcast_callback('cb', BOSS, 1, 'broadcast:next')
check('进入等输入状态', r._bc[BOSS]['waiting'] is True)
r.api.reset()
# ★ 走真实路径：私聊发一句话，应该被当成广播内容，而不是拿去记账
r.handle(upd_full({
    'message_id': 3, 'date': int(time.time()),
    'chat': {'id': BOSS, 'type': 'private'},
    'from': {'id': BOSS, 'first_name': '老板'}, 'text': '大家好，今晚照常'}, uid=3))
check('出了确认页', '广播目标' in (r.api.last_of('sendMessage').get('text') or ''))
check('★ 广播内容不会被拿去当记账记账',
      '入款' not in (r.api.last_of('sendMessage').get('text') or ''))
check('等输入状态关掉了', r._bc[BOSS]['waiting'] is False)

r.api.reset()
r.broadcast_callback('cb', BOSS, 1, 'broadcast:confirm')
deadline = time.time() + 5
while BOSS in r._bc and time.time() < deadline:
    time.sleep(0.1)
sent = [p for m, p in r.api.calls if m == 'sendMessage' and p.get('chat_id') == CHAT]
check('★ 广播真的发到群里了', len(sent) >= 1, str(len(sent)))
check('内容对', sent and sent[0].get('text') == '大家好，今晚照常')
check('只发了勾选的那个群',
      not [p for m, p in r.api.calls
           if m == 'sendMessage' and p.get('chat_id') == -100777])

r = mk()
r.store.remember_bot_chat(CHAT, '群A', 'supergroup')
r.api.reset()
r.handle(upd_full({
    'message_id': 1, 'date': int(time.time()),
    'chat': {'id': MEMBER, 'type': 'private'},
    'from': {'id': MEMBER, 'first_name': '路人'}, 'text': '广播'}))
check('★ 非主人不能广播', '只有机器人主人' in
      (r.api.last_of('sendMessage').get('text') or ''))

r = mk()
r.api.reset()
r.handle(upd_full({
    'message_id': 1, 'date': int(time.time()),
    'chat': {'id': BOSS, 'type': 'private'},
    'from': {'id': BOSS, 'first_name': '老板'}, 'text': '广播'}))
check('没有群时给提示', '还没有记录到可广播的群' in
      (r.api.last_of('sendMessage').get('text') or ''))

print()
print('=' * 62)
print('十四、群管理不影响记账本身')
print('=' * 62)
r = mk()
r.handle(upd('+100 李四', mid=1))
r.handle(upd('账单', mid=2))
check('记账照常', '入款' in (r.api.last_of('sendMessage').get('text') or ''))
r.api.reset()
r.handle(upd('+1+1', mid=3))
check('算式照常', '2' in (r.api.last_of('sendMessage').get('text') or ''))

print()
print('=' * 62)
print('十五、多个记账机器人互不串味')
print('=' * 62)
a = mk()
b = mk({'id': 'othergrp', 'token': '2:FAKE', 'note': '另一个',
        'admin_ids': [BOSS], 'owner_id': BOSS, 'enabled': True})
a.store.remember_bot_chat(CHAT, '群A', 'supergroup')
a.positions.record(CHAT, 500, '群A')
check('A 的位置库有记录', a.positions.latest(CHAT) == 500)
check('★ B 的位置库是空的', b.positions.latest(CHAT) == 0)
a._cool[CHAT] = time.monotonic()
check('★ A 的 @全体冷却不影响 B', CHAT not in b._cool)
a._welcome_at[CHAT] = time.monotonic()
check('★ A 的入群防重不影响 B', CHAT not in b._welcome_at)
a._cl[BOSS] = {'token': 'x', 'groups': [], 'selected': set(), 'page': 0}
check('★ A 的清理菜单不影响 B', BOSS not in b._cl)

print()
print('=' * 62)
print('十六、菜单过期 + 乱输入')
print('=' * 62)
r = mk()
r._bc[1] = {'groups': [], 'selected': set(), 'waiting': True, 'at': 0}
r._cl[2] = {'token': 'x', 'groups': [], 'selected': set(), 'page': 0, 'at': 0}
r._expire_menus()
check('★ 超时的广播菜单被清掉', 1 not in r._bc)
check('★ 超时的清理菜单被清掉', 2 not in r._cl)

for weird in ('', '   ', '通知所有人', '/del', '广播', '广播  ', '清理',
              '/at_all', '🎉', 'x' * 2000):
    try:
        rr = mk()
        rr.handle(upd_full({
            'message_id': 1, 'date': int(time.time()),
            'chat': {'id': CHAT, 'type': 'supergroup', 'title': '群'},
            'from': {'id': BOSS, 'first_name': '老板'}, 'text': weird}))
        rr.handle(upd_full({
            'message_id': 2, 'date': int(time.time()),
            'chat': {'id': BOSS, 'type': 'private'},
            'from': {'id': BOSS, 'first_name': '老板'}, 'text': weird}, uid=2))
        check('不炸：%r' % (weird[:10] or '（空）'), True)
    except Exception as ex:
        check('不炸：%r' % (weird[:10] or '（空）'), False,
              '%s: %s' % (type(ex).__name__, ex))

cleanup()
print()
print('=' * 62)
print('结果：%d 项通过，%d 项失败' % (len(OK), len(BAD)))
for x in BAD:
    print('  ❌', x)
print('=' * 62)
sys.exit(1 if BAD else 0)
