# -*- coding: utf-8 -*-
"""记账机器人的「管家」测试：换主人清账本、删机器人删库、停机收尾

★ 为什么单独测这个：
  记账的数据在 sqlite 文件里（不在 bots.json 里），
  换个商户如果不删库，新商户的「广播/清理」菜单里就会列出
  **上一个商户的群名和成员名单** —— 这是实打实的泄露。
  而 Windows 上文件被占着删不掉，所以删除时机很讲究。
"""
import json
import os
import shutil
import sys
import time

sys.stdout.reconfigure(encoding='utf-8')

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = os.path.join(HERE, '_tmp_lgr_house')
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(os.path.join(TMP, 'data'), exist_ok=True)

import core
core.BASE_DIR = TMP
core.BOTS_FILE = os.path.join(TMP, 'bots.json')
core.DATA_DIR = os.path.join(TMP, 'data')

from runners.ledger.storage import LedgerStore
from runners.ledger import LedgerRunner
from manager import BotManager

OK, BAD = [], []


def check(name, cond, extra=''):
    (OK if cond else BAD).append(name)
    print('  %s %s%s' % ('✅' if cond else '❌', name,
                         ('  → %s' % (extra,)) if extra else ''))


def db_path(bid):
    return os.path.join(core.DATA_DIR, '%s.sqlite3' % bid)


def pos_path(bid):
    return os.path.join(core.DATA_DIR, '%s.positions.sqlite3' % bid)


def seed(bid, group='群A', gid=-100999):
    """给这个机器人造点真实记账数据"""
    st = LedgerStore(db_path(bid))
    st.remember_bot_chat(gid, group, 'supergroup')
    st.ensure_chat(gid)
    st.add_entry(gid, 'income', '100', 'USDT', '', 1, '甲')
    n = len(st.entries(gid))
    st.close()
    with open(pos_path(bid), 'w', encoding='utf-8') as f:
        f.write('x')          # 位置库先不管内容，只验证文件在不在
    return n


def groups_of(bid):
    st = LedgerStore(db_path(bid))
    try:
        return [(int(r['chat_id']), r['title']) for r in st.list_active_bot_groups()]
    finally:
        st.close()


mgr = BotManager({'pass': 'ADMINPW123'})

print('=' * 62)
print('一、删机器人：账本要一起删掉')
print('=' * 62)
b1 = {'id': 'house1', 'note': '记账', 'type': 'ledger', 'token': '1:T',
      'admin_ids': [], 'enabled': False, 'mid': ''}
mgr.bots.append(b1)
mgr.save()
seed('house1')
check('账本建出来了', os.path.exists(db_path('house1')))
check('位置库建出来了', os.path.exists(pos_path('house1')))

mgr.remove('house1')
check('★ 机器人删了', mgr.find('house1') is None)
check('★ 账本 sqlite 也删了', not os.path.exists(db_path('house1')),
      '还在！新商户会看到旧数据')
check('★ 群消息位置库也删了', not os.path.exists(pos_path('house1')))
check('bots.json 里也没了', not os.path.exists(
    os.path.join(core.DATA_DIR, 'house1.json')))

print()
print('=' * 62)
print('二、换商户：账本要清空（不能把上个商户的群带过去）')
print('=' * 62)
b2 = {'id': 'house2', 'note': '记账', 'type': 'ledger', 'token': '2:T',
      'admin_ids': [], 'enabled': False, 'mid': ''}
mgr.bots.append(b2)
for i in range(5):
    bid = 'house2_%d' % i
    mgr.bots.append({'id': bid, 'note': '记账%d' % i, 'type': 'ledger',
                     'token': '%d:T' % (10 + i), 'admin_ids': [],
                     'enabled': False, 'mid': ''})
mgr.save()
n = seed('house2', '老商户的群', -100123)
check('先有 1 笔账', n == 1, '%d 笔' % n)
check('群列表里有「老商户的群」',
      ('老商户的群' in [t for _, t in groups_of('house2')]),
      str(groups_of('house2')))

mgr.assign_bots('m001', ['house2'], clear_secrets=True)
b = mgr.find('house2')
check('划给商户了', b.get('mid') == 'm001')
# ★ 分配会「停→等→重启」，文件一放开就当场清掉，不留到以后
check('★ 账本当场被删（群名再也查不到）', not os.path.exists(db_path('house2')))
check('★ 重新建库后是空的（没有上个商户的群）',
      groups_of('house2') == [], str(groups_of('house2')))
check('清完不留标记（wipe_data）', not b.get('wipe_data'), str(b.get('wipe_data')))

print()
print('=' * 62)
print('三、收回机器人（商户 → 管理员）也要清')
print('=' * 62)
seed('house2', '商户的群', -100456)
check('有数据了', len(groups_of('house2')) == 1)
mgr.assign_bots('', ['house2'])
check('收回了', (mgr.find('house2').get('mid') or '') == '')
check('★ 收回时也清空了', groups_of('house2') == [], str(groups_of('house2')))

print()
print('=' * 62)
print('四、别的类型不受影响（不能顺手把人家的数据删了）')
print('=' * 62)
sh = {'id': 'shopx', 'note': '商城', 'type': 'shop', 'token': '3:T',
      'admin_ids': [], 'enabled': False, 'mid': '',
      'shop': {'trx_own': 'TXXX', 'provider_key': 'K'}}
mgr.bots.append(sh)
mgr.save()
mgr.assign_bots('m002', ['shopx'], clear_secrets=True)
check('★ 商城不会被误标 wipe_data', not sh.get('wipe_data'))
check('★ 但商城的密钥照旧清空（原有行为不变）',
      (sh.get('shop') or {}).get('provider_key') == '',
      str(sh.get('shop')))

kf = {'id': 'kefux', 'note': '客服', 'type': 'kefu', 'token': '4:T',
      'admin_ids': [], 'enabled': False, 'mid': ''}
mgr.bots.append(kf)
mgr.save()
mgr.assign_bots('m002', ['kefux'], clear_secrets=True)
check('★ 客服不会被误标', not kf.get('wipe_data'))

print()
print('=' * 62)
print('五、文件占着删不掉时：不能假装删成功')
print('=' * 62)
b3 = {'id': 'house3', 'note': '记账', 'type': 'ledger', 'token': '5:T',
      'admin_ids': [], 'enabled': False, 'mid': '', 'wipe_data': True}
mgr.bots.append(b3)
seed('house3')
holder = open(db_path('house3'), 'rb')          # 占住文件（模拟 sqlite 还开着）
try:
    mgr._wipe_on_handover(b3)
    check('★ 删不掉时保留标记（下次启动再试）', b3.get('wipe_data') is True)
    check('★ 不会谎报成功', os.path.exists(db_path('house3')))
finally:
    holder.close()
mgr._wipe_on_handover(b3)
check('文件放开后能删掉', not os.path.exists(db_path('house3')))
check('删掉后标记摘掉', not b3.get('wipe_data'))

print()
print('=' * 62)
print('六、非记账类型带着脏标记也不会出事')
print('=' * 62)
b4 = {'id': 'kefu_dirty', 'note': '客服', 'type': 'kefu', 'token': '6:T',
      'admin_ids': [], 'enabled': False, 'mid': '', 'wipe_data': True}
mgr.bots.append(b4)
mgr.save()
mgr._wipe_on_handover(b4)
check('★ 脏标记被清掉，且不会去删不该删的文件', not b4.get('wipe_data'))

print()
print('=' * 62)
print('七、★ 机器人「正在跑」的时候删/移交（最容易漏的情况）')
print('=' * 62)


class OfflineLedger(LedgerRunner):
    """不上网的记账机器人：把 sqlite 真的打开并占着，模拟「正在运行」

    ★ 为什么要它：之前那版测试里 runner 根本没启动，文件压根没被打开，
      所以「删不掉」这个 bug 测不出来 —— 测试全绿但线上照样出事。
      这个假 runner 会真的持有 sqlite 连接，才测得出真问题。

    ★ 覆盖的是 _loop() 不是 run() —— 这样 on_stop() 那套收尾逻辑
      （关 sqlite）走的是和线上**一模一样**的路径。
    """
    def _loop(self):
        self.on_start()
        _ = self.store            # ★ 真的打开 sqlite（占住文件的就是它）
        _ = self.positions
        while not self.stop_evt.is_set():
            self.stop_evt.wait(0.2)


def start_fake(bid):
    b = {'id': bid, 'note': '记账', 'type': 'ledger', 'token': '7:T',
         'admin_ids': [], 'enabled': False, 'mid': ''}
    mgr.bots.append(b)
    mgr.save()
    r = OfflineLedger(mgr, b)
    with mgr.lock:
        mgr.runners[bid] = r
    r.start()
    deadline = time.time() + 5
    while not os.path.exists(db_path(bid)) and time.time() < deadline:
        time.sleep(0.1)
    return b, r


b5, r5 = start_fake('house5')
check('机器人跑起来了 + 库打开了',
      r5.is_alive() and os.path.exists(db_path('house5')))
seed('house5', '跑着的时候写的群', -100555)     # 让库里有真数据
check('库里有数据', len(groups_of('house5')) == 1)

# ---- 7.1 正在跑的时候「删除机器人」----
mgr.remove('house5')
check('★ 删机器人：线程已经停了', not r5.is_alive())
check('★★ 库文件真的删掉了（不是「删不掉、请手动删」）',
      not os.path.exists(db_path('house5')),
      '还在！日志里会出现「删不掉…请手动删」')
check('★ 位置库也删掉了', not os.path.exists(pos_path('house5')))
check('机器人也移出列表了', mgr.find('house5') is None and 'house5' not in mgr.runners)

# ---- 7.2 正在跑的时候「换商户」----
b6, r6 = start_fake('house6')
seed('house6', '上一个商户的群', -100666)
check('跑着 + 有上个商户的数据', len(groups_of('house6')) == 1)

mgr.assign_bots('mZ', ['house6'], clear_secrets=False)
check('线程停了', not r6.is_alive())
check('★★ 换商户：账本被清干净（不是锁着删不掉、机器人带着旧数据起来）',
      groups_of('house6') == [], str(groups_of('house6')))
check('文件确实被重建成空库', os.path.exists(db_path('house6')))

# ---- 7.3 stop_all 要把连接关干净 ----
b7, r7 = start_fake('house7')
seed('house7')
mgr.stop_all()
check('stop_all 之后线程都停了', not r7.is_alive())
os.remove(db_path('house7'))
check('★ 连接确实关了（能删掉 = 没占着）', not os.path.exists(db_path('house7')))
mgr.bots = [b for b in mgr.bots if b['id'] not in ('house6', 'house7')]
mgr.save()

print()
print('=' * 62)
print('八、事务性：一次分配多个商户的机器人不串')
print('=' * 62)
mgr2 = BotManager({'pass': 'ADMINPW123'})
ids = []
for i in range(4):
    bid = 'multi%d' % i
    ids.append(bid)
    mgr2.bots.append({'id': bid, 'note': '记账%d' % i, 'type': 'ledger',
                      'token': '20:T%d' % i, 'admin_ids': [],
                      'enabled': False, 'mid': ''})
mgr2.save()
mgr2.assign_bots('mA', ids[:2], clear_secrets=True)
mgr2.assign_bots('mB', ids[2:], clear_secrets=True)
a = [b['id'] for b in mgr2.bots if b.get('mid') == 'mA']
bb = [b['id'] for b in mgr2.bots if b.get('mid') == 'mB']
check('mA 拿到前两个', sorted(a) == sorted(ids[:2]), str(a))
check('mB 拿到后两个', sorted(bb) == sorted(ids[2:]), str(bb))
check('★ 四台都清空了（没有别人的群）',
      all(groups_of(i) == [] for i in ids))
check('都没留标记', all(not mgr2.find(i).get('wipe_data') for i in ids))
r1 = mgr2.assign_bots('mA', ids[:2], clear_secrets=True)   # 再分配一次（没换人）
check('★ 同一个人再分配不会重复清（不算移交）', r1[0] == 0, str(r1))

print()
print('=' * 62)
print('九、★ 群消息记录：换主人/删除都要清掉（那就是上个主人的群聊）')
print('=' * 62)


def arc_path(bid):
    return os.path.join(core.DATA_DIR, '%s.archive.sqlite3' % bid)


def arc_media(bid):
    return os.path.join(core.DATA_DIR, '%s.archive-media' % bid)


def seed_archive(bid, text='上一个主人的群聊内容'):
    from archive import MessageArchive
    ar = MessageArchive(arc_path(bid))
    ar.record_result({'message': {
        'message_id': 1, 'date': int(time.time()),
        'chat': {'id': -100777, 'type': 'supergroup', 'title': '上个主人的群'},
        'from': {'id': 5, 'first_name': '客', 'username': 'k'},
        'text': text}})
    os.makedirs(arc_media(bid), exist_ok=True)
    with open(os.path.join(arc_media(bid), 'x.jpg'), 'wb') as f:
        f.write(b'fake')
    return ar


def arc_texts(bid):
    from archive import MessageArchive
    if not os.path.exists(arc_path(bid)):
        return []
    return [m['text'] for m in MessageArchive(arc_path(bid)).messages()]


# ---- 9.1 删机器人：记录和图片一起删 ----
b8 = {'id': 'arc1', 'note': '监控', 'type': 'kefu', 'token': '8:T',
      'admin_ids': [], 'enabled': False, 'mid': '',
      'archive': {'enabled': True, 'keep_days': 7}}
mgr.bots.append(b8)
mgr.save()
seed_archive('arc1')
check('记录建出来了',
      os.path.exists(arc_path('arc1')) and os.path.isdir(arc_media('arc1')))
mgr.remove('arc1')
check('★ 删机器人：记录库删了', not os.path.exists(arc_path('arc1')))
check('★ 图片目录也删了', not os.path.isdir(arc_media('arc1')))

# ---- 9.2 换商户：记录清空、开关关掉、图片删掉 ----
b9, r9 = start_fake('arc2')
b9['archive'] = {'enabled': True, 'keep_days': 7}
mgr.save()
seed_archive('arc2', 'A商户的客户聊天记录')
check('有记录', arc_texts('arc2') == ['A商户的客户聊天记录'], str(arc_texts('arc2')))

mgr.assign_bots('mNew', ['arc2'], clear_secrets=False)
check('★ 换商户：记录被清空（新商户看不到旧群聊）',
      arc_texts('arc2') == [], str(arc_texts('arc2')))
check('★ 图片目录也被清了', not os.path.isdir(arc_media('arc2')))
check('★ 记录开关被关掉（下家要记录得自己开）',
      (mgr.find('arc2').get('archive') or {}).get('enabled') is False,
      str(mgr.find('arc2').get('archive')))
check('清理标记已摘掉', not mgr.find('arc2').get('wipe_data'))
check('线程停了', not r9.is_alive())

# ---- 9.3 回收（商户→管理员）也要清 ----
seed_archive('arc2', '又要被清掉的内容')
mgr.assign_bots('', ['arc2'])
check('★ 收回时也清干净', arc_texts('arc2') == [], str(arc_texts('arc2')))

# ---- 9.4 没开记录的机器人不受影响 ----
b10 = {'id': 'arc3', 'note': '普通客服', 'type': 'kefu', 'token': '9:T',
       'admin_ids': [], 'enabled': False, 'mid': ''}
mgr.bots.append(b10)
mgr.save()
seed_archive('arc3', '不该被删的内容')
mgr.assign_bots('mNew', ['arc3'], clear_secrets=False)
check('★ 没开记录的：换商户不会被误清（也不该被清，那是别的数据）',
      arc_texts('arc3') == ['不该被删的内容'], str(arc_texts('arc3')))
check('也没打清理标记', not mgr.find('arc3').get('wipe_data'))

shutil.rmtree(TMP, ignore_errors=True)
print()
print('=' * 62)
print('结果：%d 项通过，%d 项失败' % (len(OK), len(BAD)))
for x in BAD:
    print('  ❌', x)
print('=' * 62)
sys.exit(1 if BAD else 0)
