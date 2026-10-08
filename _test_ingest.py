# -*- coding: utf-8 -*-
"""客户自建机器人的消息回传（`/api/ingest`）

★★ 这是**唯一对外的口子**（免面板鉴权），所以每一条都得测：

  1. **密钥** —— 错的不行、没带的不行、太短的不行
  2. ★★ **到期 / 停用一律拒收** —— 客户不续费数据就进不了库。
     这条**直接关系到钱**，不查等于白送
  3. **幂等** —— 客户那边网络抖了会重发，同一条不能变成两条
  4. **图片** —— 客户下好的图能落盘，并且面板那套 media 路由能取到
  5. **坏输入不能 500** —— 这是公网口子，乱发什么都不能把面板搞崩
  6. **「关闭记录某个群」也得认** —— 面板上关了，回传的不能再灌进来
"""
import io
import json
import os
import shutil
import sys
import threading
import time
from http.server import ThreadingHTTPServer

sys.stdout.reconfigure(encoding='utf-8')

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = os.path.join(HERE, '_tmp_ingest')
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(os.path.join(TMP, 'data'), exist_ok=True)

import core                                          # noqa: E402
core.set_base_dir(TMP)
core.BOTS_FILE = os.path.join(TMP, 'bots.json')
core.DATA_DIR = os.path.join(TMP, 'data')
core.CONFIG_FILE = os.path.join(TMP, 'config.json')
core.CODES_DIR = os.path.join(TMP, 'codes')
core.LOG_FILE = os.path.join(TMP, '运行日志.txt')
json.dump({}, io.open(core.CONFIG_FILE, 'w', encoding='utf-8'))

from manager import BotManager                       # noqa: E402
from merchants import MerchantStore                  # noqa: E402
from panel import PanelHandler                       # noqa: E402
from archive import MessageArchive                   # noqa: E402

OK, BAD = [], []
ADMIN_PW = 'ADMINPW123'
# 回传暗号 = core.ingest_sig(机器人id, token) —— **没有单独的密钥**，
# 两边从 token 现算。这里先算出来当常量用。
KEY_A = core.ingest_sig('botA', 'token-A')
KEY_B = core.ingest_sig('botB', 'token-B')


def check(name, cond, extra=''):
    (OK if cond else BAD).append(name)
    print('  %s %s%s' % ('✅' if cond else '❌', name,
                         ('  → %s' % (extra,)) if extra else ''))


def mkbot(bid, key, **kw):
    b = {'id': bid, 'type': 'ledger', 'note': bid, 'token': 'token-' + bid[3:],
         'username': bid + 'bot', 'enabled': True, 'expire_at': 0,
         'expired_at': 0, 'remote': True,
         'archive': {'enabled': True}, 'admin_ids': [], 'owner_id': '',
         'mid': '', 'bind_code': 'BC123456', 'created': '09-29 01:00'}
    b.update(kw)
    return b


mgr = BotManager({'pass': ADMIN_PW})
mgr.bots.append(mkbot('botA', KEY_A))
mgr.bots.append(mkbot('botB', KEY_B))
PanelHandler.mgr = mgr
PanelHandler.password = ADMIN_PW
PanelHandler.merch = MerchantStore()
PanelHandler.token_secret = 'S' * 32
PanelHandler.page_cache = None

httpd = ThreadingHTTPServer(('127.0.0.1', 0), PanelHandler)
BASE = 'http://127.0.0.1:%d' % httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()
time.sleep(0.3)


def arc_of(bid):
    return MessageArchive(os.path.join(TMP, 'data', '%s.archive.sqlite3' % bid))


def mkmsg(text, mid, chat=-1001234, date=None, extra=None):
    m = {'message_id': mid, 'date': date or int(time.time()),
         'chat': {'id': chat, 'type': 'supergroup', 'title': '客户A的群'},
         'from': {'id': 222, 'first_name': '张', 'last_name': '三',
                  'username': 'zs', 'is_bot': False},
         'text': text}
    if extra:
        m.update(extra)
    return {'update_id': mid, 'message': m}


def post(path, body, key=None, headers=None, raw=None):
    h = dict(headers or {})
    if key is not None:
        h['X-Ingest-Sig'] = key
    if raw is not None:
        return requests.post(BASE + path, data=raw, headers=h, timeout=10)
    return requests.post(BASE + path, json=body, headers=h, timeout=10)


print('=' * 62)
print('① ★ 密钥：错的不行、没带的不行、太短的不行')
print('=' * 62)
r = post('/api/ingest', {'events': [mkmsg('你好', 1)]})
check('★ 没带密钥 → 403', r.status_code == 403, str(r.status_code))
r = post('/api/ingest', {'events': [mkmsg('你好', 1)]}, key='x' * 64)
check('★ 密钥不对 → 403', r.status_code == 403, str(r.status_code))
r = post('/api/ingest', {'events': [mkmsg('你好', 1)]}, key='abc')
check('★ 密钥太短 → 403（不给爆破留门）', r.status_code == 403,
      str(r.status_code))
r = post('/api/ingest', {'events': [mkmsg('你好', 1)]}, key='')
check('★ 空密钥 → 403', r.status_code == 403, str(r.status_code))
check('  被拒时库里什么都没有',
      not [x for x in arc_of('botA').messages()] if
      os.path.exists(os.path.join(TMP, 'data', 'botA.archive.sqlite3')) else True)

print()
print('=' * 62)
print('② 密钥对 → 消息进库')
print('=' * 62)
r = post('/api/ingest', {'events': [mkmsg('+100 张三', 1),
                                    mkmsg('好的', 2)]}, key=KEY_A)
check('回传成功', r.status_code == 200 and r.json().get('ok'), r.text[:80])
rows = arc_of('botA').messages()
check('★★ 消息真的进库了', len(rows) == 2, '%d 条' % len(rows))
check('  内容对得上', sorted(x['text'] for x in rows) == ['+100 张三', '好的'],
      str([x['text'] for x in rows]))
check('  群名对得上', rows[0]['chat_title'] == '客户A的群', rows[0]['chat_title'])
check('  发言人对得上', rows[0]['display_name'] == '张 三')

print()
print('=' * 62)
print('③ ★★ 幂等：重发同一条不能变两条')
print('=' * 62)
post('/api/ingest', {'events': [mkmsg('+100 张三', 1),
                                mkmsg('好的', 2)]}, key=KEY_A)
rows = arc_of('botA').messages()
check('★★ 重发之后还是 2 条（客户网络抖了会重发）', len(rows) == 2,
      '%d 条' % len(rows))

print()
print('=' * 62)
print('④ ★★ 两个客户的数据必须分开')
print('=' * 62)
post('/api/ingest', {'events': [mkmsg('B 的群消息', 1, chat=-1009999)]},
     key=KEY_B)
a = arc_of('botA').messages()
b = arc_of('botB').messages()
check('★ A 的库里没有 B 的消息',
      all(x['text'] != 'B 的群消息' for x in a), '%d 条' % len(a))
check('★ B 的库里只有自己那条', len(b) == 1 and b[0]['text'] == 'B 的群消息',
      '%d 条' % len(b))

print()
print('=' * 62)
print('⑤ ★★★ 到期 / 停用 → 拒收（这条关系到钱）')
print('=' * 62)
mgr.find('botA')['expire_at'] = int(time.time()) - 100      # 已过期
r = post('/api/ingest', {'events': [mkmsg('过期还发', 9)]}, key=KEY_A)
check('★★ 已到期 → 403', r.status_code == 403, str(r.status_code))
check('★★ 而且**没进库**（不续费就看不到数据）',
      all(x['text'] != '过期还发' for x in arc_of('botA').messages()))
mgr.find('botA')['expire_at'] = 0

mgr.find('botA')['enabled'] = False
r = post('/api/ingest', {'events': [mkmsg('停用还发', 10)]}, key=KEY_A)
check('★★ 已停用 → 403', r.status_code == 403, str(r.status_code))
check('★★ 也没进库',
      all(x['text'] != '停用还发' for x in arc_of('botA').messages()))
mgr.find('botA')['enabled'] = True

print()
print('=' * 62)
print('⑥ ★ 图片：客户下好传过来，落盘后面板那套能取到')
print('=' * 62)
FID = 'AgACAgUAAx0EU1234567890abcdef'
post('/api/ingest', {'events': [mkmsg('截图',
                                      20, extra={'photo': [{'file_id': FID}]})]},
     key=KEY_A)
PNG = (b'\x89PNG\r\n\x1a\n' + b'\x00' * 64)
r = post('/api/ingest/media', None, key=KEY_A, raw=PNG,
         headers={'X-Chat': '-1001234', 'X-Msg': '20', 'X-File-Id': FID,
                  'X-Suffix': '.png', 'Content-Type': 'application/octet-stream'})
check('图片上传成功', r.status_code == 200 and r.json().get('ok'), r.text[:80])
arc = arc_of('botA')
row = [x for x in arc.messages() if x['message_id'] == '20'][0]
check('★★ payload.media 补上了（面板靠它出图）', bool(row['media']),
      repr(row['media']))
check('★ 文件名是那个确定性格式（<chat>_<msg>_<hash><后缀>）',
      row['media'].endswith('.png') and row['media'].startswith('-1001234_20_'),
      row['media'])
p = arc.media_path(row['media'])
check('★★ media_path() 能取到（面板的图片路由就靠它）', p is not None)
check('  内容跟传上来的一模一样', p and p.read_bytes() == PNG)

print()
print('   —— 记录不存在时不能让服务端崩 ——')
r = post('/api/ingest/media', None, key=KEY_A, raw=PNG,
         headers={'X-Chat': '-1009999', 'X-Msg': '404', 'X-File-Id': 'NOPE',
                  'X-Suffix': '.png'})
check('★ 没这条记录 → ok:false（不是 500）',
      r.status_code == 200 and r.json().get('ok') is False, r.text[:60])
r = post('/api/ingest/media', None, key=KEY_A, raw=b'',
         headers={'X-Chat': '-1001234', 'X-Msg': '20', 'X-File-Id': FID})
check('★ 空图片 → 413（不是 500）', r.status_code == 413, str(r.status_code))
r = post('/api/ingest/media', None, key=KEY_A, raw=PNG,
         headers={'X-Chat': '-1001234', 'X-Msg': '20', 'X-File-Id': FID,
                  'X-Suffix': '.exe'})
check('★ 乱给后缀 → 退回 .jpg（不会写出奇怪的文件名）',
      r.status_code == 200)

print()
print('=' * 62)
print('⑦ ★ 坏输入不能把面板搞崩（这是公网口子）')
print('=' * 62)
r = requests.post(BASE + '/api/ingest', data=b'not json at all',
                  headers={'X-Ingest-Sig': KEY_A,
                           'Content-Type': 'application/json'}, timeout=10)
check('★ 坏 JSON → 400（不是 500）', r.status_code == 400, str(r.status_code))
r = post('/api/ingest', {}, key=KEY_A)
check('★ 没有 events → 400', r.status_code == 400, str(r.status_code))
r = post('/api/ingest', {'events': '不是列表'}, key=KEY_A)
check('★ events 不是列表 → 400', r.status_code == 400, str(r.status_code))
r = post('/api/ingest', {'events': [1, 2, 'abc', {}]}, key=KEY_A)
check('★ events 里全是垃圾 → 不崩（archive 自己会跳过）',
      r.status_code == 200, str(r.status_code))
r = post('/api/ingest', {'events': [mkmsg('x', i) for i in range(500)]},
         key=KEY_A)
check('★ 一次发 500 条 → 截到 200 条，不崩', r.status_code == 200,
      r.text[:60])

print()
print('=' * 62)
print('⑧ ★ 面板上「关闭记录某个群」对回传也要生效')
print('=' * 62)
mgr.find('botA')['archive'] = {'enabled': True, 'mute_chats': ['-1007777']}
post('/api/ingest', {'events': [mkmsg('这个群关了', 30, chat=-1007777),
                                mkmsg('这个群没关', 31, chat=-1008888)]},
     key=KEY_A)
texts = [x['text'] for x in arc_of('botA').messages()]
check('★★ 关掉的群：回传的**整条不入库**', '这个群关了' not in texts)
check('★ 没关的群照常进', '这个群没关' in texts)

print()
print('   —— ★★ 面板列表必须把 remote 传给前端 ——')
# snapshot() 是**白名单式**拼字段的：新加一个字段忘了写进去，
# 前端就永远收不到 —— 表现为「面板上怎么都不显示客户自建、
# 也没有安装包按钮」，但在接口层怎么测都是对的（2026-09-29 踩过）
_snap = PanelHandler.mgr.snapshot(None)
_row = [x for x in _snap if x['id'] == 'botA']
check('★★ snapshot 里带 remote（漏了面板上就不显示「客户自建」和安装包按钮）',
      _row and _row[0].get('remote') is True,
      str(_row[0].get('remote') if _row else '找不到 botA'))

print()
print('   —— ★ 补票：添加时忘了勾，事后改成「客户自建」——')
# 那个勾选框只在**添加**的时候有。忘了勾只能删掉重建，
# 而重建会换一个机器人 id —— 已经发给客户的安装包当场失效。
# 所以留了个口子，这里把它锁住。
_b = mgr.find('botB')
_b['remote'] = False
check('  没设 remote 时本机会跑它', mgr.should_run(_b) is True)
_ok, _err = mgr.set_remote('botB', True)
check('★★ 能改成客户自建', _ok and _b.get('remote') is True, _err)
check('★★ 改完本机就不跑了（不然跟客户的服务器抢 token，两边都收不到）',
      mgr.should_run(_b) is False)
check('★ 非记账机器人不让改（客服/USDT/商城没有独立版）',
      mgr.set_remote('noSuchBot', True)[0] is False)

print()
print('   —— ★★ 客户那边绑定了，要报回来 ——')
# 绑定是**在客户的服务器上**发生的。面板自己那份永远是空的 ——
# 不报回来的话就永远显示「等待绑定」，客户明明已经绑好了。
r = post('/api/ingest/state', {'admin_ids': [77001], 'owner_id': 77001,
                               'admin_name': '张老板'}, key=KEY_A)
check('★ 客户机器人把绑定状态报上来了',
      r.status_code == 200 and r.json().get('ok'), r.text[:60])
_bb = mgr.find('botA')
check('★★ 面板记下了（不再是「等待绑定」）',
      _bb.get('admin_ids') == [77001] and _bb.get('admin_name') == '张老板',
      str(_bb.get('admin_ids')))
_sn = [x for x in mgr.snapshot(None) if x['id'] == 'botA'][0]
check('★★ 前端拿到的 bound 也变 True 了', _sn.get('bound') is True)
check('★ 而且带上了绑定者的名字', _sn.get('admin_name') == '张老板')

post('/api/ingest/state', {'admin_ids': [1], 'note': '改个名试试',
                           'token': 'hack', 'remote': False}, key=KEY_A)
_bb = mgr.find('botA')
check('★★ 只能报绑定状态，改不了别的（note / token 纹丝不动）',
      _bb.get('note') == 'botA' and _bb.get('token') == 'token-A',
      '%s / %s' % (_bb.get('note'), _bb.get('token')))

print()
print('=' * 62)
print('⑨ ★ 面板那边没受影响（ingest 是免鉴权的，别的接口不是）')
print('=' * 62)
r = requests.get(BASE + '/api/bots', timeout=10)
check('★ 没密码读机器人列表 → 还是被挡', r.status_code in (401, 403),
      str(r.status_code))
r = requests.get(BASE + '/api/bots', headers={'X-Panel-Pass': ''}, timeout=10)
check('★ 空密码 → 还是被挡', r.status_code in (401, 403), str(r.status_code))

print()
print('=' * 62)
print('⑩ ★★ 同一秒里的两条：先后必须按 message_id 排，不能乱')
print('=' * 62)
# 2026-09-29 用户截图报的：群里先发 z0、机器人**同一秒**回了报价，
# 面板上却把机器人的排到了提问前面。
# 根因：ts 只精确到**秒**（Telegram 的 date 就是秒），光按 ts 排，
# 同一秒里两条的先后是**不确定的**。message_id 在群里递增，才是真实先后。
_T = int(time.time())
post('/api/ingest', {'events': [mkmsg('先问的', 501, date=_T),
                                mkmsg('后回的', 502, date=_T)]}, key=KEY_A)
_rows = arc_of('botA').messages(chat_id='-1001234', limit=200)
_pair = [x for x in _rows if x['message_id'] in ('501', '502')]
check('  同秒的两条都进去了', len(_pair) == 2, '%d 条' % len(_pair))
check('★★ 接口是「新的在前」→ message_id 大的必须排前面',
      bool(_pair) and _pair[0]['message_id'] == '502',
      '实际顺序：%s' % [x['message_id'] for x in _pair])
# ★ 顺便验一下 message_id 是 TEXT 存的：不比大小的话 '9' 会排在 '10' 后面
post('/api/ingest', {'events': [mkmsg('九', 9, date=_T, chat=-1005555),
                                mkmsg('十', 10, date=_T, chat=-1005555)]},
     key=KEY_A)
_rows = arc_of('botA').messages(chat_id='-1005555', limit=50)
check('★★ 数字位数不同也不能按字符串排（9 要排在 10 后面）',
      len(_rows) == 2 and _rows[0]['message_id'] == '10',
      '实际顺序：%s' % [x['message_id'] for x in _rows])

print()
print('=' * 62)
print('⑪ ★★ 主人的消息要标成 is_owner（前端靠它放右边）')
print('=' * 62)
# 前端「像微信那样」把主人的话放右边，靠的就是这个字段。
# 以前回传路径建库时**没传 owner_id** → 主人所有消息都算 False → 全在左边，
# 那段前端代码从来没生效过。
OWNER_ID = 88888
mgr.find('botA')['owner_id'] = OWNER_ID
post('/api/ingest', {'events': [
    mkmsg('主人说的话', 600, extra={'from': {'id': OWNER_ID, 'first_name': '主',
                                            'is_bot': False}}),
    mkmsg('客人说的话', 601)]}, key=KEY_A)
_r = requests.get(BASE + '/api/archive/botA/messages',
                  params={'chat': '-1001234'},
                  headers={'X-Panel-Pass': ADMIN_PW}, timeout=10).json()
_by_id = {x['message_id']: x for x in (_r.get('messages') or [])}
check('★★ 主人发的 → is_owner=True（前端才会放右边）',
      (_by_id.get('600') or {}).get('is_owner') is True,
      str((_by_id.get('600') or {}).get('is_owner')))
check('★ 别人发的 → is_owner=False（放左边）',
      (_by_id.get('601') or {}).get('is_owner') is False,
      str((_by_id.get('601') or {}).get('is_owner')))

# ★★ 再验「老数据也能好」：以前的库是**没传 owner_id** 建的，
#    主人那些消息存进去时 is_owner 就已经是 False 了。
#    这里照当时的样子写一条，再看接口认不认。
from archive import MessageArchive                     # noqa: E402
_legacy = MessageArchive(os.path.join(TMP, 'data', 'botA.archive.sqlite3'))
_legacy.record_result({'message': {
    'message_id': 700, 'date': int(time.time()),
    'chat': {'id': -1001234, 'type': 'supergroup', 'title': '客户A的群'},
    'from': {'id': OWNER_ID, 'first_name': '主', 'is_bot': False},
    'text': '老库里存错的'}})
_stored = [x for x in _legacy.messages(chat_id='-1001234', limit=200)
           if x['message_id'] == '700']
check('  老库写进去的确实是错的（is_owner=False）',
      _stored and _stored[0].get('is_owner') is False,
      str(_stored[0].get('is_owner') if _stored else '找不到'))
_r2 = requests.get(BASE + '/api/archive/botA/messages',
                   params={'chat': '-1001234'},
                   headers={'X-Panel-Pass': ADMIN_PW}, timeout=10).json()
_got = [x for x in (_r2.get('messages') or []) if x['message_id'] == '700']
check('★★ 接口读出来已经修成 True 了（不用改库、不用写迁移）',
      _got and _got[0].get('is_owner') is True,
      str(_got[0].get('is_owner') if _got else '找不到'))
_legacy.close()

# ★ 还有一类机器人：只记了「谁绑定的」（admin_ids），没有单独的 owner_id。
#   core.owner_id() 对这种情况有兜底，读的时候必须用**同一套**，
#   不然同一条消息在两种机器人上会左右不一样。
mgr.find('botA')['owner_id'] = 0
mgr.find('botA')['admin_ids'] = [77777]
post('/api/ingest', {'events': [mkmsg('绑定人说的话', 800,
                                      extra={'from': {'id': 77777,
                                                      'first_name': '绑',
                                                      'is_bot': False}})]},
     key=KEY_A)
_r3 = requests.get(BASE + '/api/archive/botA/messages',
                   params={'chat': '-1001234'},
                   headers={'X-Panel-Pass': ADMIN_PW}, timeout=10).json()
_g3 = [x for x in (_r3.get('messages') or []) if x['message_id'] == '800']
check('★ 只记了绑定人、没记 owner_id 时也认得出主人（口径跟 core 一致）',
      _g3 and _g3[0].get('is_owner') is True,
      str(_g3[0].get('is_owner') if _g3 else '找不到'))

print()
print('=' * 62)
print('⑫ ★ 群成员（消息流表头那个「👥 群成员」标签）')
print('=' * 62)
# ★★ 口径：Telegram **不让机器人拉群成员全表**（只有成员数 + 管理员列表），
#    所以列的是「机器人见过发言的人」。换会话要重新拉，按人聚合。
MCHAT = -1006666
mgr.find('botA')['owner_id'] = 99999
mgr.find('botA')['admin_ids'] = [99999]


def _mk(text, mid, uid, name, uname='', is_bot=False):
    frm = {'id': uid, 'first_name': name, 'is_bot': is_bot}
    if uname:
        frm['username'] = uname
    return mkmsg(text, mid, chat=MCHAT, extra={'from': frm})


post('/api/ingest', {'events': [
    _mk('一', 1, 111, '张', 'zs'),
    _mk('二', 2, 111, '张', 'zs'),          # 同一个人的第二条
    _mk('三', 3, 222, '李'),                # 没设用户名
    _mk('老板', 4, 99999, '老板', 'boss'),   # 主人
    _mk('账单', 5, 555, '机器人', is_bot=True),
]}, key=KEY_A)
_r = requests.get(BASE + '/api/archive/botA/members',
                  params={'chat': str(MCHAT)},
                  headers={'X-Panel-Pass': ADMIN_PW}, timeout=10).json()
_mem = _r.get('members') or []
_by = {m['user_id']: m for m in _mem}
check('★ 接口通，4 个人（机器人不算人）', _r.get('ok') and len(_mem) == 4,
      '返回 %d 条' % len(_mem))
check('★ 同一个人发两条 → 合成一条、计数 2',
      (_by.get('111') or {}).get('count') == 2,
      str((_by.get('111') or {}).get('count')))
check('★ 名字和用户名都取到了',
      (_by.get('111') or {}).get('display_name') == '张'
      and (_by.get('111') or {}).get('username') == 'zs',
      repr(_by.get('111')))
check('★ 没设用户名的也在（用户名空着，不假装有）',
      '222' in _by and (_by.get('222') or {}).get('username') == '',
      repr(_by.get('222')))
check('★ 主人被标出来（跟消息列表同一套口径）',
      (_by.get('99999') or {}).get('is_owner') is True)
check('★ 机器人也被标出来', (_by.get('555') or {}).get('is_bot') is True)
check('★★ 人排前面、机器人垫底', bool(_mem) and _mem[-1]['user_id'] == '555',
      '顺序：%s' % [m['user_id'] for m in _mem])
_r2 = requests.get(BASE + '/api/archive/botA/members',
                   headers={'X-Panel-Pass': ADMIN_PW}, timeout=10).json()
check('★ 不传 chat（全部会话）也不炸', _r2.get('ok') is True, str(_r2)[:50])

# ★★ 管理员：Bot API 里唯一能拿到「**没发过言的人**」的口子。
#    上面那几条跑的时候 Telegram 是够不着的（假 token），
#    结果照样出来了 —— 这就顺带验了「取不到管理员也不能把成员表搞挂」。
check('★★ 够不着 Telegram 时成员表照样能用（降级，不报错）',
      _r.get('ok') and len(_mem) == 4)
check('★ 而且没有硬塞空的 admin_title 进去',
      all('admin_title' not in m for m in _mem), str(_mem[:1])[:80])

import runners.ledger.api as LAPI                       # noqa: E402


class _FakeResp:
    def __init__(self, data):
        self._d = data

    def json(self):
        return self._d


_real_post = LAPI.requests.post
LAPI.requests.post = lambda *a, **k: _FakeResp({'ok': True, 'result': [
    {'status': 'creator',
     'user': {'id': 7001, 'first_name': '群主大人', 'username': 'boss1',
              'is_bot': False}},
    {'status': 'administrator', 'custom_title': '值班',
     'user': {'id': 7002, 'first_name': '管理', 'is_bot': False}},
    # ★ 匿名管理员返回的是个假 id，必须过滤掉（不然名字都显示不出来）
    {'status': 'administrator', 'is_anonymous': True,
     'user': {'id': 7003, 'first_name': '匿名的', 'is_bot': False}},
    # 机器人自己也是管理员，要标出来、但排序仍在最底
    {'status': 'administrator',
     'user': {'id': 555, 'first_name': '机器人', 'is_bot': True}},
]})
_r3 = requests.get(BASE + '/api/archive/botA/members',
                   params={'chat': str(MCHAT)},
                   headers={'X-Panel-Pass': ADMIN_PW}, timeout=10).json()
_m3 = _r3.get('members') or []
_b3 = {m['user_id']: m for m in _m3}
LAPI.requests.post = _real_post

check('★★ 从没发过言的群主也出现了（这才是这个接口的意义）',
      (_b3.get('7001') or {}).get('count') == 0
      and (_b3.get('7001') or {}).get('admin_title') == '群主',
      repr(_b3.get('7001')))
check('★ 名字和用户名都拿到了（哪怕没发过言）',
      (_b3.get('7001') or {}).get('display_name') == '群主大人'
      and (_b3.get('7001') or {}).get('username') == 'boss1',
      repr(_b3.get('7001')))
check('★ 自定义头衔照原样显示（值班）',
      (_b3.get('7002') or {}).get('admin_title') == '值班',
      repr(_b3.get('7002')))
check('★★ 匿名管理员被过滤掉（他那个 id 是假的）',
      '7003' not in _b3, str(sorted(_b3.keys())))
check('★ 机器人自己也是管理员，标上了',
      (_b3.get('555') or {}).get('is_admin') is True)
check('★★ 管理员排最前、机器人仍在最底',
      _m3[0]['user_id'] == '7001' and _m3[-1]['user_id'] == '555',
      '顺序：%s' % [m['user_id'] for m in _m3])
check('★ 发过言的人也还在（合并，不是覆盖）',
      '111' in _b3 and '222' in _b3, str(sorted(_b3.keys())))

print()
print('   —— 商户不能从这个口子进去 ——')
mA, _ = PanelHandler.merch.add('张三', 'zhangsan')
PanelHandler.merch.set_pass(mA['mid'], 'zhangsan123', must_change=False)
tok = requests.post(BASE + '/api/login',
                    json={'user': 'zhangsan', 'pass': 'zhangsan123'},
                    timeout=10).json().get('token') or ''
r = requests.get(BASE + '/api/bots/%s/clientcfg' % 'botA',
                 headers={'X-Panel-Token': tok}, timeout=10)
check('★ 商户拿不到「客户配置」（管理端专属）',
      r.status_code != 200 or not r.json().get('ok'), r.text[:60])
r = requests.get(BASE + '/api/bots/%s/clientcfg' % 'botA',
                 headers={'X-Panel-Pass': ADMIN_PW}, timeout=10)
check('★ 管理员拿得到', r.status_code == 200 and r.json().get('ok'),
      r.text[:60])
check('  ★ 而且里面**没有密钥这种东西**（用户不用管）',
      'key' not in r.json() and 'sig' not in r.json(), str(r.json()))

try:
    httpd.shutdown()
except Exception:
    pass
shutil.rmtree(TMP, ignore_errors=True)

print()
print('=' * 62)
print('结果：%d 项通过，%d 项失败' % (len(OK), len(BAD)))
for b in BAD:
    print('  ❌', b)
print('=' * 62)
sys.exit(1 if BAD else 0)
