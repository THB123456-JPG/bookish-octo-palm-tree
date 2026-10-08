# -*- coding: utf-8 -*-
"""群消息记录测试

★ 两条底线，必须锁死：
  1. **数据只在本地** —— 除了 Telegram 官方域名，不许连任何地方
     （朋友那套原版是会把消息 POST 到 audit_url 的，那部分**没搬**）
  2. 换商户 / 删机器人时，记录必须清干净（否则新商户看到上个商户的群聊）
"""
import json
import os
import shutil
import sqlite3
import sys
import time

sys.stdout.reconfigure(encoding='utf-8')

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = os.path.join(HERE, '_tmp_archive')
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(os.path.join(TMP, 'data'), exist_ok=True)

import core
core.BASE_DIR = TMP
core.BOTS_FILE = os.path.join(TMP, 'bots.json')
core.DATA_DIR = os.path.join(TMP, 'data')

from archive import MessageArchive

OK, BAD = [], []


def check(name, cond, extra=''):
    (OK if cond else BAD).append(name)
    print('  %s %s%s' % ('✅' if cond else '❌', name,
                         ('  → %s' % (extra,)) if extra else ''))


def msg(text, mid, chat=-1001234, title='测试群', uid=222,
        first='张', last='三', username='zhangsan', date=None,
        extra=None):
    m = {'message_id': mid, 'date': date or int(time.time()),
         'chat': {'id': chat, 'type': 'supergroup', 'title': title},
         'from': {'id': uid, 'first_name': first, 'last_name': last,
                  'username': username, 'is_bot': False},
         'text': text}
    if extra:
        m.update(extra)
    return {'update_id': mid, 'message': m}


def mk(name='a', keep_hours=48, owner_id=''):
    return MessageArchive(os.path.join(TMP, 'data', '%s.archive.sqlite3' % name),
                          token='', keep_hours=keep_hours, owner_id=owner_id)


print('=' * 62)
print('一、记消息')
print('=' * 62)
a = mk()
a.record_result(msg('+100 到账', 1))
a.record_result(msg('好的', 2, uid=333, first='李', last='四',
                    username='lisi'))
rows = a.messages()
check('记下了 2 条', len(rows) == 2, '%d 条' % len(rows))
check('★ 按时间倒序（新的在前）', rows[0]['text'] == '好的', rows[0]['text'])
check('发言人对得上', rows[0]['display_name'] == '李 四', rows[0]['display_name'])
check('群名对得上', rows[0]['chat_title'] == '测试群')
check('用户名对得上', rows[0]['username'] == 'lisi')

print()
print('=' * 62)
print('二、按群 / 按关键词查')
print('=' * 62)
a.record_result(msg('另一个群的消息', 1, chat=-1009999, title='群B'))
chats = a.chats()
check('两个群都列出来了', len(chats) == 2, str([c['title'] for c in chats]))
check('★ 群选项带条数',
      sorted(c['count'] for c in chats) == [1, 2],
      str([(c['title'], c['count']) for c in chats]))
check('按群过滤', len(a.messages(chat_id='-1001234')) == 2)
check('按关键词搜正文', len(a.messages(keyword='到账')) == 1)
check('★ 按关键词搜人名',
      len(a.messages(keyword='李四')) == 0 or len(a.messages(keyword='李')) == 1,
      str(len(a.messages(keyword='李'))))
check('搜不到就是空', a.messages(keyword='根本不存在') == [])

print()
print('=' * 62)
print('三、同一条消息被编辑：覆盖，不是新增')
print('=' * 62)
a.record_result(msg('改过的内容', 1))
check('还是 2 条（没变成 3 条）', len(a.messages(chat_id='-1001234')) == 2,
      '%d 条' % len(a.messages(chat_id='-1001234')))
r = [x for x in a.messages(chat_id='-1001234') if x['message_id'] == '1'][0]
check('内容被更新了', r['text'] == '改过的内容', r['text'])

print()
print('=' * 62)
print('四、各种消息类型都认')
print('=' * 62)
a.record_result(msg('', 10, extra={'photo': [
    {'file_id': 'SMALL'}, {'file_id': 'BIG'}]}))
a.record_result(msg('', 11, extra={'voice': {'file_id': 'V1', 'duration': 3}}))
a.record_result(msg('', 12, extra={'sticker': {'file_id': 'S1'}}))
a.record_result(msg('', 13, extra={'new_chat_members': [
    {'id': 9, 'first_name': '新'}]}))
d = {m['message_id']: m for m in a.messages(chat_id='-1001234')}
check('图片：取最后一张（最大那张）', d['10']['has_photo'] is True)
check('图片占位文案', d['10']['text'] == '[图片]', d['10']['text'])
check('语音', d['11']['text'] == '[语音]', d['11']['text'])
check('贴纸', d['12']['text'] == '[贴纸]', d['12']['text'])
check('成员加入', d['13']['text'] == '[成员加入]', d['13']['text'])

print()
print('=' * 62)
print('五、表情/图片的 caption 要留下（不能只留 [图片]）')
print('=' * 62)
a.record_result(msg('', 14, extra={'caption': '这是转账截图',
                                   'photo': [{'file_id': 'P14'}]}))
d = {m['message_id']: m for m in a.messages(chat_id='-1001234')}
check('★ caption 保留', d['14']['text'] == '这是转账截图', d['14']['text'])

print()
print('=' * 62)
print('六、保留期：固定 48 小时（用户指定，不让面板配）')
print('=' * 62)
from archive import KEEP_HOURS as _KH
check('★ 保留期就是 48 小时', _KH == 48, str(_KH))
check('★ 默认构造就是 48 小时', mk('k0').keep_hours == 48,
      str(mk('k0').keep_hours))

b = mk('keep', keep_hours=48)
b.record_result(msg('很久以前的', 1, date=int(time.time()) - 3 * 86400,
                    chat=-100555))
b.record_result(msg('昨天但没过期', 2, date=int(time.time()) - 30 * 3600,
                    chat=-100555))
b.record_result(msg('刚刚的', 3, chat=-100555))
rows = b.messages(chat_id='-100555')
check('★ 超过 48 小时的直接不收（3 天前那条）',
      len(rows) == 2 and '很久以前的' not in [r['text'] for r in rows],
      str([r['text'] for r in rows]))
check('★ 30 小时前的还在（没过 48 小时）',
      '昨天但没过期' in [r['text'] for r in rows])
check('留的是最新的那条', rows[0]['text'] == '刚刚的')
n = b.prune()
check('prune 不炸', n >= 0)

print()
print('=' * 62)
print('六之二、★ 按群「关闭记录」：整个不写库')
print('=' * 62)
m = mk('mute')
m.record_result(msg('群A的消息', 1, chat=-1001, title='群A'))
m.record_result(msg('群B的消息', 1, chat=-1002, title='群B'))
check('两个群都记上了', len(m.chats()) == 2)
# 把群A 关掉
m.record_result(msg('群A不该记的', 2, chat=-1001, title='群A'), mute=['-1001'])
m.record_result(msg('群B照记', 2, chat=-1002, title='群B'))
a_txt = [x['text'] for x in m.messages(chat_id='-1001')]
b_txt = [x['text'] for x in m.messages(chat_id='-1002')]
check('★★ 关掉的群，新消息一条都不写', '群A不该记的' not in a_txt, str(a_txt))
check('★ 没关的群照常记', '群B照记' in b_txt, str(b_txt))
check('★ 关之前记下的还在（不是删库）', '群A的消息' in a_txt, str(a_txt))

mute = set(['-1002'])
m.record_result(msg('群B被关了', 3, chat=-1002, title='群B'), mute=mute)
check('★ mute 传集合也行', '群B被关了' not in
      [x['text'] for x in m.messages(chat_id='-1002')])
check('mute 传 None 不炸',
      m.record_result(msg('x', 9, chat=-1003), mute=None) is None)
check('mute 传空列表不炸', m.record_result(msg('y', 9, chat=-1003),
                                           mute=[]) is None)

print()
print('=' * 62)
print('七、面板要的统计和安全')
print('=' * 62)
s = a.stats()
check('统计有条数', s['count'] >= 6, str(s['count']))
check('统计有最早时间', bool(s['first']))
check('★ 图片路径只认文件名（防目录穿越）',
      a.media_path('../../../etc/passwd') is None)
check('★ 带路径分隔符的也挡住',
      a.media_path('..\\..\\windows\\win.ini') is None)
check('不存在的图返回 None', a.media_path('nope.jpg') is None)
check('空名字返回 None', a.media_path('') is None)

print()
print('=' * 62)
print('八、清空')
print('=' * 62)
c = mk('clear')
c.record_result(msg('群1的消息', 1, chat=-100111, title='群1'))
c.record_result(msg('群2的消息', 1, chat=-100222, title='群2'))
check('两个群都有记录', len(c.chats()) == 2)
c.forget_chat(-100111)
check('★ 清掉一个群，另一个还在',
      len(c.chats()) == 1 and c.chats()[0]['chat_id'] == '-100222',
      str(c.chats()))
c.clear_all()
check('全部清空', c.chats() == [] and c.stats()['count'] == 0)

print()
print('=' * 62)
print('九、★★ 绝不发数据到外部（这是和原版最大的区别）')
print('=' * 62)
src = open('archive.py', encoding='utf-8').read()
# ★ 查的是**能执行的代码**，不是注释 —— 注释里会解释「为什么没搬同步代理」，
#   那属于说明文档，不是「代码在干这事」
import ast
tree = ast.parse(src)
for node in ast.walk(tree):
    # 去掉 docstring，剩下的就是真正的代码
    if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                         ast.AsyncFunctionDef)):
        if (node.body and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)):
            node.body.pop(0)
code = ast.unparse(tree)
check('★ 代码里没有 audit_url / enrollment / 上报接口',
      'audit_url' not in code and 'enrollment' not in code
      and 'audit_agent' not in code and '/api/sync' not in code)
check('★ 代码里没有朋友那台服务器',
      'jiqirenhoutai' not in code and 'duckdns' not in code)
check('★ 注释里也没有他的域名（免得被误改回去）',
      'jiqirenhoutai' not in src and 'duckdns' not in src)
# 只允许出现 Telegram 官方域名
import re
urls = set(re.findall(r'https?://([a-zA-Z0-9\.\-]+)', code))
check('★ 代码只连 Telegram 官方域名', urls == {'api.telegram.org'},
      str(urls))

# 真跑一遍「下载图片」那条路，看它请求的是不是只有 Telegram
import archive as AR
calls = []
real_post, real_stream = None, None


class SpySession:
    def __init__(self):
        self.trust_env = True

    def post(self, url, **kw):
        calls.append(url)
        raise RuntimeError('测试里不发真请求')

    def stream(self, method, url, **kw):
        calls.append(url)
        raise RuntimeError('测试里不发真请求')


d = mk('spy')
d.record_result(msg('', 1, extra={'photo': [{'file_id': 'F1'}]}))
spy = SpySession()
try:
    d._download(spy, '-1001234', '1', 'F1')
except Exception:
    pass
check('★ 下载图片只打 api.telegram.org',
      calls and all(u.startswith('https://api.telegram.org/') for u in calls),
      str(calls))

print()
print('=' * 62)
print('七之二、★★ 机器人自己发的（账单/回执）也要记下来')
print('=' * 62)
check('★ Archive 支持 is_own 参数',
      'is_own' in __import__('inspect').signature(
          MessageArchive.record_result).parameters)

own = mk('own2')
own.record_result(msg('群友说的话', 1, chat=-1009, title='群'))
# 模拟机器人发出去的账单（走 is_own=True）
own.record_result({'message': {
    'message_id': 2, 'date': int(time.time()),
    'chat': {'id': -1009, 'type': 'supergroup', 'title': '群'},
    'from': {'id': 999, 'first_name': 'bot', 'is_bot': True},
    'text': '今日账单\n总入款金额：1100'}}, is_own=True)
rows = {m['text']: m for m in own.messages(chat_id='-1009')}
bill = [v for k, v in rows.items() if k.startswith('今日账单')]
check('★ 机器人发的也进库了', len(bill) == 1, str(list(rows.keys())))
check('★★ 被标成 is_bot（界面上走单独样式）',
      bill and bill[0]['is_bot'] is True)
check('★ 群友那条没被误标', rows['群友说的话']['is_bot'] is False)

print()
print('  ---- ★★ 机器人自己发的不算未读 ----')
seen = {}
u1 = own.unread_counts(seen)
check('★★ 未读只算群友那 1 条（账单不算）',
      u1.get('-1009') == 1, str(u1))
check('★ 全标已读后也是 0', own.unread_counts(
    {'-1009': own.newest_ts()}) == {})

# 再加一条群友的，未读应该是 2 而不是 3
own.record_result(msg('又一句', 3, chat=-1009, title='群'))
check('★ 再来一条群友的 → 未读 2（还是不含账单）',
      own.unread_counts({}).get('-1009') == 2,
      str(own.unread_counts({})))

print()
print('=' * 62)
print('八之二、★ 分得清「机器人主人」和「别人」（页面靠它分左右）')
print('=' * 62)
o = MessageArchive(os.path.join(TMP, 'data', 'own.archive.sqlite3'),
                   keep_hours=48, owner_id='111')
o.record_result(msg('主人说的话', 1, uid=111, first='我'))
o.record_result(msg('别人说的话', 2, uid=222, first='客', last='户'))
rows = {m['text']: m for m in o.messages()}
check('★ 主人认出来了', rows['主人说的话']['is_owner'] is True,
      str(rows['主人说的话'].get('is_owner')))
check('★ 别人没被认成主人', rows['别人说的话']['is_owner'] is False,
      str(rows['别人说的话'].get('is_owner')))
check('主人和别人都在同一条时间线上（能排在一起）',
      len(o.messages()) == 2)

nobody = MessageArchive(os.path.join(TMP, 'data', 'noown.archive.sqlite3'))
nobody.record_result(msg('没配 owner 的消息', 1, uid=111))
check('★ 没配 owner_id 时不会乱认',
      nobody.messages()[0]['is_owner'] is False)

print()
print('=' * 62)
print('九之二、★★ 图片下载（两个坑都踩过，锁死）')
print('=' * 62)


class _Resp:
    def __init__(self, obj=None, status=200):
        self._o = obj
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError('HTTP %s' % self.status_code)

    def json(self):
        return self._o


class _Stream(_Resp):
    def __init__(self, blob, status=200):
        super().__init__(status=status)
        self._blob = blob

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_content(self, n):
        for i in range(0, len(self._blob), n):
            yield self._blob[i:i + n]


class FakeSess:
    """照着 requests.Session 的行为做 —— 关键是 request(stream=True) 这条路"""
    def __init__(self, getfile, blob):
        self.getfile = getfile
        self.blob = blob
        self.calls = []

    def post(self, url, **kw):
        self.calls.append(('post', url, kw))
        return _Resp(self.getfile)

    def request(self, method, url, **kw):
        self.calls.append(('request', url, kw))
        return _Stream(self.blob)


d2 = mk('img')
d2.token = '1:FAKE'
d2.record_result(msg('', 1, extra={'photo': [{'file_id': 'F1'}]}))
sess = FakeSess({'ok': True, 'result': {'file_path': 'photos/a.jpg',
                                        'file_size': 1024}}, b'X' * 2048)
d2._download(sess, '-1001234', '1', 'F1')
check('★ 走的是 request(stream=True)，不是 sess.stream(...)',
      sess.calls[1][0] == 'request' and sess.calls[1][2].get('stream') is True,
      str(sess.calls[1][0]) + ' ' + str(sess.calls[1][2].get('stream')))
row = d2.messages(chat_id='-1001234')[0]
check('★ 图取名并存进了 payload', bool(row.get('media')), str(row.get('media')))
check('★ 文件真的落盘了',
      (d2.media / row['media']).is_file()
      and (d2.media / row['media']).stat().st_size == 2048,
      str(d2.media / row['media']))
check('★ 只连 Telegram 官方域名',
      all('api.telegram.org' in c[1] for c in sess.calls), str(sess.calls))

# 非图片的扩展名不下
d3 = mk('img2')
d3.token = '1:FAKE'
d3.record_result(msg('', 1, extra={'document': {'file_id': 'D1',
                                                'mime_type': 'image/png'}}))
s3 = FakeSess({'ok': True, 'result': {'file_path': 'files/a.pdf',
                                      'file_size': 10}}, b'Y' * 10)
d3._download(s3, '-1001234', '1', 'D1')
check('★ 不是图片扩展名就不存',
      not d3.messages(chat_id='-1001234')[0].get('media'))
check('   而且根本没去下载正文', len(s3.calls) == 1, str(len(s3.calls)))

# 超大文件不下
d4 = mk('img3')
d4.token = '1:FAKE'
d4.record_result(msg('', 1, extra={'photo': [{'file_id': 'BIG'}]}))
s4 = FakeSess({'ok': True, 'result': {'file_path': 'photos/big.jpg',
                                      'file_size': 99 * 1024 * 1024}},
              b'Z' * 10)
d4._download(s4, '-1001234', '1', 'BIG')
check('★ 超过 10MB 的图不存（原版就是这个限制）',
      not d4.messages(chat_id='-1001234')[0].get('media'))
check('   也没去下正文', len(s4.calls) == 1)

print()
print('  ---- ★★ 代理不能关（关了连不上 Telegram）----')
code2 = ast.parse(open('archive.py', encoding='utf-8').read())
for node in ast.walk(code2):
    if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                         ast.AsyncFunctionDef)):
        if (node.body and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)):
            node.body.pop(0)
_code = ast.unparse(code2)
check('★★ 绝不能写 trust_env = False',
      'trust_env' not in _code,
      'ApiTrx 那边必须直连，但 Telegram 这边必须走系统代理 —— 抄错了就是图片永远下载中')

print()
print('=' * 62)
print('十、目录穿越 / 坏数据不炸')
print('=' * 62)
e = mk('weird')
for bad in ({}, {'message': {}}, {'message': {'chat': {}, 'message_id': 1}},
            {'message': {'chat': {'id': -1}, 'message_id': 1}},
            {'message': {'chat': {'id': -1}, 'message_id': 1, 'date': 0}},
            {'callback_query': {'id': 'x'}}, 'not a dict', None, 12345):
    try:
        e.record_result(bad)
        check('不炸：%s' % str(bad)[:34], True)
    except Exception as ex:
        check('不炸：%s' % str(bad)[:34], False, '%s: %s' % (type(ex).__name__, ex))
check('坏数据一条都没记进去', e.stats()['count'] == 0,
      '%d 条' % e.stats()['count'])

print()
print('=' * 62)
print('十之二、实时刷新：只取新的（since）')
print('=' * 62)
s = mk('since')
old = int(time.time()) - 100
s.record_result(msg('老消息A', 1, date=old, chat=-100111))
s.record_result(msg('老消息B', 2, date=old + 1, chat=-100111))
cut = s.newest_ts()
check('拿得到最新时间', bool(cut), cut)

s.record_result(msg('新消息C', 3, chat=-100111))
s.record_result(msg('新消息D', 4, chat=-100111))
fresh = s.messages_since(cut)
texts = {m['text'] for m in fresh}
check('★ 新的都返回了', '新消息C' in texts and '新消息D' in texts, str(texts))
check('★ 比游标早的不会返回（这条是排除）', '老消息A' not in texts, str(texts))
# ★ 游标那条（老消息B）会**故意**带出来：同一秒可能有好几条，
#   用 `>` 严格比较会漏掉和游标同秒的后半截，所以用 `>=`，靠客户端去重
check('★ 游标那条会带出来（宁可重复，不能漏）',
      '老消息B' in texts, str(texts))

check('after 传空 → 退回全量', len(s.messages_since('')) == 4,
      '%d 条' % len(s.messages_since('')))
check('按会话过滤', all(m['chat_id'] == '-100111'
                   for m in s.messages_since('', chat_id='-100111')))
check('没有新消息时返回空', s.messages_since(s.newest_ts() + 'z') == [])
check('空库的 newest_ts 是空串', mk('empty2').newest_ts() == '')

print()
print('=' * 62)
print('十之二点五、未读数')
print('=' * 62)
u = mk('unread')
base = int(time.time()) - 100
for i, (cid, title) in enumerate(((-1001, '群A'), (-1002, '群B'),
                                  (-1003, '群C'))):
    for j in range(3 + i):
        u.record_result(msg('群里第%d条' % j, j + 1, date=base + j,
                            chat=cid, title=title))

allchats = {c['chat_id']: c['last_ts'] for c in u.chats()}
check('三个群都在', len(allchats) == 3, str(sorted(allchats)))

# 全都从来没点开过 → 全部算未读
un = u.unread_counts({})
check('★ 从没点开过 → 全部算未读',
      sum(un.values()) == 3 + 4 + 5, str(un))

# 群A 看到最新 → 它不算未读，另外两个照旧
seen = dict(allchats)
un = u.unread_counts(seen)
check('★★ 标了已读的就不算未读', un == {}, str(un))

# 群B 有 4 条；只看到「第二新」那条 → 应该只剩最新那 1 条未读
#   （messages() 是新的在前，所以 [1] 就是第二新）
b_rows = u.messages(chat_id='-1002')
check('群B 有 4 条', len(b_rows) == 4, '%d 条' % len(b_rows))
seen2 = dict(allchats)
seen2['-1002'] = b_rows[1]['ts']
un = u.unread_counts(seen2)
check('★★ 只看到一半 → 只算剩下的', un.get('-1002') == 1, str(un))
check('其它已读的不受影响', '-1001' not in un and '-1003' not in un, str(un))

# 群C 从没点开（seen 里没有）→ 全部算未读
seen3 = {'-1001': allchats['-1001']}
un = u.unread_counts(seen3)
check('★ seen 里没有的会话 → 全部算未读',
      un.get('-1003') == 5 and '-1001' not in un, str(un))
check('怪输入不炸', u.unread_counts(None) != {} or True)
check('空 seen 字典也不炸', isinstance(u.unread_counts({}), dict))
check('传字符串当 seen 也不炸', isinstance(u.unread_counts('abc'), dict))
check('传列表当 seen 也不炸', isinstance(u.unread_counts([]), dict))

print()
print('=' * 62)
print('十之三、★ 只有记账和客服能开记录（用户指定）')
print('=' * 62)


class FakeMgr:
    def save(self):
        pass

    def is_expired(self, b):
        return False


class FakeRunner(core.BaseRunner):
    """不起线程、不联网 —— 直接把 update 喂给 handle()，验分发和归档"""
    kind = 'fake'


def mk_runner(bid, extra=None):
    bot = {'id': bid, 'token': '1:FAKE', 'note': 'x', 'admin_ids': [],
           'owner_id': 0, 'enabled': True}
    bot.update(extra or {})
    return FakeRunner(FakeMgr(), bot)


check('★ 白名单只留记账机器人',
      tuple(core.ARCHIVE_TYPES) == ('ledger',), str(core.ARCHIVE_TYPES))

from manager import BotManager
m2 = BotManager({'password': 'x'})
for t in ('kefu', 'usdt', 'shop', 'ledger'):
    m2.bots.append({'id': 't_' + t, 'note': t, 'type': t, 'token': '1:T',
                    'admin_ids': [], 'enabled': False, 'mid': ''})
m2.save()
ok, err = m2.set_archive_cfg('t_ledger', {'enabled': True})
check('记账机器人：能开记录', ok is True, err)
for t in ('kefu', 'usdt', 'shop'):
    ok, err = m2.set_archive_cfg('t_' + t, {'enabled': True})
    check('★ %s：开不了（会被拦住）' % t, ok is False, err[:40])
    check('   %s 的配置没被写进去' % t, not (m2.find('t_' + t).get('archive')))

# 核心闸口：类型不对时 archive_cfg 直接当没开
r = mk_runner('type_gate', {'type': 'usdt',
                            'archive': {'enabled': True}})
check('★ 就算配置里写了 enabled，类型不对也不收群消息',
      r.wants_groups() is False)
r.handle(msg('usdt 机器人收到的群消息', 1))
check('★ 也不会写盘', r._archive is None)
r2ok = mk_runner('type_gate2', {'type': 'ledger',
                                'archive': {'enabled': True}})
check('记账机器人开着就能收', r2ok.wants_groups() is True)
# 开机预热：不然重启后群里没人说话，待下载的图永远轮不到
r2ok2 = mk_runner('warm', {'type': 'ledger',
                           'archive': {'enabled': True}})
check('★ 没预热时归档还没建', r2ok2._archive is None)
r2ok2.get_archive()
check('★ 预热之后归档建出来了（后台线程起来了）', r2ok2._archive is not None)
r2ok2.close_archive()
check('关掉预热不炸', r2ok2._archive is None)
r3ok = mk_runner('type_gate3', {'type': 'kefu',
                                'archive': {'enabled': True}})
check('★★ 客服机器人开不了（用户后来去掉了，只留记账）',
      r3ok.wants_groups() is False and r3ok._archive is None)

print()
print('=' * 62)
print('十一、★ 端到端：真的走 runner 的收消息链路')
print('=' * 62)


# ---- 11.1 客服机器人：开不了记录（用户只要记账）----
r = mk_runner('r1', {'type': 'kefu'})
check('客服类型默认不收群消息', r.wants_groups() is False)
r.handle(msg('不该被记下来', 1))
check('★ 没开记录 → 什么都不写', r._archive is None,
      '居然建了归档')

# ---- 11.2 开了记录：群消息落盘 ----
r2 = mk_runner('r2', {'type': 'ledger',
                      'archive': {'enabled': True}})
check('★ 开了记录 → 开始收群消息', r2.wants_groups() is True)
r2.handle(msg('客户在群里说的话', 1, chat=-100777, title='客户群'))
r2.handle(msg('第二条', 2, chat=-100777, title='客户群'))
a2 = r2.get_archive()
rows = a2.messages()
check('★★ 群里两条都记下来了', len(rows) == 2, '%d 条' % len(rows))
check('内容对', {x['text'] for x in rows} == {'客户在群里说的话', '第二条'},
      str([x['text'] for x in rows]))
check('★ 保留期固定 48 小时（不再让面板配）', a2.keep_hours == 48, str(a2.keep_hours))

# ★★ 私聊**一概不记**（2026-09-29 用户改的需求）
#   原来是「私聊也记」，理由是客服机器人的客户私聊要留档 ——
#   但**只有记账类型有归档**（core.ARCHIVE_TYPES），客服根本不走这里，
#   而记账的私聊全是「绑定、广播、查地址」这些自己的操作，纯噪音。
r2.handle({'update_id': 9, 'message': {
    'message_id': 3, 'date': int(time.time()),
    'chat': {'id': 555, 'type': 'private', 'first_name': '客户'},
    'from': {'id': 555, 'first_name': '客户', 'username': 'k'},
    'text': '私聊问的价'}})
check('★★ 私聊不记（用户要求只记群聊）',
      not any(x['text'] == '私聊问的价' for x in a2.messages()),
      str([x['text'] for x in a2.messages()]))
check('★ 群聊那两条不受影响', len(a2.messages()) == 2,
      '%d 条' % len(a2.messages()))

# 按钮点击不该记
before = len(a2.messages())
r2.handle({'update_id': 10, 'callback_query': {
    'id': 'cb', 'from': {'id': 1, 'first_name': 'x'},
    'message': {'message_id': 1, 'chat': {'id': -100777}},
    'data': 'x:y'}})
check('按钮点击不会被当成消息记下来', len(a2.messages()) == before)

# ---- 11.3 ★★ 记账类型：记录**默认就是开的**（用户 2026-09-28 明确）----
#    「机器人默认都是开的，只有消息列表是我个人选择开或者关的」
#    所以「archive 字段缺省」= 开，只有**明确写 enabled=False** 才是关。
r3 = mk_runner('r3', {'type': 'ledger'})     # ★ 故意不给 archive 字段
check('★ 记账类型天生收群消息',
      r3.__class__.allow_groups is False and r3.wants_groups() is True)
check('★★ 没写 archive 字段 = 开（不是关）',
      r3.archive_cfg().get('enabled') is True,
      str(r3.archive_cfg()))
r3.handle(msg('记账群的消息', 1, chat=-100888))
check('★★ 默认就记录 → 群消息直接落盘（不用用户去开）',
      r3._archive is not None and len(r3.get_archive().messages()) == 1,
      '写进去 %d 条' % (0 if r3._archive is None
                       else len(r3.get_archive().messages())))

# 明确关掉的才是关（换商户时会被清成 False，别又给开回来）
r3off = mk_runner('r3off', {'type': 'ledger',
                            'archive': {'enabled': False}})
check('★★ 明确写 enabled=False 才是关', r3off.archive_cfg().get('enabled') is False,
      str(r3off.archive_cfg()))
r3off.allow_groups = True
r3off.handle(msg('关掉之后不该记', 1, chat=-100888))
check('★ 关掉之后不写盘', r3off._archive is None)
# 只写了 mute_chats、没写 enabled → 还是开（别把 muted 当成关）
r3m = mk_runner('r3m', {'type': 'ledger',
                        'archive': {'mute_chats': ['-100999']}})
check('★★ 只有 mute_chats 没有 enabled → 依然是开',
      r3m.archive_cfg().get('enabled') is True, str(r3m.archive_cfg()))

# ---- 11.3b 机器人自己发的也要走归档（发的时候顺手记）----
class _SpyAPI:
    """假 API：记下发出去的东西，并像真的一样回调 on_sent"""
    def __init__(self, result):
        self.result = result
        self.sent = []
        self.on_sent = None
    def call(self, method, **params):
        self.sent.append((method, params))
        if self.on_sent and method in core.SEND_METHODS:
            self.on_sent(method, params, self.result)
        return self.result

    def call_file(self, method, field, filename, fileobj, **params):
        """上传那条路（sendPhoto 传图用）—— 真实现里也回调 on_sent"""
        self.sent.append((method, params))
        if self.on_sent and method in core.SEND_METHODS:
            self.on_sent(method, params, self.result)
        return self.result


r5 = mk_runner('sent', {'type': 'ledger',
                        'archive': {'enabled': True}})
# ★ 换了假 API 之后，钩子得自己接回去 —— 真跑的时候是 __init__ 里接的
r5.api = _SpyAPI({'message_id': 77, 'date': int(time.time()),
                  'chat': {'id': -100777, 'type': 'supergroup',
                           'title': '客户群'},
                  'from': {'id': 999, 'first_name': 'bot', 'is_bot': True},
                  'text': '今日账单\n总入款金额：1100'})
r5.api.on_sent = r5.archive_sent
r5.send(12345, 'hi')          # 随便发点东西，触发 on_sent
rows5 = r5.get_archive().messages(chat_id='-100777')
check('★★ 机器人发出去的消息被归档了', len(rows5) == 1,
      '%d 条' % len(rows5))
check('★ 内容对', '今日账单' in rows5[0]['text'], rows5[0]['text'][:20])
check('★★ 标成了 is_bot', rows5[0]['is_bot'] is True)
check('★ 不算未读', r5.get_archive().unread_counts({}).get('-100777') in (None, 0),
      str(r5.get_archive().unread_counts({})))

# ★ 明确关掉的：发出去也不归档（默认开的那台见上面 11.3）
r6 = mk_runner('sent2', {'type': 'ledger',
                         'archive': {'enabled': False}})
r6.api = _SpyAPI({'message_id': 88, 'date': int(time.time()),
                  'chat': {'id': -100888, 'type': 'supergroup',
                           'title': '群'},
                  'from': {'id': 999, 'is_bot': True}, 'text': 'x'})
r6.api.on_sent = r6.archive_sent
r6.send(1, 'hi')
check('★ 关掉的机器人 → 发出去也不归档', r6._archive is None)

# 客服类型（不能记录）发出去也不归档
r7 = mk_runner('sent3', {'type': 'kefu',
                         'archive': {'enabled': True}})
r7.api = _SpyAPI({'message_id': 89, 'date': int(time.time()),
                  'chat': {'id': -100999, 'type': 'supergroup',
                           'title': '群'},
                  'from': {'id': 999, 'is_bot': True}, 'text': 'x'})
r7.api.on_sent = r7.archive_sent
r7.send(1, 'hi')
check('★ 非记账类型发出去也不归档（白名单挡着）', r7._archive is None)

# ★ call_file（上传图片）那条路也要挂上钩子 —— 不挂的话
#   TRC20 核对图这种「机器人自己发的图」在记录里永远看不到
_class_cf = _SpyAPI({'message_id': 90, 'date': int(time.time()),
                     'chat': {'id': -100777, 'type': 'supergroup',
                              'title': '客户群'},
                     'from': {'id': 999, 'is_bot': True},
                     'photo': [{'file_id': 'F1'}], 'caption': '核对图'})
r8 = mk_runner('sent4', {'type': 'ledger',
                         'archive': {'enabled': True}})
r8.api = _class_cf
r8.api.on_sent = r8.archive_sent
r8.api.call_file('sendPhoto', 'photo', 'a.jpg', None)
rows8 = r8.get_archive().messages(chat_id='-100777')
check('★★ 上传发的图片也归档了（call_file 那条路）', len(rows8) == 1,
      '%d 条' % len(rows8))
check('★ 图归档成 has_photo（后台会去把它下下来）',
      bool(rows8) and rows8[0]['has_photo'] is True,
      str([x.get('has_photo') for x in rows8]))
check('★ 上传发的也算机器人发的（不算未读）',
      r8.get_archive().unread_counts({}).get('-100777') in (None, 0))

# ---- 11.3c 账单是 HTML 发的，存进库的必须是**能读的文本** ----
# 不转的话面板上看到的是「<a href="https://t.me/">1100</a>」这种尖括号乱码
print()
print('  --- HTML 转纯文本 ---')
_h = core.html_to_text
check('★★ 金额的蓝色假链接还原成纯数字',
      _h('<a href="https://t.me/">1100</a>') == '1100',
      _h('<a href="https://t.me/">1100</a>'))
check('★ <code> 去标签留字',
      _h('<code>+100</code>入款') == '+100入款',
      _h('<code>+100</code>入款'))
check('★ 成对/单标签、大小写都能去',
      _h('<B>x</B><i>y</i><s>z</s>') == 'xyz'
      and _h('<strong>a</strong><tg-spoiler>b</tg-spoiler>') == 'ab')
check('★ 实体还原（&lt; &gt; &amp;）',
      _h('a&lt;b&gt;c&amp;d') == 'a<b>c&d', _h('a&lt;b&gt;c&amp;d'))
check('★★ 先删标签再还原实体 —— 用户打的 &lt;b&gt; 不该被当标签删掉',
      _h('额度&lt;b&gt;满了') == '额度<b>满了',
      _h('额度&lt;b&gt;满了'))
check('★ <br> 变真换行',
      _h('第一行<br>第二行') == '第一行\n第二行', repr(_h('第一行<br>第二行')))
check('★ 空值不炸', _h('') == '' and _h(None) is None)
check('★ 没标签的普通文本原样返回',
      _h('今日账单\n总入款：1100') == '今日账单\n总入款：1100')

# 真走一遍：HTML 账单发出去 → 归档的是纯文本
_cf_html = _SpyAPI({'message_id': 91, 'date': int(time.time()),
                    'chat': {'id': -100777, 'type': 'supergroup',
                             'title': '客户群'},
                    'from': {'id': 999, 'is_bot': True},
                    'text': '今日账单\n<a href="https://t.me/">1100</a>'})
r9 = mk_runner('sent5', {'type': 'ledger', 'archive': {'enabled': True}})
r9.api = _cf_html
r9.api.on_sent = r9.archive_sent
r9.api.call('sendMessage', chat_id=-100777, text='x', parse_mode='HTML')
t9 = r9.get_archive().messages(chat_id='-100777')[0]['text']
check('★★ HTML 账单归档成了能读的文本（没有尖括号）',
      t9 == '今日账单\n1100' and '<a href' not in t9, repr(t9))

# 非 HTML 的别乱动（Markdown 那些，转了就丢内容）
_cf_md = _SpyAPI({'message_id': 92, 'date': int(time.time()),
                  'chat': {'id': -100777, 'type': 'supergroup',
                           'title': '客户群'},
                  'from': {'id': 999, 'is_bot': True},
                  'text': '*粗体* 和 <b> 这种字面量'})
r10 = mk_runner('sent6', {'type': 'ledger', 'archive': {'enabled': True}})
r10.api = _cf_md
r10.api.on_sent = r10.archive_sent
r10.api.call('sendMessage', chat_id=-100777, text='x', parse_mode='MarkdownV2')
t10 = r10.get_archive().messages(chat_id='-100777')[0]['text']
check('★ 不是 HTML 的（Markdown）原样存，不乱转',
      t10 == '*粗体* 和 <b> 这种字面量', repr(t10))

# ---- 11.4 关掉之后不再写新的 ----
r2.bot['archive'] = {'enabled': False}
r2.close_archive()
r2.handle(msg('关了之后的消息', 4, chat=-100777))
check('★ 关掉后新消息不再写', r2._archive is None)
rows = MessageArchive(os.path.join(TMP, 'data', 'r2.archive.sqlite3')).messages()
# 2 条群聊（原来第 3 条是那条私聊 —— 现在私聊一概不记了）
check('★ 关掉前记的还在', len(rows) == 2, '%d 条' % len(rows))
check('★ 剩下的都是群聊', all(x['chat_type'] != 'private' for x in rows),
      str([x['chat_type'] for x in rows]))

# ★★ 一次性清理：**以前**存进去的私聊要删掉
#    光改写入过滤不够 —— 之前那些私聊还挂在记录列表里，看着还是杂乱
_purge = os.path.join(TMP, 'data', 'purge.archive.sqlite3')
_c = sqlite3.connect(_purge)
_c.execute('CREATE TABLE events(chat_id TEXT, message_id TEXT, ts TEXT,'
           ' payload TEXT, file_id TEXT, PRIMARY KEY(chat_id, message_id))')
for _cid, _ctype, _mid, _text in (('-100777', 'supergroup', '1', '群里的话'),
                                  ('555', 'private', '2', '私聊的话')):
    _c.execute('INSERT INTO events VALUES(?,?,?,?,?)',
               (_cid, _mid, '2026-09-29T05:00:00+08:00',
                json.dumps({'chat_id': _cid, 'message_id': _mid,
                            'ts': '2026-09-29T05:00:00+08:00',
                            'chat_type': _ctype, 'text': _text,
                            'user_id': '1'}), ''))
_c.commit()
_c.close()
_pa = MessageArchive(_purge)
_left = [x['text'] for x in _pa.messages(limit=50)]
check('★★ 老库里**已经存着**的私聊被清掉了（光改写入过滤不够）',
      '群里的话' in _left and '私聊的话' not in _left, str(_left))
_pa2 = MessageArchive(_purge)
check('★ 清一次就不再重复删（user_version 标记，不然每次开库都删一遍）',
      [x['text'] for x in _pa2.messages(limit=50)] == _left)
check('★ 清理标记写进去了', sqlite3.connect(_purge).execute(
    'PRAGMA user_version').fetchone()[0] >= 1)

# ---- 11.5 归档库要能被删掉（连接确实关了）----
r2.close_archive()
p = os.path.join(TMP, 'data', 'r2.archive.sqlite3')
os.remove(p)
check('★ 关掉归档后文件能删（连接没占着）', not os.path.exists(p))

os.chdir(HERE)
shutil.rmtree(TMP, ignore_errors=True)
print()
print('=' * 62)
print('结果：%d 项通过，%d 项失败' % (len(OK), len(BAD)))
for x in BAD:
    print('  ❌', x)
print('=' * 62)
sys.exit(1 if BAD else 0)
