# -*- coding: utf-8 -*-
"""客户版转发 → 服务商面板：端到端

把「客户那台机器」和「服务商的面板」在同一台机器上跑起来，验证整条链路：

    客户那边                     服务商这边
    relay.push(update)  ──HTTP──>  /api/ingest
                                   → archive.record_result()
                                   → 面板的 /api/archive/<bid>/messages 读得到

★ 三条最要命的，专门测：
  1. **转发卡住绝不能拖慢记账** —— `on_message()` 必须**立刻返回**
     （它是在记账的收消息循环里被调的，慢一点就是记账变慢）
  2. **队列满了要丢最老的** —— 不能把客户的内存吃光
  3. **对面拒收（403）不能无限重试** —— 不然日志会被刷爆

图片：真的去 Telegram 下不了（没有真 token），所以把下载那步换掉，
      但**上传那一段走真的**。
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
TMP = os.path.join(HERE, '_tmp_solo')
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(os.path.join(TMP, 'data'), exist_ok=True)

import core                                          # noqa: E402
core.set_base_dir(TMP)
core.BOTS_FILE = os.path.join(TMP, 'bots.json')
core.DATA_DIR = os.path.join(TMP, 'data')
core.CONFIG_FILE = os.path.join(TMP, 'config.json')
core.CODES_DIR = os.path.join(TMP, 'codes')
core.LOG_FILE = os.path.join(TMP, '运行日志.txt')

from manager import BotManager                       # noqa: E402
from merchants import MerchantStore                  # noqa: E402
from panel import PanelHandler                       # noqa: E402
from archive import MessageArchive                   # noqa: E402

# 客户版的转发模块（就在仓库的 solo/ 下，打包时原样搬走）
sys.path.insert(0, os.path.join(HERE, 'solo'))
import relay                                         # noqa: E402

OK, BAD = [], []
# 回传暗号是两边从 token 现算的（core.ingest_sig），没有单独的密钥
BID, TOK = 'custA', '1:a'
SIG = core.ingest_sig(BID, TOK)


def check(name, cond, extra=''):
    (OK if cond else BAD).append(name)
    print('  %s %s%s' % ('✅' if cond else '❌', name,
                         ('  → %s' % (extra,)) if extra else ''))


mgr = BotManager({'pass': 'PW'})
mgr.bots.append({
    'id': BID, 'type': 'ledger', 'note': '客户A', 'token': TOK,
    'bind_code': 'PANELBC1',
    'enabled': True, 'expire_at': 0, 'expired_at': 0,
    'remote': True, 'archive': {'enabled': True},
    'admin_ids': [], 'mid': '',
})
PanelHandler.mgr = mgr
PanelHandler.password = 'PW'
PanelHandler.merch = MerchantStore()
PanelHandler.token_secret = 'S' * 32
PanelHandler.page_cache = None

httpd = ThreadingHTTPServer(('127.0.0.1', 0), PanelHandler)
BASE = 'http://127.0.0.1:%d' % httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()
time.sleep(0.3)


def mkup(text, mid, chat=-1001234, extra=None):
    m = {'message_id': mid, 'date': int(time.time()),
         'chat': {'id': chat, 'type': 'supergroup', 'title': '客户的群'},
         'from': {'id': 9, 'first_name': '张', 'last_name': '三',
                  'username': 'zs', 'is_bot': False},
         'text': text}
    if extra:
        m.update(extra)
    return {'update_id': mid, 'message': m}


def wait_for(cond, timeout=8):
    t = time.time()
    while time.time() - t < timeout:
        if cond():
            return True
        time.sleep(0.1)
    return False


print('=' * 62)
print('① ★ 转发钩子：必须**立刻返回**，绝不能拖慢记账')
print('=' * 62)
rep_off = relay.setup({'enabled': False}, BID, TOK)
t0 = time.time()
for i in range(200):
    relay.on_message(None, mkup('x', i))
dt = (time.time() - t0) * 1000
check('★ 没开转发时：200 次调用 < 50ms（就是个空跑）', dt < 50,
      '%.1f ms' % dt)
check('★ 没开时不返回 True（返回 True 会把消息从记账那边吃掉！）',
      relay.on_message(None, mkup('x', 1)) is None)

rep = relay.setup({'url': BASE, 'enabled': True,
                   'interval': 0.2, 'batch': 50}, BID, TOK)
t0 = time.time()
for i in range(200):
    relay.on_message(None, mkup('y', i))
dt = (time.time() - t0) * 1000
check('★★ 开了转发也一样快（只是塞内存队列，不发网络）', dt < 200,
      '%.1f ms' % dt)

print()
print('=' * 62)
print('② ★★ 端到端：消息真的到了面板的归档库里')
print('=' * 62)


def arc():
    p = os.path.join(TMP, 'data', 'custA.archive.sqlite3')
    return MessageArchive(p) if os.path.exists(p) else None


ok = wait_for(lambda: arc() is not None and len(arc().messages()) >= 100)
a = arc()
check('★★ 200 条里有 100+ 条进了面板的库', ok and len(a.messages()) >= 100,
      '%d 条' % (len(a.messages()) if a else 0))
rows = a.messages() if a else []
check('★ 内容对得上', rows and rows[0]['text'].startswith('y'),
      rows[0]['text'] if rows else '')
check('★ 群名对得上', rows and rows[0]['chat_title'] == '客户的群')
check('★ 发言人对得上', rows and rows[0]['display_name'] == '张 三')
check('★ 转发统计里有成功数', (relay.stats() or {}).get('sent', 0) >= 100,
      str(relay.stats()))

print()
print('=' * 62)
print('③ ★ 面板自己的接口能读到（不用改任何东西）')
print('=' * 62)
r = requests.get(BASE + '/api/archive/custA/messages',
                 headers={'X-Panel-Pass': 'PW'}, timeout=10).json()
check('★★ 面板的「消息记录」接口读得到回传的消息',
      r.get('ok') and len(r.get('messages') or []) >= 100,
      '%d 条' % len(r.get('messages') or []))
r = requests.get(BASE + '/api/archive/custA/chats',
                 headers={'X-Panel-Pass': 'PW'}, timeout=10).json()
check('★ 群列表也出来了（三级下钻的第 2 级）',
      r.get('ok') and r.get('chats'), str(r.get('chats'))[:80])

print()
print('=' * 62)
print('④ ★★ 对面拒收（403）不能无限重试、不能刷屏')
print('=' * 62)
# ★ 拿一个**不对的 token** 去算暗号 —— 服务端认不出来
rep_bad = relay.Reporter({'url': BASE, 'enabled': True,
                          'interval': 0.2, 'batch': 10}, BID, '错的token')
before = rep_bad.failed
rep_bad._send(requests.Session(), [mkup('bad', 1)] * 10)
check('★★ 密钥错 → 不重试 3 轮，直接丢（failed 不涨）',
      rep_bad.failed == before, 'failed=%d' % rep_bad.failed)
check('★ 丢掉的条数记上了', rep_bad.dropped >= 10, str(rep_bad.dropped))

print()
print('=' * 62)
print('⑤ ★ 队列满了丢**最老的**（不撑爆客户内存）')
print('=' * 62)
rep_q = relay.Reporter({'url': BASE, 'enabled': True,
                        'max_queue': 10}, BID, TOK)
for i in range(50):
    rep_q.push(mkup('q', i))
check('★ 队列长度被摁在 10（不是 50）', len(rep_q.q) == 10, str(len(rep_q.q)))
check('★ 丢的是最老的（留下的是最后 10 条）',
      rep_q.q[0]['message']['text'] == 'q' and
      rep_q.q[0]['message']['message_id'] == 40,
      str(rep_q.q[0]['message']['message_id']))
check('★ 丢了多少记了数', rep_q.dropped == 40, str(rep_q.dropped))

print()
print('=' * 62)
print('⑥ ★ 图片：下载换掉，上传走真的')
print('=' * 62)
FID = 'AgACAgUAAx0EU_img_test_0001'
PNG = b'\x89PNG\r\n\x1a\n' + b'\x11' * 80


class FakeSess(requests.Session):
    pass


rep_img = relay.Reporter({'url': BASE, 'enabled': True}, BID, TOK)
rep_img._download = lambda sess, fid: (PNG, '.png')     # 只换掉「去 Telegram 下」
up = mkup('截图', 88, extra={'photo': [{'file_id': FID}]})
# ★ 要先让**消息本身**过去（面板那边有了这条记录，图才挂得上去），
#   再走图片那一步 —— 顺序反了的话服务端会回「记录不在」
rep_img._send(requests.Session(), [up])
time.sleep(0.3)
a = arc()
row = [x for x in a.messages() if x['message_id'] == '88']
check('★ 带图那条进库了', len(row) == 1, '%d 条' % len(row))
check('★★ payload.media 补上了（面板靠它出图）',
      row and bool(row[0]['media']), repr(row[0]['media'] if row else None))
if row and row[0]['media']:
    p = a.media_path(row[0]['media'])
    check('★★ 图片文件真的在，内容对得上',
          p is not None and p.read_bytes() == PNG)
    r = requests.get(BASE + '/api/archive/custA/media/' + row[0]['media'],
                     headers={'X-Panel-Pass': 'PW'}, timeout=10)
    check('★★ 面板的图片路由也能取到（前端 <img> 就靠它）',
          r.status_code == 200 and r.content == PNG,
          '%s %d 字节' % (r.status_code, len(r.content)))

print()
print('=' * 62)
print('⑦ ★ 服务商那边整个挂了，客户这边记账照常')
print('=' * 62)
rep_dead = relay.Reporter({'url': 'http://127.0.0.1:1',
                           'enabled': True, 'interval': 0.2, 'batch': 5,
                           'timeout': 1, 'max_queue': 20}, BID, TOK)
t0 = time.time()
for i in range(100):
    rep_dead.push(mkup('dead', i))
dt = (time.time() - t0) * 1000
check('★★ 对面连不上，push 还是瞬时的（记账不受影响）', dt < 100,
      '%.1f ms' % dt)
# ★ 这里验的是**内存有上限**：对面挂着的时候消息一直堆，
#   但队列封顶 20，多出来的丢掉。不是「一秒内就清空」——
#   重试本身要好几秒，那几秒里新消息本来就该堆着（堆不下才丢）。
check('★★ 队列被摁在 20（不会把客户机器的内存吃光）',
      len(rep_dead.q) <= 20, '队列里 %d 条' % len(rep_dead.q))
check('★ 装不下的那些丢掉了，而且记了数', rep_dead.dropped >= 80,
      'dropped=%d' % rep_dead.dropped)
rep_dead.start()
time.sleep(1.0)
check('★ 一直在失败也不崩（线程还活着）',
      rep_dead.thread is not None and rep_dead.thread.is_alive())
rep_dead.stop()

print()
print('=' * 62)
print('⑨ ★★★ 机器人**自己发的**消息（账单）也要传过来')
print('=' * 62)
# 2026-09-29 客户实测：面板里那个群有 26 条消息，**机器人自己发的 0 条** ——
# 账单一条都没传过来。原因：Telegram 的 getUpdates **不返回机器人自己发的
# 消息**，只能挂在 api.on_sent 上截（面板版就是靠它归档自己发的账单）。
# 之前只监听了 on_message，于是「别人说的话」传过来了、账单全丢。


class FakeHookAPI:
    """假装是 core.TgAPI —— on_sent 上**已经挂了** core 自己的归档钩子"""

    def __init__(self):
        self.on_sent = self.core_hook
        self.core_called = []

    def core_hook(self, method, params, result):
        self.core_called.append(result.get('message_id'))


class FakeRunner:
    def __init__(self):
        self.api = FakeHookAPI()
        self.bot = {'admin_ids': [], 'owner_id': 0}


fr = FakeRunner()
rep_own = relay.Reporter({'url': BASE, 'enabled': True, 'interval': 0.2,
                          'batch': 20}, BID, TOK)
rep_own.attach(fr)
check('★★ 钩上 api.on_sent 了（不钩的话账单永远传不过来）',
      callable(getattr(fr.api, 'on_sent', None)))

BILL = ('<b>今日账单</b>\n总入款金额: <a href="https://t.me/">1100</a>'
        '\n应下发: 100 | 100U')
SENT = {'message_id': 900, 'date': int(time.time()),
        'chat': {'id': -1001234, 'type': 'supergroup', 'title': '客户的群'},
        'from': {'id': 555, 'is_bot': True, 'first_name': '沃克'},
        'text': BILL}

fr.api.on_sent('sendMessage', {'parse_mode': 'HTML'}, SENT)
check('★ 转发**没有**把 core 自己的钩子顶掉（是包一层，不是覆盖）',
      fr.api.core_called == [900], str(fr.api.core_called))
check('★ 账单进了转发队列', len(rep_own.q) == 1, '队列 %d 条' % len(rep_own.q))
_q = rep_own.q[0]['message'] if rep_own.q else {}
check('★★ HTML 转成纯文本了（不然面板上是 <a href=...> 这种乱码）',
      '<' not in (_q.get('text') or '') and '1100' in (_q.get('text') or ''),
      repr((_q.get('text') or '')[:44]))
check('★ 标成「机器人自己发的」（面板里样式不同，而且不算未读）',
      (_q.get('from') or {}).get('is_bot') is True)

rep_own._send(requests.Session(), list(rep_own.q))
rep_own.q.clear()
time.sleep(0.3)
a = arc()
row = [x for x in a.messages(limit=1000) if x['message_id'] == '900']
check('★★★ 账单真的落到面板的归档库里了', len(row) == 1, '%d 条' % len(row))
check('★ 库里也标着 is_bot', bool(row) and row[0]['is_bot'] is True,
      str(row[0]['is_bot'] if row else None))
check('★ 内容对得上（是「今日账单」那条）',
      bool(row) and '今日账单' in row[0]['text'],
      (row[0]['text'] if row else '')[:30])
_nonbot = [x for x in a.messages(limit=1000) if not x['is_bot']]
check('★★ 自己发的**不算未读**（不然面板一直冒红点）',
      sum(a.unread_counts({}).values()) == len(_nonbot),
      '未读合计 %d，非机器人消息 %d 条'
      % (sum(a.unread_counts({}).values()), len(_nonbot)))

_before_hook = fr.api.on_sent
rep_own.attach(fr)
check('★ 重复 attach 不会套两层（不然同一条账单传两遍）',
      fr.api.on_sent is _before_hook)

print()
print('=' * 62)
print('⑩ ★★ 私聊不转发（用户要求「只记群聊」）')
print('=' * 62)
# ★ 在**源头**就拦住，比传到对面再丢掉干净：客户私聊机器人的内容
#   （绑定、广播、查地址）根本没必要离开客户那台服务器
rep_pv = relay.Reporter({'url': BASE, 'enabled': True}, BID, TOK)
rep_pv.push({'message': {'message_id': 1, 'chat': {'id': 999, 'type': 'private'},
                         'text': '私聊说的话'}})
check('★★ 私聊消息不进转发队列', len(rep_pv.q) == 0, '%d 条' % len(rep_pv.q))
rep_pv.push({'message': {'message_id': 2, 'chat': {'id': -100,
                                                   'type': 'supergroup'},
                         'text': '群里说的话'}})
check('★ 群聊照常转发', len(rep_pv.q) == 1, '%d 条' % len(rep_pv.q))
rep_pv.push_sent({'message_id': 3, 'date': int(time.time()),
                  'chat': {'id': 999, 'type': 'private'},
                  'from': {'id': 5, 'is_bot': True}, 'text': '私聊里的回执'}, {})
check('★ 机器人自己发的私聊消息也不转', len(rep_pv.q) == 1,
      '%d 条' % len(rep_pv.q))
check('★ 认得出私聊（is_private 这个判断要留着）',
      relay.is_private({'message': {'chat': {'type': 'private'}}}) is True
      and relay.is_private({'message': {'chat': {'type': 'group'}}}) is False
      and relay.is_private({}) is False)

rep.stop()

print()
print('=' * 62)
print('⑧ ★★ 一键下载「客户安装包」（全自动那一步）')
print('=' * 62)
import io as _io                                          # noqa: E402
import urllib.parse                                       # noqa: E402
import zipfile                                            # noqa: E402

url = BASE + '/api/bots/custA/solozip?url=' + urllib.parse.quote(BASE)
r = requests.get(url, headers={'X-Panel-Pass': 'PW'}, timeout=60)
check('★★ 下载得到（是个 zip）',
      r.status_code == 200 and r.content[:2] == b'PK',
      '%s %d 字节' % (r.status_code, len(r.content)))
names = []
cfg = {}
if r.content[:2] == b'PK':
    z = zipfile.ZipFile(_io.BytesIO(r.content))
    names = z.namelist()
    cfg = json.loads(z.read('记账独立版/config.json').decode('utf-8'))
check('★ 里面该有的都在（入口 / 转发 / 核心 / 记账包 / 安装脚本）',
      all(any(n.endswith(x) for n in names) for x in
          ('独立版.py', 'relay.py', 'core.py', 'runners/ledger/runner.py',
           'install.sh', 'requirements.txt', 'config.json')),
      '共 %d 个文件' % len(names))
check('★★★ config.json 是**填好的** —— 用户不用粘、客户不用填',
      cfg.get('id') == 'custA' and cfg.get('token') == TOK
      and (cfg.get('report') or {}).get('url') == BASE,
      json.dumps(cfg, ensure_ascii=False)[:110])
check('★★ 而且里面的暗号算出来跟服务端认的一致（真能发回来）',
      core.ingest_sig(cfg.get('id'), cfg.get('token')) == SIG)
# ★★ 绑定码必须跟**面板上显示的**是同一个。不带上它的话客户那边会自己
#    再生一个，两个码「看起来都对」但客户照面板上的发 /admin 会被拒 ——
#    2026-09-29 第一次真给客户装的时候踩到，极难查。
check('★★★ config.json 里带上了**面板上那个**绑定码（不然客户绑不上）',
      cfg.get('bind_code') == 'PANELBC1', repr(cfg.get('bind_code')))
check('★★ 没有面板 / 商城 / USDT / 客服 / 商户 的东西',
      not any(x in n for n in names for x in
              ('panel', 'shop', 'usdt', 'kefu', 'merchants', 'login_log',
               'totp', '/api.py', 'identity.py')),
      str([n for n in names if 'panel' in n or 'shop' in n]))
check('★ 回传地址不合法要拒（别让人往配置里塞别的东西）',
      requests.get(BASE + '/api/bots/custA/solozip?url=file:///etc/passwd',
                   headers={'X-Panel-Pass': 'PW'},
                   timeout=10).status_code == 400)
r = requests.get(BASE + '/api/bots/custA/solozip',
                 headers={'X-Panel-Pass': 'PW'}, timeout=10)
check('★ 不给地址也拒', r.status_code == 400, str(r.status_code))
r = requests.get(BASE + '/api/bots/custA/solozip?url=' + urllib.parse.quote(BASE),
                 timeout=30)
check('★ 没登录拿不到（里面有 token）', r.status_code in (401, 403),
      str(r.status_code))

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
