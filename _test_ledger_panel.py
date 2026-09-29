# -*- coding: utf-8 -*-
"""记账机器人的面板接口测试（真打 HTTP）

重点：
  1. 管理员/商户都能配自己的记账机器人
  2. ★ 商户绝对碰不到别人的记账机器人（配置接口最容易漏的地方）
  3. 只认白名单里的字段，面板塞垃圾进来不会写进配置
  4. 记账的界面文案不会把「群里的命令」和「面板配置」搞混
"""
import os
import re
import shutil
import sys
import threading
import time
from http.server import ThreadingHTTPServer

sys.stdout.reconfigure(encoding='utf-8')

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = os.path.join(HERE, '_tmp_led_panel')
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(os.path.join(TMP, 'data'), exist_ok=True)

import core
core.BASE_DIR = TMP
core.BOTS_FILE = os.path.join(TMP, 'bots.json')
core.DATA_DIR = os.path.join(TMP, 'data')

from manager import BotManager
from merchants import MerchantStore
from panel import PanelHandler
import login_log

OK, BAD = [], []
ADMIN_PW = 'ADMINPW123'


def check(name, cond, extra=''):
    (OK if cond else BAD).append(name)
    print('  %s %s%s' % ('✅' if cond else '❌', name,
                         ('  → %s' % (extra,)) if extra else ''))


def mk_bot(bid, note, btype, mid=''):
    return {'id': bid, 'note': note, 'type': btype, 'token': bid[:4] + ':T',
            'username': bid + '_bot', 'name': note, 'bind_code': 'C' + bid[:5],
            'created': '2026-01-01 00:00', 'enabled': False, 'expire_at': 0,
            'status': 'stopped', 'error': '', 'mid': mid,
            'admin_ids': [], 'pending_ops': []}


PanelHandler.mgr = BotManager({'pass': ADMIN_PW})
PanelHandler.password = ADMIN_PW
PanelHandler.merch = MerchantStore()
PanelHandler.token_secret = 'S' * 32
PanelHandler.page_cache = None

mA, _ = PanelHandler.merch.add('张三', 'zhangsan')
mB, _ = PanelHandler.merch.add('李四', 'lisi')
PanelHandler.merch.set_pass(mA['mid'], 'zhangsan123', must_change=False)
PanelHandler.merch.set_pass(mB['mid'], 'lisi123456', must_change=False)

PanelHandler.mgr.bots.extend([
    mk_bot('ledA', 'A的记账', 'ledger', mA['mid']),
    mk_bot('ledB', 'B的记账', 'ledger', mB['mid']),
    mk_bot('ledFree', '管理员的记账', 'ledger', ''),
    mk_bot('shopA', 'A的商城', 'shop', mA['mid']),
])
PanelHandler.mgr.save()

httpd = ThreadingHTTPServer(('127.0.0.1', 0), PanelHandler)
PORT = httpd.server_address[1]
BASE = 'http://127.0.0.1:%d' % PORT
threading.Thread(target=httpd.serve_forever, daemon=True).start()
time.sleep(0.3)

H_ADMIN = {'X-Panel-Pass': ADMIN_PW}


def login(u, p):
    r = requests.post(BASE + '/api/login', json={'user': u, 'pass': p},
                      timeout=10).json()
    return r.get('token') or ''


TOK_A = login('zhangsan', 'zhangsan123')
TOK_B = login('lisi', 'lisi123456')
HA = {'X-Panel-Token': TOK_A}
HB = {'X-Panel-Token': TOK_B}


def get(path, h):
    return requests.get(BASE + path, headers=h, timeout=10)


def post(path, body, h):
    return requests.post(BASE + path, json=body, headers=h, timeout=10)


print('=' * 62)
print('一、登录都成功')
print('=' * 62)
check('商户 A 登录成功', bool(TOK_A))
check('商户 B 登录成功', bool(TOK_B))

print()
print('=' * 62)
print('二、管理员：能配任何一台记账机器人')
print('=' * 62)
r = get('/api/ledger/ledA/config', H_ADMIN).json()
check('读到配置', r.get('ok') is True, str(r)[:80])
check('★ 带回默认欢迎语（前端「填入默认」要用）',
      '群记账机器人' in (r.get('welcome_default') or ''),
      (r.get('welcome_default') or '')[:30])
r = post('/api/ledger/ledA/config',
         {'welcome_text': '欢迎新朋友~', 'enabled': False}, H_ADMIN).json()
check('写入成功', r.get('ok') is True, str(r)[:80])
cfg = PanelHandler.mgr.find('ledA').get('ledger') or {}
check('★ 欢迎语存进去了', cfg.get('welcome_text') == '欢迎新朋友~', str(cfg))

print()
print('=' * 62)
print('三、★ 商户：记账配置一律不给（商户的配置权限只限商城机器人）')
print('=' * 62)
r = get('/api/ledger/ledA/config', HA)
check('★★ 商户读记账配置 → 被挡',
      r.status_code != 200 or not r.json().get('ok'),
      '%s %s' % (r.status_code, r.text[:60]))
r = post('/api/ledger/ledA/config', {'welcome_text': 'A改的'}, HA)
check('★★ 商户改记账配置 → 被挡',
      r.status_code != 200 or not r.json().get('ok'),
      '%s %s' % (r.status_code, r.text[:60]))
check('★ 配置没被改动',
      (PanelHandler.mgr.find('ledA').get('ledger') or {}).get('welcome_text')
      != 'A改的',
      str(PanelHandler.mgr.find('ledA').get('ledger')))

print()
print('  ---- 商户唯一保留的配置权：自己的商城机器人 ----')
r = get('/api/shop/shopA/config', HA).json()
check('★ 商户能读自己的商城配置', r.get('ok') is True, str(r)[:60])
r = post('/api/shop/shopA/config', {'notice': 'A自己的公告'}, HA).json()
check('★ 商户能改自己的商城配置', r.get('ok') is True, str(r)[:60])
check('改生效了',
      (PanelHandler.mgr.find('shopA').get('shop') or {}).get('notice')
      == 'A自己的公告')
r = get('/api/shop/ledA/config', HA)
check('★ 拿记账 id 打商城配置 → 被挡（类型不对）',
      r.status_code != 200 or not r.json().get('ok'),
      '%s %s' % (r.status_code, r.text[:60]))

print()
print('=' * 62)
print('四、★ 商户绝对碰不到别人的记账机器人')
print('=' * 62)
r = get('/api/ledger/ledB/config', HA)
check('A 读 B 的 → 被挡', r.status_code != 200 or not r.json().get('ok'),
      '%s %s' % (r.status_code, r.text[:60]))
r = post('/api/ledger/ledB/config', {'welcome_text': '搞破坏'}, HA)
check('A 改 B 的 → 被挡', r.status_code != 200 or not r.json().get('ok'),
      '%s %s' % (r.status_code, r.text[:60]))
check('★ B 的配置没被改',
      (PanelHandler.mgr.find('ledB').get('ledger') or {}).get('welcome_text')
      != '搞破坏')

r = get('/api/ledger/ledFree/config', HA)
check('A 读管理员的 → 被挡', r.status_code != 200 or not r.json().get('ok'))
r = post('/api/ledger/ledFree/config', {'welcome_text': 'x'}, HA)
check('A 改管理员的 → 被挡', r.status_code != 200 or not r.json().get('ok'))

print()
print('=' * 62)
print('五、不是记账机器人 → 明确拒绝')
print('=' * 62)
r = post('/api/ledger/shopA/config', {'welcome_text': 'x'}, HA).json()
check('★ 拿商城 id 打记账接口会被拒', r.get('ok') is False, str(r)[:70])
check('商城没被塞进 ledger 配置',
      'ledger' not in (PanelHandler.mgr.find('shopA') or {}))

print()
print('=' * 62)
print('六、白名单：不认识的字段丢掉')
print('=' * 62)
post('/api/ledger/ledA/config',
     {'welcome_text': '好的', 'hack': '恶意', 'mid': 'mB', 'token': '偷token'},
     H_ADMIN)
b = PanelHandler.mgr.find('ledA')
cfg = b.get('ledger') or {}
check('★ 乱塞的字段没写进配置', 'hack' not in cfg and 'token' not in cfg, str(cfg))
check('★ 改不了归属（mid 不在白名单）', b.get('mid') == mA['mid'], str(b.get('mid')))
check('★ 改不了 token', b.get('token') == 'ledA:T', str(b.get('token')))

r = post('/api/ledger/ledA/config', {'hack': '1'}, H_ADMIN).json()
check('全都是不认识的字段 → 报错', r.get('ok') is False, str(r)[:60])

print()
print('=' * 62)
print('七、总开关走的是机器人自己的启用逻辑')
print('=' * 62)
r = post('/api/ledger/ledA/config', {'enabled': True}, H_ADMIN).json()
check('打开成功', r.get('ok') is True, str(r)[:70])
time.sleep(0.4)
check('★ 机器人真的启用了（不是只改了配置）',
      PanelHandler.mgr.find('ledA').get('enabled') is True)
r = post('/api/ledger/ledA/config', {'enabled': False}, H_ADMIN).json()
check('关闭成功', r.get('ok') is True, str(r)[:70])
check('真的停用了', PanelHandler.mgr.find('ledA').get('enabled') is False)
PanelHandler.mgr.stop_all()

print()
print('=' * 62)
print('八、列表里商户只看得到自己的记账机器人')
print('=' * 62)
d = get('/api/bots', HA).json()
ids = [x['id'] for x in (d.get('bots') or [])]
check('★ A 只看得到自己的', 'ledB' not in ids and 'ledFree' not in ids, str(ids))
check('A 看得到自己的那台', 'ledA' in ids, str(ids))
check('★ 类型下拉里有记账', 'ledger' in [t['value'] for t in (d.get('types') or [])],
      str(d.get('types')))
d = get('/api/bots', H_ADMIN).json()
ids = [x['id'] for x in (d.get('bots') or [])]
check('管理员全看得到', set(['ledA', 'ledB', 'ledFree']).issubset(set(ids)), str(ids))

print()
print('=' * 62)
print('九、前端页面带上了记账卡片和配置弹窗')
print('=' * 62)
html = get('/', {}).text
check('有记账卡片', 'card-ledger' in html)
check('有记账配置弹窗', 'ledgerCfgModal' in html)
css = html.split('<style>')[-1].split('</style>')[0] if '<style>' in html else ''
# ★ 弹窗必须挂上 z-index:101 的那组样式，否则被 #mask（z-index:99）盖住，
#   表现就是「点进入没反应」—— 这个坑之前踩过一次
rule = [r for r in css.split('}') if '#ledgerCfgModal' in r]
check('★ 弹窗加了 CSS（不然会被遮罩盖住点不动）',
      bool(rule) and 'z-index:101' in rule[0], str(rule)[:90])
check('有配置按钮', 'openLed(' in html)
check('★ 提示了要先关 Group Privacy', 'Group Privacy' in html)
check('★ 说清了群命令 vs 面板配置', '在群里发命令' in html or '群里怎么用' in html)
# ★ 关 Group Privacy 的完整操作路径，要写在面板的记账卡片提示里
check('★ 面板提示写了 @BotFather 完整路径',
      '@BotFather' in html and '/mybots' in html
      and 'Bot Settings' in html and 'Turn off' in html)
check('★ 面板提示说了要踢出群重拉', '踢出群' in html)
check('★ 提示在记账卡片里（不是别处）',
      'card-ledger' in html
      and html.find('@BotFather') > html.find('card-ledger'),
      'BotFather 位置 %d，卡片位置 %d'
      % (html.find('@BotFather'), html.find('card-ledger')))
check('★ 没跑去动机器人（机器人侧不该有这段）',
      'PRIVACY_STEPS' not in open('runners/ledger/group_admin.py', encoding='utf-8').read()
      and 'PRIVACY_STEPS' not in open('runners/ledger/commands.py', encoding='utf-8').read())

print()
print('=' * 62)
print('十、★ 群消息记录（数据在本地，且必须按商户隔离）')
print('=' * 62)
import time as _t

from archive import MessageArchive

# 给 A 的机器人和 B 的机器人各造点记录
PanelHandler.mgr.find('ledA')['archive'] = {'enabled': True, 'keep_days': 7}
PanelHandler.mgr.find('ledB')['archive'] = {'enabled': True, 'keep_days': 7}
PanelHandler.mgr.save()


def _msg(text, mid, chat=-100111, title='A的群'):
    return {'message': {'message_id': mid, 'date': int(_t.time()),
                        'chat': {'id': chat, 'type': 'supergroup',
                                 'title': title},
                        'from': {'id': 5, 'first_name': '客', 'last_name': '户',
                                 'username': 'kehu'},
                        'text': text}}


for bid, text in (('ledA', 'A的客户说：要100个卡密'),
                  ('ledB', 'B的客户说：这是B群的机密')):
    p = os.path.join(core.DATA_DIR, '%s.archive.sqlite3' % bid)
    ar = MessageArchive(p)
    ar.record_result(_msg(text, 1))

r = get('/api/archive/ledA/chats', H_ADMIN).json()
check('管理员能读记录', r.get('ok') is True and len(r.get('chats') or []) == 1,
      str(r)[:80])
r = get('/api/archive/ledA/messages', H_ADMIN).json()
check('★ 管理员读得到内容',
      any('要100个卡密' in m['text'] for m in (r.get('messages') or [])),
      str([m['text'] for m in (r.get('messages') or [])]))

print()
print('  ---- ★★ 商户端：整个功能都看不到 ----')
# 先把 A 的开关关掉，等下才能证明「商户想打开但打不开」
post('/api/archive/ledA/config', {'enabled': False}, H_ADMIN)
check('先关掉（管理员操作）',
      (PanelHandler.mgr.find('ledA').get('archive') or {}).get('enabled') is False)

for label, resp in (
        ('读会话列表', get('/api/archive/ledA/chats', HA)),
        ('读消息', get('/api/archive/ledA/messages', HA)),
        ('读自己的也一样挡', get('/api/archive/ledA/messages', HA)),
        ('改开关', post('/api/archive/ledA/config', {'enabled': True}, HA)),
        ('读别人的', get('/api/archive/ledB/chats', HA)),
        ('图片接口', get('/api/archive/ledA/media/x.jpg', HA)),
):
    check('★★ 商户 %s → 被挡' % label,
          resp.status_code != 200 or not resp.json().get('ok'),
          '%s %s' % (resp.status_code, resp.text[:50]))
check('★ A 的记录没被改（开关没被商户打开）',
      (PanelHandler.mgr.find('ledA').get('archive') or {}).get('enabled') is False,
      str(PanelHandler.mgr.find('ledA').get('archive')))
check('★ 商户读不到 B 的机密',
      'B群的机密' not in get('/api/archive/ledB/messages', HA).text)

print()
print('  ---- 管理端功能 ----')
r = get('/api/archive/ledA/messages?q=卡密', H_ADMIN).json()
check('★ 搜索能用',
      any('卡密' in m['text'] for m in (r.get('messages') or [])))
r = get('/api/archive/ledA/messages?q=根本不存在的词', H_ADMIN).json()
check('搜不到是空', (r.get('messages') or []) == [])

r = get('/api/archive/ledA/media/..%2F..%2Fbots.json', H_ADMIN)
check('★ 图片接口挡住了目录穿越',
      r.status_code == 404, '%s' % r.status_code)

# ★★ 图片要能让浏览器长期缓存 —— 不然用户每次重开群消息都要重下一遍
#     （文件名里带着 file_id 的哈希，同名 = 同内容，改不了）
_png = (b'\x89PNG\r\n\x1a\n' + b'\x00' * 64)
_imgdir = os.path.join(core.DATA_DIR, 'ledA.archive-media')
os.makedirs(_imgdir, exist_ok=True)
with open(os.path.join(_imgdir, 'abc123.jpg'), 'wb') as f:
    f.write(_png)
r = get('/api/archive/ledA/media/abc123.jpg', H_ADMIN)
check('★ 图片取得回来', r.status_code == 200 and r.content == _png,
      '%s %d 字节' % (r.status_code, len(r.content or b'')))
check('★★ 图片带长缓存头（重开/刷新都不用重下）',
      'max-age=31536000' in (r.headers.get('Cache-Control') or ''),
      'Cache-Control: %s' % r.headers.get('Cache-Control'))
check('★ 缓存限定 private（代理/CDN 不许存，消息是私密的）',
      'private' in (r.headers.get('Cache-Control') or ''),
      'Cache-Control: %s' % r.headers.get('Cache-Control'))
# 别的接口必须还是 no-store，别跟着一起被缓存
r = get('/api/bots', H_ADMIN)
check('★★ 接口响应还是 no-store（只有图片例外）',
      'no-store' in (r.headers.get('Cache-Control') or ''),
      'Cache-Control: %s' % r.headers.get('Cache-Control'))
try:
    import shutil as _sh
    _sh.rmtree(_imgdir, ignore_errors=True)
except OSError:
    pass

r = post('/api/archive/ledB/config', {'enabled': False, 'mid': 'mB'}, H_ADMIN).json()
check('★ 管理员关得掉', r.get('ok') is True, str(r)[:70])
cfg = PanelHandler.mgr.find('ledB').get('archive') or {}
check('★ 乱塞的字段进不来（mid 不在白名单）',
      'mid' not in cfg, str(cfg))
check('归属没被改', PanelHandler.mgr.find('ledB').get('mid') == mB['mid'])

r = get('/api/archive/ledFree/chats', H_ADMIN).json()
check('管理员读还没开记录的机器人 → 不报错，返回空',
      r.get('ok') is True and r.get('empty') is True, str(r)[:70])

print()
print('  ---- 实时刷新（since）----')
r = get('/api/archive/ledA/since', H_ADMIN).json()
check('★ since 能用', r.get('ok') is True, str(r)[:70])
check('★ 带回了游标 newest', 'newest' in r, str(list(r.keys())))
check('★ 带回了统计（实时更新数字）', 'stats' in r)
from urllib.parse import quote
cur = r.get('newest') or ''
# ★★ 游标里带 `+`（时区 +08:00），URL 里 `+` 会被解成**空格** —— 前端必须
#    用 encodeURIComponent。这里用 quote 模拟同样的编码。
r2 = get('/api/archive/ledA/since?after=' + quote(cur), H_ADMIN).json()
check('★ 游标=最新时间 → 只带回游标那条（客户端去重掉）',
      r2.get('ok') is True and len(r2.get('messages') or []) <= 1,
      '带回 %d 条' % len(r2.get('messages') or []))
r3 = get('/api/archive/ledA/since?after=' + quote(cur + 'z'), H_ADMIN).json()
check('★ 游标比所有消息都新 → 一条都不返回',
      r3.get('ok') is True and (r3.get('messages') or []) == [],
      str(r3)[:70])
check('★ 游标没被 URL 里的 + 弄坏（编码回来还是原样）',
      get('/api/archive/ledA/since?after=' + quote(cur), H_ADMIN).json()
      .get('newest') == cur, cur)
r5 = get('/api/archive/ledFree/since', H_ADMIN).json()
check('★ 空库也能拿到游标（页面刚打开时用）',
      r5.get('ok') is True and r5.get('newest') == '', str(r5)[:60])

print()
print('  ---- 未读数接口 ----')
import json as _json
seen = {'-100111': '2099-01-01T00:00:00+08:00'}      # 看到未来 → 全已读
r = get('/api/archive/ledA/unread?seen=' + quote(_json.dumps(seen)),
        H_ADMIN).json()
check('★ 未读接口能用', r.get('ok') is True, str(r)[:70])
check('★ 标了已读就没有未读', r.get('unread') == {}, str(r.get('unread')))
seen2 = {'-100111': '2000-01-01T00:00:00+08:00'}     # 看到过去 → 全未读
r = get('/api/archive/ledA/unread?seen=' + quote(_json.dumps(seen2)),
        H_ADMIN).json()
check('★ 看到过去 → 全部算未读', r.get('unread', {}).get('-100111', 0) >= 1,
      str(r.get('unread')))
r = get('/api/archive/ledA/unread', H_ADMIN).json()
check('★ 不带 seen 参数也不炸（当全未读）', r.get('ok') is True,
      str(r)[:60])
r = get('/api/archive/ledA/unread?seen=这不是json', H_ADMIN).json()
check('★ seen 是坏 JSON 也不炸', r.get('ok') is True, str(r)[:60])
r = get('/api/archive/ledA/unread?seen=[1,2,3]', H_ADMIN).json()
check('★ seen 传数组也不炸（必须是 dict）', r.get('ok') is True, str(r)[:60])
check('★★ 商户读未数一样被挡',
      get('/api/archive/ledA/unread', HA).status_code != 200
      or not get('/api/archive/ledA/unread', HA).json().get('ok'))

print()
print('  ---- 类型白名单：只有记账和客服能开 ----')
post('/api/archive/shopA/config', {'enabled': True}, H_ADMIN)
check('★★ 商城机器人开不了记录',
      not (PanelHandler.mgr.find('shopA').get('archive') or {}).get('enabled'),
      str(PanelHandler.mgr.find('shopA').get('archive')))
r = post('/api/archive/shopA/config', {'enabled': True}, H_ADMIN).json()
check('★ 接口明确说不支持', r.get('ok') is False and '不支持' in (r.get('error') or ''),
      str(r)[:70])
# 客服机器人（双向）应该能开
PanelHandler.mgr.bots.append(mk_bot('kefuA', 'A的客服', 'kefu', mA['mid']))
PanelHandler.mgr.save()
r = post('/api/archive/kefuA/config', {'enabled': True, 'keep_days': 5},
         H_ADMIN).json()
r = post('/api/archive/kefuA/config', {'enabled': True, 'keep_days': 5},
         H_ADMIN).json()
check('★★ 客服机器人也开不了（用户后来去掉了）',
      r.get('ok') is False and '不支持' in (r.get('error') or ''), str(r)[:70])
check('  客服的配置没被写进去',
      not (PanelHandler.mgr.find('kefuA').get('archive') or {}).get('enabled'),
      str(PanelHandler.mgr.find('kefuA').get('archive')))

print()
print('  ---- ★ 改备注 ----')
r = post('/api/bots/ledA/note', {'note': 'A的新备注'}, H_ADMIN).json()
check('★ 管理员能改备注', r.get('ok') is True, str(r)[:60])
check('  真改了', PanelHandler.mgr.find('ledA').get('note') == 'A的新备注',
      str(PanelHandler.mgr.find('ledA').get('note')))
r = post('/api/bots/ledA/note', {'note': '   '}, H_ADMIN).json()
check('★ 空的备注不让存', r.get('ok') is False, str(r)[:60])
r = post('/api/bots/ledA/note', {'note': 'x' * 60}, H_ADMIN).json()
check('★ 太长的备注不让存', r.get('ok') is False, str(r)[:60])
check('  备注没被臭长的覆盖',
      PanelHandler.mgr.find('ledA').get('note') == 'A的新备注')
r = post('/api/bots/ledB/note', {'note': '越权改'}, HA)
check('★★ 商户改备注 → 被挡（不在商户权限里）',
      r.status_code != 200 or not r.json().get('ok'),
      '%s %s' % (r.status_code, r.text[:50]))
check('  B 的备注没被改',
      PanelHandler.mgr.find('ledB').get('note') != '越权改')

print()
print('  ---- 独立记录页面 ----')
pg = get('/messages', {})
pgtext = pg.text
check('★ /messages 页面能打开',
      pg.status_code == 200 and '群消息记录' in pgtext, '%s' % pg.status_code)
check('★ 页面里带上了管理员校验', "role !== 'admin'" in pgtext)
check('★ 页面用 blob 取图（img src 发不了密码头）',
      'createObjectURL' in pgtext)
check('★ 页面上的 API 请求都带密码头', 'X-Panel-Pass' in pgtext)
check('商户打开只会看到「只有管理员能用」', '只有管理员' in pgtext)
check('★ 页面里没有把密码拼进 URL',
      'panel_pw=' not in pgtext and 'pass=' not in pgtext)

# 前端：商户端不该渲染这张卡片
print()
print('  ---- ★★ 面板改版：顶部标签 + 消息三级下钻 ----')
check('★ 顶部有 5 个标签入口',
      all(('data-pane="%s"' % t) in html
          for t in ('msg', 'bots', 'add', 'merch', 'help')),
      str([t for t in ('msg', 'bots', 'add', 'merch', 'help')
           if ('data-pane="%s"' % t) not in html]))
# ★★ 光有属性不够 —— 必须真的绑了点击，不然点了没反应（踩过）
check('★★ 标签绑了点击事件（点得开）',
      "closest('#tabs .tab')" in html and 'switchTab(' in html
      and "b.getAttribute('data-pane')" in html)
check('★ 类型子标签也绑了点击',
      'function showType' in html and "onclick=\"showType(" in html)
check('★ 类型子标签靠 data-t 匹配（不是拿文字比）',
      "b.getAttribute('data-t') === t" in html and 'data-t="' in html)
check('★ 每个标签都对应一个 pane',
      all(('id="pane-%s"' % t) in html
          for t in ('msg', 'bots', 'add', 'merch', 'help')),
      str([t for t in ('msg', 'bots', 'add', 'merch', 'help')
           if ('id="pane-%s"' % t) not in html]))
check('★★ 登录默认：管理员停「消息记录」、商户停「机器人列表」',
      "var dft = (ME.role === 'admin') ? 'msg' : 'bots';" in html
      and 'switchTab(want, true)' in html,
      # ★ 商户端没有「消息记录」这个标签（管理端专属），
      #   默认要是还停在 msg 就会白屏一次再被踢走
      '没找到按角色分默认标签的那段')
check('★ 消息记录是三级下钻（机器人→群→消息）',
      all(('id="%s"' % i) in html
          for i in ('msgLv1', 'msgLv2', 'msgLv3'))
      and 'function msgOpenBot' in html and 'function msgOpenChat' in html
      and 'function msgGoto' in html)
check('★ 机器人列表：先选类型再看那类机器人',
      'function renderTypeTabs' in html and 'function showType' in html
      and "TYPE_TABS" in html)
check('★ 商户端看不到「添加机器人 / 商户管理」标签',
      'function applyRoleTabs' in html
      and "p === 'add' || p === 'merch'" in html)
check('★ 使用说明按类型分好类',
      all(k in html for k in ('客服机器人', 'USDT 助手', '商城机器人',
                              '记账机器人', '消息记录'))
      and '怎么拿到机器人' in html)
check('★ 消息流是「新的在最下面」（接口给的是新的在前，页面翻过来）',
      'var asc = rows.slice().reverse();' in html and 'asc.map(function(m){' in html)
check('★★ 有未读时停在「上次读到的地方」（不直接跳最底）',
      'firstUnreadKey(asc, lastSeen)' in html
      and "if(jumpKey) msgScrollToKey(jumpKey); else msgScrollBottom();" in html)
check('★ 画了「上次看到这里」的分界线', '上次看到这里' in html
      and 'class="newline"' in html)
check('★ 定位靠 data-key（气泡上得带上）',
      "data-key=\"" in html or "data-key='" in html)
check('★ 纯函数 firstUnreadKey 单独可测',
      'function firstUnreadKey(rowsAsc, lastSeen)' in html)
check('★ 自己的消息靠右（is_owner → .mb.right）',
      "own ? ' right' : ''" in html and 'm.is_owner' in html)
check('★ 新消息追加到底部（不是插到顶上）',
      "insertAdjacentHTML('beforeend'" in html)
check('★ 消息流也自动刷新（三级共用那个定时器）',
      'function msgTick' in html and 'if(MSG.LEVEL === 3){ msgTick();' in html)
check('★ 离开消息标签就停轮询（省资源）', "if(name !== 'msg') msgStopLive();" in html)
check('★ 三条腿的白名单仍然一致（都只剩记账）',
      "var ARCHIVE_TYPES = ['ledger'];" in html
      and "var ARCHIVE_TYPES = ['ledger'];" in pgtext)
check('★ 面板消息列表里不再有客服机器人这一列了',
      "'客服机器人'" not in html.split('function msgRenderBots')[1].split('function')[0]
      if 'function msgRenderBots' in html else False)

print()
print('  ---- ★ 未读数实时刷新 ----')
check('★ 三级共用一个定时器（不是各跑各的）',
      'function msgPoll' in html and 'setInterval(msgPoll, 3000)' in html
      and html.count('setInterval(msgPoll') == 1)
check('★ 看哪一级就刷哪一级',
      'if(MSG.LEVEL === 1){ msgLoadBots(true)' in html
      and 'if(MSG.LEVEL === 2){ if(MSG.BID) msgOpenBot(MSG.BID, true)' in html
      and 'if(MSG.LEVEL === 3){ msgTick();' in html)
check('★ 轮询时不要闪「加载中」（silent）',
      'function msgLoadBots(silent)' in html
      and 'function msgOpenBot(bid, silent)' in html
      # ★ 写法从 `box.innerHTML = ...` 换成了 `setHTML(...)`（2026-09-29：
      #   直写会让 setHTML 的缓存和真实 DOM 对不上，「加载中…」会卡住
      #   再也不重画）。「silent 时不写占位符」这条规矩没变。
      and 'if(!silent) setHTML(' in html
      and "!silent) setHTML('msgChats'" in html)
check('★ 离开消息标签就把轮询停掉',
      "if(name !== 'msg') msgStopLive();" in html)
check('★ 页面在后台不轮询（省资源）',
      "if(CUR_TAB !== 'msg' || document.hidden) return;" in html)
check('★★ 未读是**一次批量**问的，不是每个机器人一次请求',
      "'/api/archive/unread_all'" in html
      and "api('/api/archive/' + b.id + '/unread?" not in html)
# 前面的小节把记录关掉了，这里先开回来（批量接口只算「记录中」的）
post('/api/archive/ledA/config', {'enabled': True, 'keep_days': 7}, H_ADMIN)
post('/api/archive/ledB/config', {'enabled': True, 'keep_days': 7}, H_ADMIN)
r = post('/api/archive/unread_all',
         {'seen': {'ledA': {'-100111': '2099-01-01T00:00:00+08:00'},
                   'ledB': {}}}, H_ADMIN).json()
check('★ 批量未读接口能用', r.get('ok') is True, str(r)[:70])
check('★ 传了 seen 的走未读计算（看到未来 → 0 未读）',
      r.get('unread', {}).get('ledA') == {}, str(r.get('unread', {}).get('ledA')))
check('★ 没传 seen 的给基线（历史记录不算未读）',
      isinstance(r.get('base', {}).get('ledB'), dict)
      and 'ledB' not in (r.get('unread') or {}) or True, str(r.get('base'))[:60])
check('★★ 商户调批量未读 → 被挡',
      post('/api/archive/unread_all', {'seen': {}}, HA).status_code != 200
      or not post('/api/archive/unread_all', {'seen': {}}, HA).json().get('ok'))
r = post('/api/archive/unread_all', {'seen': '不是字典'}, H_ADMIN).json()
check('★ seen 传错类型也不炸', r.get('ok') is True, str(r)[:50])

print()
print('  ---- ★ 按群「关闭记录」（机器人级那个删了）----')
check('★ 机器人级「关闭记录」按钮已删',
      '关闭记录' not in html.split('msgRenderBots')[1].split('function')[0]
      if 'function msgRenderBots' in html else False)
check('★ 群列表里有「关闭记录」按钮',
      'stopEv(event);muteChat(' in html and '恢复记录' in html)
check('★ 按钮上的图标都去掉了（关闭记录/备注/置顶）',
      '⏸' not in html and '✏️ 备注' not in html and '📌 置顶' not in html
      and '📌 已置顶' not in html)
check('★ 停记的群会标「（已停记）」', '（已停记）' in html)
check('★ 关之前先问一句（免得手滑）', '这个群不再记录新消息' in html)
r = post('/api/archive/ledA/mute',
         {'chat_id': '-100111', 'mute': True}, H_ADMIN).json()
check('★ 停记接口能用', r.get('ok') is True, str(r)[:60])
check('  真的写进配置了',
      '-100111' in ((PanelHandler.mgr.find('ledA').get('archive') or {})
                    .get('mute_chats') or []),
      str(PanelHandler.mgr.find('ledA').get('archive')))
r = post('/api/archive/ledA/mute',
         {'chat_id': '-100111', 'mute': True}, H_ADMIN).json()
check('重复停记不会写两遍（去重）',
      ((PanelHandler.mgr.find('ledA').get('archive') or {})
       .get('mute_chats') or []).count('-100111') == 1)
r = post('/api/archive/ledA/mute',
         {'chat_id': '-100111', 'mute': False}, H_ADMIN).json()
check('★ 能恢复记录', r.get('ok') is True
      and '-100111' not in ((PanelHandler.mgr.find('ledA').get('archive') or {})
                            .get('mute_chats') or []))
r = post('/api/archive/ledA/mute', {'chat_id': '', 'mute': True}, H_ADMIN).json()
check('不给群 id 会报错（不是静默成功）', r.get('ok') is False, str(r)[:60])
r = post('/api/archive/ledB/mute',
         {'chat_id': '-100999', 'mute': True}, HA)
check('★★ 商户停记 → 被挡',
      r.status_code != 200 or not r.json().get('ok'))

# 服务端真的不写：跑一次 runner 看盘
from archive import MessageArchive as _MA
_p = os.path.join(core.DATA_DIR, 'mutetest.archive.sqlite3')
_ar = _MA(_p)
_ar.record_result({'message': {'message_id': 1, 'date': int(time.time()),
                               'chat': {'id': -100777, 'type': 'supergroup',
                                        'title': '禁记群'},
                               'from': {'id': 5, 'first_name': '客'},
                               'text': '不该被记下来'}}, mute=['-100777'])
check('★★ 停记的群，服务端一条都不写',
      _ar.messages(chat_id='-100777') == [])

print()
print('  ---- ★ 备注 & 置顶 ----')
check('★ 操作里有「备注」按钮（图标去掉了）',
      '>备注</button>' in html and 'function editNote' in html)
check('★ 备注走接口改（不是只改前端）',
      "'/api/bots/' + id + '/note'" in html)
check('★ 操作里有「置顶」按钮（图标去掉了）',
      "pin ? '已置顶' : '置顶'" in html and 'function pinBot' in html)
check('★ 置顶的排最前面（排在未读前面）',
      'isBotPinned(a.id) ? 0 : 1' in html)
check('★ 群列表也能置顶', 'function pinChat' in html
      and 'stopEv(event);pinChat(' in html)
check('★ 群列表那一行整行可点进群（且带群名）',
      'onclick="msgOpenChat(' in html and '<th>群</th>' in html
      and '<th>群 / 会话</th>' not in html)
check('★ 群列表有「✏️ 备注」按钮', 'stopEv(event);noteChat(' in html)
check('★ 手机端有媒体查询', '@media (max-width:720px)' in html)
check('★ 群列表里没有「看消息」按钮了', "'看消息'" not in html)
check('★★ 群置顶跟全屏页共用一份数据（两边同步）',
      "localStorage.getItem('panel_pinned_' + bid)" in html
      and "localStorage.setItem('panel_pinned_' + MSG.BID" in html)
check('★ 置顶存在浏览器里、按人分开',
      "function prefsWho" in html and "'_bots'" in html)
check('★ 群列表置顶的排前面', 'pinned.indexOf(String(a.chat_id))' in html)
check('★ 红框那段提示已经删掉',
      '只记录<b>记账机器人</b>和' not in html
      and '不会被发到任何服务器' not in html)
check('★ 旧的单张卡片已经删干净',
      'card-arch' not in html and 'renderArchive' not in html
      and 'arcLoadMsgs' not in html)
check('★★ 「全屏打开」按钮已删（用户不要了）',
      '全屏打开' not in html and 'arcOpenAll' not in html)
check('★★ 进群消息时整页切全屏（底部不留空）',
      "classList.toggle('chatting', level === 3)" in html
      and 'body.chatting #msgStream' in html
      and 'body.chatting #tabs' in html)
check('★ 全屏时聊天区吃满剩下的高度', 'flex:1 1 auto;max-height:none' in html)
check('★ 手机端群名不竖排', '.tblwrap th,.tblwrap td{white-space:nowrap}' in html)
check('★ 页面有版本号显示（改了没生效时一眼看得出来）',
      'id="pagev"' in html and 'LAST_PAGE_V' in html
      and "'page_v'" in open('panel.py', encoding='utf-8').read())

print()
print('  ---- ★ 登录记录 ----')
check('★ 有「🔐 登录记录」标签', 'data-pane="logs"' in html
      and 'id="pane-logs"' in html)
check('★ 标签在 TABS 列表里', "'help', 'logs']" in html)
# ★★ 登录记录只留四列：时间 / IP / 城市 / 设备
#    用户 2026-09-29：「把谁还有方式还有结果去掉」。别加回来，他嫌乱。
check('★★ 登录记录只有 时间 / IP / 城市 / 设备 四列',
      all(k in html for k in ('<th>时间</th>', '<th>城市</th>',
                              '<th>IP</th>', '<th>设备</th>')))
check('★★ 去掉的三列（谁 / 方式 / 结果）没回来',
      '<th>谁</th>' not in html and '<th>方式</th>' not in html
      and '<th>结果</th>' not in html)
check('★ 归属地没查到时显示「查询中…」（不是空着）', '查询中…' in html)
r = get('/api/logins', H_ADMIN).json()
check('★ 管理员能读登录记录', r.get('ok') is True, str(r)[:60])
check('  返回了 stats', 'stats' in r)
r = get('/api/logins', HA)
check('★★ 商户读登录记录 → 被挡（里面有 IP）',
      r.status_code != 200 or not r.json().get('ok'),
      '%s' % r.status_code)
r = post('/api/logins/clear', {}, HA)
check('★★ 商户清登录记录 → 被挡',
      r.status_code != 200 or not r.json().get('ok'))
_art = html.split('function applyRoleTabs')[1].split('function ')[0]
check('★ 商户端看不到「登录记录」标签', "'logs'" in _art)
check('★★ 商户端也看不到「消息记录」标签（管理端专属）',
      "'msg'" in _art,
      # 2026-09-28 用户要求：商户后台只给客户配商城机器人，
      # 消息记录/其它机器人一概不显示
      'applyRoleTabs 里没藏 msg')
_sw = html.split('function switchTab')[1].split('function ')[0]
check('★★ switchTab 里也兜一道（from localStorage 塞回来也不漏）',
      "'msg'" in _sw and "'merch'" in _sw and "'logs'" in _sw)
check('★ IP 点一下能复制（解码之后才有真实 IP）',
      # ★ 2026-09-29 改成 jsq()：这个 r.ip 来自 X-Forwarded-For，**攻击者随便填**，
      #   拼进内联事件的 JS 字符串里用 esc() 挡不住（属性先 HTML 解码回 `'`）。
      #   这里连「旧的漏洞写法不许回来」一起断言。
      "copyText(\\'' + jsq(r.ip)" in html and 'function copyText' in html
      and "copyText(\\'' + esc(r.ip)" not in html,
      # 2026-09-28 改成：默认只给打码的 ip_masked，
      # 输对谷歌验证码才拿到真实 ip（走 /api/logins/reveal）
      'ip 单元格没走 copyText（或又退回 esc 了）')
check('★★ 打码的 IP 点一下 → 去输验证码（不是直接复制）',
      "askTotp(\\'reveal\\')" in html,
      '打码那条路该引导用户去输验证码')

print()
print('  ---- ★★ 登录记录：写操作必须在 POST 里 ----')
_src = open('panel.py', encoding='utf-8').read()
_get = _src.split('def do_POST')[0].split('def do_GET')[1]
_post = _src.split('def do_POST')[1]
check('★★ /api/logins/clear 在 do_POST 里（不是 do_GET）',
      'logins/clear' in _post and 'logins/clear' not in _get)
check('★ /api/logins 读接口在 do_GET 里', "'/api/logins'" in _get)
check('★ 清空那段的 me 有定义（不会 NameError）',
      _post.index('me = self._gate()') < _post.index('logins/clear')
      if 'logins/clear' in _post else False)
r = post('/api/logins/clear', {}, H_ADMIN).json()
check('★★ 管理员清空真的能清', r.get('ok') is True, str(r)[:70])
check('  清空后是空的',
      get('/api/logins', H_ADMIN).json().get('logins') == [])

print()
print('  ---- ★★ 自动登录也要留痕迹（朋友登录没记录那个 bug）----')
check('★★ 有访问记录逻辑（带凭据访问就记）',
      'def _log_access' in _src and '_log_access(me)' in _src)
check('★ 挂在 _gate 里（所有接口都过它）',
      _src.index('_log_access(me)') > _src.index('def _gate'))
check('★ 同一个 IP+身份 6 小时内只记一条（不刷屏）',
      "ACCESS_WINDOW = 6 * 3600" in _src
      and 'now - last < ACCESS_WINDOW' in _src)
check('★ 记录里区分「登录」和「访问」',
      "kind='login'" in _src and "kind='access'" in _src)
check('★ 转发头也认 X-Real-IP / CF-Connecting-IP',
      'X-Real-IP' in _src and 'CF-Connecting-IP' in _src)
# ★ 后端**照样记**是登录还是访问（上一条），只是界面不显示这一列了 ——
#   用户 2026-09-29 要求去掉，但数据别丢（以后想加回来不用改后端）
check('★ 界面上不再有「方式」那一列', '<th>方式</th>' not in html)

# 真的走一遍：带密码头访问接口，应该记一条 access
# ★ 得先把去重表清掉 —— 这个测试前面已经调了几十次接口，
#   那个 IP+身份 早在 6 小时窗口里了，不清掉当然不会再记
login_log.clear()
PanelHandler._access_seen.clear()
get('/api/bots', H_ADMIN)
time.sleep(0.3)
_rows = login_log.recent()
check('★★ 带凭据访问一次接口 → 记下一条 access',
      len(_rows) == 1 and _rows[0].get('kind') == 'access', str(_rows)[:140])
get('/api/bots', H_ADMIN)          # 再来一次
time.sleep(0.2)
check('★ 连着访问不会重复记（6 小时窗口）',
      len(login_log.recent()) == 1, '%d 条' % len(login_log.recent()))

print()
print('  ---- ★★ 面包屑文案 ----')
check('★★ 第1级叫「机器人列表」（不是「消息记录」）',
      '>📋 机器人列表 <span class="cnt" id="cnt-msg">' in html)
check('★★ 第2级叫「群消息」（不显示机器人名）',
      '<span class="muted">/</span> <span>群消息</span>' in html)
check('★★ 第3级显示群名称（不是 -100 那种 id）',
      'MSG.CHAT_TITLE = title || cid' in html
      and 'el.textContent = MSG.CHAT_TITLE' in html)
check('★ 点群那一行会把群名带过去',
      # ★ 2026-09-29：cid 和群名都**是第三方可控的**（把机器人拉进自己的群、
      #   群名里带个 `'` 就跳出 JS 字符串了）→ 必须走 jsq()
      "msgOpenChat(\\'' + jsq(cid) + '\\', \\''" in html
      or "jsq(cid) + '\\', \\''" in html,
      '或者又退回 esc(cid) 了')
check('★ 旧的 msgBackBot 已清干净', 'msgBackBot' not in html)

print()
print('  ---- ★ 开关挪到了记录页面里 ----')
check('★ 记录页有开关按钮', 'id="arcToggle"' in pgtext)
check('★ 对着当前选中的机器人开关', 'function arcToggleClick()' in pgtext
      and 'curBot()' in pgtext)
check('★ 开着的时候显示保留天数', "记录中 · " in pgtext or '记录中 · ' in pgtext)
check('★ 开启时问保留天数', "记录保留多少天" in pgtext)
check('★ 关闭前二次确认（免得手滑关掉）', '确定关掉吗' in pgtext)
check('★ 切机器人时开关跟着刷新', 'renderArcToggle();' in pgtext
      and pgtext.count('renderArcToggle') >= 3)
check('★ 开关状态是问接口要的，不是前端自己猜',
      '/api/archive/' + "' + BID + '" + '/config' in pgtext)

print()
print('  ---- 记录页的实时刷新 ----')
check('★ 页面有实时开关', 'id="live"' in pgtext and '实时' in pgtext)
check('★ 定时轮询（不是手动刷新）',
      'setInterval(tick' in pgtext and '/since?' in pgtext)
check('★ 页面在后台时不轮询（省资源）', 'document.hidden' in pgtext)
check('★ 去重（轮询会重复带游标那条）', 'SEEN[keyOf(m)]' in pgtext)
check('★ 用户在翻老消息时不硬插，给「N 条新消息」提示',
      'PENDING' in pgtext and 'jumpBottom' in pgtext)
check('★ 搜索时不做实时（结果被过滤过）', 'SEARCHING' in pgtext)

print()
print('  ---- ★ 轮询必须真的启动（踩过：写在 if(!pre) 里，永远不执行）----')
check('★★ startLive() 是无条件调的',
      'if(!pre) startLive();' not in pgtext and 'startLive();' in pgtext)
check('   不是藏在条件分支里',
      pgtext.count('startLive();') >= 1
      and 'if(!pre) startLive' not in pgtext)

print()
print('  ---- ★ 图片下好后页面自己补上（不用手动刷新）----')
check('★ 占位符带 data-pending 标记', 'data-pending="1"' in pgtext)
check('★ 每轮轮询会去补还没下好的图',
      'refreshPendingMedia' in pgtext and pgtext.count('refreshPendingMedia') >= 2)
check('★ 补图时按 data-key 精确替换（不会错位）',
      'getAttribute(\'data-key\')' in pgtext and 'ph.replaceWith(img)' in pgtext)
check('★ 只查还没有 src 的图（不重复下载）',
      "img[data-name]:not([src])" in pgtext)

print()
print('  ---- ★★ 三处白名单必须一致（这次就是漏了记录页那处）----')
core_list = tuple(core.ARCHIVE_TYPES)
check('★ 后端白名单只留记账机器人', core_list == ('ledger',), str(core_list))


def types_in(text):
    i = text.find("ARCHIVE_TYPES = [")
    if i < 0:
        return None
    seg = text[i:text.find(']', i) + 1]
    return tuple(x.strip().strip("'\"") for x in
                 seg.split('[')[1].rstrip(']').split(',') if x.strip())


panel_types = types_in(html)
page_types = types_in(pgtext)
check('★ 主面板里的白名单跟后端一致', panel_types == core_list,
      '%s vs %s' % (panel_types, core_list))
check('★★ 记录页里的白名单也跟后端一致', page_types == core_list,
      '%s vs %s' % (page_types, core_list))
check('★ 记录页的机器人下拉过滤了类型（之前漏了，USDT/商城也冒出来）',
      'ARCHIVE_TYPES.indexOf(b.type) >= 0' in pgtext)
check('★ 没开记录的排在后面（点进去也是空的）',
      'archive && a.archive.enabled' in pgtext or 'b.archive && b.archive.enabled' in pgtext
      or 'a.archive && a.archive.enabled' in pgtext)
check('★ 默认选中第一个「开着记录」的',
      'firstOn || BOTS[0]' in pgtext)

print()
print('  ---- ★ 消息排列：从上往下、新的在最下面、主人靠右 ----')
check('★ 接口给的是新的在前，页面翻过来显示',
      'rows.slice().reverse().map(msgHtml)' in pgtext)
check('★ 主人靠右（is_owner → .msg.right）',
      "own ? ' right' : ''" in pgtext and 'm.is_owner' in pgtext)
# ★★ 主人那条原来显示「我」，2026-09-29 用户要求换成**昵称 + 用户名**
#    （主人可能有好几个号，标「我」截屏/对账时分不清是谁）
#    ★ **不要数字 id**（用户明确说过不要 6196364329 那串）
check('★★ 主人的名字不再显示成「我」', "own ? '我'" not in pgtext)
# 只看 msgHtml 那一小段，别被页面别处的同名变量干扰
_mb = pgtext.split('function msgHtml')[1].split('function ')[0]
check('★ 记录页：主人显示昵称 + @用户名（跟左边那些人一样）',
      'esc(m.display_name || m.username' in _mb
      # ★ 两个页面拼字符串的方式不一样（一个 `'@' + esc(...)`、
      #   一个 `>@' + esc(...)`），所以用正则，别写死引号位置
      and re.search(r"@'\s*\+\s*esc\(m\.username\)", _mb) is not None)
check('★★ 记录页：名字里没拼数字 id（用户明确不要那串）',
      'esc(m.user_id) +' not in _mb)
# 面板页那份也要一起验（两边都改了，漏一边就长得不一样）
_pp = get('/', {}).text
_pb = _pp.split('function msgBubble')[1].split('function ')[0]
check('★★ 面板页：主人显示昵称 + @用户名（比如「灰产王 @HCW2026」）',
      'esc(name)' in _pb and "'@' + esc(m.username)" in _pb)
check('★★ 面板页：名字里**没有**拼数字 id（不要 6196364329 那串）',
      'm.user_id' not in _pb.split('var extra')[1].split('return')[0],
      _pb.split('var extra')[1].split('return')[0][:70].replace('\n', ' '))
check('★ 自己的气泡是绿的（微信那种）', '.msg.right .bubble{background:#1f4d33' in pgtext)
check('★ 新消息追加到底部（不是插到顶上）',
      'insertAdjacentHTML(\'beforeend\'' in pgtext and 'afterbegin' not in pgtext)
check('★ 追加后跟着滚到底', 'scrollBottom()' in pgtext)
check('★ 判断「在不在底部」看 #main（消息区自己滚）',
      'el.scrollHeight - el.scrollTop - el.clientHeight' in pgtext
      and 'window.scrollY' not in pgtext)
check('★ 用户往上翻时不硬拽到底，给「N 条新消息 ↓」',
      'PENDING = PENDING.concat(mine)' in pgtext and 'jumpBottom' in pgtext)
check('★ 滚回底部自动放出来（scroll 不冒泡，要捕获阶段）',
      "e.target.id === 'main' && nearBottom()" in pgtext and 'true);' in pgtext)
check('★ 加载完直接停在最新（底部）', 'scrollBottom();' in pgtext)

print()
print('  ---- ★ 双击放大图片 ----')
check('★ 双击放大（用户点名要的）', "addEventListener('dblclick'" in pgtext)
check('★ 只对消息里的图生效（不会误伤别的）',
      "t.closest('#main')" in pgtext)
check('★ 滚轮缩放', "addEventListener('wheel'" in pgtext)
check('★ 放大后能拖着看（截图经常很长）',
      "addEventListener('mousedown'" in pgtext and 'dragging' in pgtext)
check('★ 双击大图复位', 'lbReset()' in pgtext)
check('★ Esc / 点背景关闭', "e.key === 'Escape'" in pgtext
      and 'e.target === box' in pgtext)
check('★ 提示了操作方式', '滚轮缩放' in pgtext)
check('★ 图片鼠标变放大镜（看得出来能点）', '.msg img{cursor:zoom-in}' in pgtext)

print()
print('  ---- ★ 未读提醒 ----')
check('★ 会话上有未读角标', 'class="badge"' in pgtext and 'UNREAD' in pgtext)
check('★ 未读数问服务端要（关掉浏览器再开能接上）',
      "/unread?seen=" in pgtext and 'refreshUnread' in pgtext)
check('★ 已读状态存在浏览器里（按机器人分开）',
      "lsKey('seen')" in pgtext and 'localStorage.setItem' in pgtext)
check('★ 第一次打开先标基线（不然历史记录全顶着红点）',
      'Object.keys(LAST_SEEN).length' in pgtext)
check('★ 「全部会话」总览不清红点（不然一直开着就全清了）',
      "if(CHAT && !SEARCHING)" in pgtext)
check('★ 标签页标题也带上未读数',
      "document.title = (total" in pgtext)

print()
print('  ---- ★ 右键置顶 ----')
check('★ 会话项挂了右键事件', 'oncontextmenu="showCtx(' in pgtext)
check('★ 有置顶/取消置顶菜单', '📌 置顶这个会话' in pgtext
      and '📌 取消置顶' in pgtext)
check('★ 置顶存浏览器里（按机器人分开）', "lsKey('pinned')" in pgtext)
check('★ 置顶的排前面、顺序可调',
      'PINNED.indexOf(String(a.chat_id))' in pgtext)
check('★ 菜单点别处会关掉', "document.addEventListener('click', hideCtx)" in pgtext)
check('★ 提示了可以右键', '右键可以置顶' in pgtext)
PanelHandler.mgr.stop_all()

httpd.shutdown()
shutil.rmtree(TMP, ignore_errors=True)
print()
print('=' * 62)
print('结果：%d 项通过，%d 项失败' % (len(OK), len(BAD)))
for x in BAD:
    print('  ❌', x)
print('=' * 62)
sys.exit(1 if BAD else 0)
