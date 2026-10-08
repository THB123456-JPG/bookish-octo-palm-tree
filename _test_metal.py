# -*- coding: utf-8 -*-
"""贵金属价格（群里发 `G`）—— 解析 / 卡片 / 触发词 / 不阻塞主线程

★ 全部**离线**：把 httpx.Client 换成假的，样本用的是**真实抓到的报文**
  （2026-10-08 从上金所那个接口抄下来的原文）。
"""
import os
import shutil
import sys
import threading
import time

sys.stdout.reconfigure(encoding='utf-8')

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = os.path.join(HERE, '_tmp_metal')
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(os.path.join(TMP, 'data'), exist_ok=True)

import core
core.BASE_DIR = TMP
core.DATA_DIR = os.path.join(TMP, 'data')

from runners.ledger import metal as M
from runners.ledger import LedgerRunner

OK, BAD = [], []


def check(name, cond, extra=''):
    (OK if cond else BAD).append(name)
    print('  %s %s%s' % ('✅' if cond else '❌', name,
                         ('  → %s' % (extra,)) if extra else ''))


# 真实报文（原样抄的，含中文名 —— 用来盯住 GBK 那件事）
SINA_OK = (
    'var hq_str_gds_AU9999="891.81,0,891.81,892.00,896.30,889.00,15:30:01,'
    '907.32,890.50,1441100,104.00,1000.00,2026-10-08,沪金99";\n'
    'var hq_str_gds_AUTD="891.50,0,891.01,891.60,895.17,888.41,15:30:06,'
    '906.63,890.05,31904,3.00,10.00,2026-10-08,黄金延期";\n'
    'var hq_str_gds_AU9995="890.60,0,882.05,894.99,892.80,890.00,15:29:58,'
    '904.50,892.80,6,2.00,1.00,2026-10-08,沪金95";\n'
    'var hq_str_gds_AGTD="14365.00,0,14336.00,14365.00,14719.00,14271.00,'
    '15:30:03,14896.00,14614.00,392430,61.00,62.00,2026-10-08,白银延期";\n'
    'var hq_str_gds_PT9995="410.00,0,0.00,410.00,411.47,409.80,15:15:19,'
    '422.23,409.80,106,0.00,10.00,2026-10-08,沪铂95";\n')


class FakeResp:
    def __init__(self, text='', payload=None, code=200):
        self.text = text
        self._payload = payload
        self.status_code = code
        self.encoding = 'utf-8'

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError('HTTP %d' % self.status_code)

    def json(self):
        return self._payload


class BlockingResp(FakeResp):
    """一直卡到 gate 放开为止 —— 用来证明抓价真的在后台线程里跑"""
    def __init__(self, gate, text=''):
        FakeResp.__init__(self, text=text)
        self.gate = gate


class FakeClient:
    """替 httpx.Client。routes: {url子串: FakeResp / BlockingResp / 异常}"""
    routes = {}
    hits = []

    def __init__(self, timeout=None, headers=None):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, **kw):
        FakeClient.hits.append(url)
        for key, val in FakeClient.routes.items():
            if key in url:
                if isinstance(val, Exception):
                    raise val
                if isinstance(val, BlockingResp):
                    val.gate.wait(5)
                return val
        return FakeResp(text=SINA_OK)

    @classmethod
    def reset(cls, routes=None):
        cls.routes = routes or {'sinajs': FakeResp(text=SINA_OK)}
        cls.hits = []


M.httpx = type('H', (), {'Client': FakeClient})
RECYCLE_OK = FakeResp(payload={'data': {'domestic': [
    {'name': '国内金价', 'price': '891.50'},
    {'name': '黄金回收价格', 'price': '876.50'},
    {'name': '国内银价', 'price': '14.37'}]}})


def fresh():
    M._cache['at'] = 0.0
    M._cache['value'] = None


print('=' * 62)
print('一、解析上金所报文')
print('=' * 62)
q = M.parse_sina(SINA_OK)
check('五个品种全解析出来', len(q) == 5, str(sorted(q)))
check('黄金9999 = 891.81', str(q['AU9999']['price']) == '891.81')
check('黄金T+D = 891.50', str(q['AUTD']['price']) == '891.50')
check('黄金（沪金95）= 890.60', str(q['AU9995']['price']) == '890.60')
check('铂金 = 410.00', str(q['PT9995']['price']) == '410.00')
check('★★ 白银按千克报的 14365 → 换算成 14.365 元/克（不换算就差一千倍）',
      str(q['AGTD']['price']) == '14.365', str(q['AGTD']['price']))
check('带上了日期和时间', q['AU9999']['date'] == '2026-10-08'
      and q['AU9999']['time'] == '15:30:01',
      '%s %s' % (q['AU9999']['date'], q['AU9999']['time']))

# 缺品种：空串不能当成 0
partial = SINA_OK.replace('891.50,0,891.01', ',0,891.01')
q2 = M.parse_sina(partial)
check('★★ 某个品种是空串 → **不收进来**（绝不塞个 0 冒充「0 元/克的黄金」）',
      'AUTD' not in q2 and len(q2) == 4, str(sorted(q2)))

try:
    M.parse_sina('var hq_str_gds_AU9999="";')
    check('★★ 一个都解析不出来 → 抛错（不是返回空卡）', False)
except M.MetalError:
    check('★★ 一个都解析不出来 → 抛错（不是返回空卡）', True)

print()
print('=' * 62)
print('二、卡片')
print('=' * 62)
card = M.format_card(q, None)
for label in ('黄金9999', '黄金T+D', '黄金', '白银', '铂金'):
    check('卡片里有「%s」' % label, label in card)
check('★ 每项都写「回购价格: 数字」（照用户给的那张参考图）',
      card.count('回购价格:') == 5, '%d 处' % card.count('回购价格:'))
check('白银保留 3 位小数（14.365）', '14.365' in card, card[:200].replace('\n', ' | '))
check('黄金保留 2 位小数（891.81）', '891.81 元/克' in card)
check('★★★ 用户要求：卡片里**不显示来源/交易所字样**',
      '来源' not in card and '交易所' not in card and '沪金95' not in card,
      card[-80:].replace('\n', ' | '))
check('★ 有分隔线和更新时间（跟参考图一样）',
      M.DIVIDER in card and '更新时间' in card and '2026-10-08 15:30:01' in card)
check('★ 单位「元/克」留着（丢了白银会被当成元/千克，差一千倍）',
      card.count('元/克') >= 5)
check('★ 没写 TRON', 'TRON' not in card.upper())
check('★ HTML 标签闭合（半截标签会让整条发不出去）',
      card.count('<b>') == card.count('</b>'))

card2 = M.format_card(q, M.Decimal('876.50'))
check('★ 有回收价时多一行', '黄金回收价' in card2 and '876.50' in card2)
check('★ 没有回收价时**那一行**不出现（不是显示个 0）',
      '黄金回收价' not in card and '876' not in card)

print()
print('=' * 62)
print('三、抓取：缓存 / 失败 / 回收价')
print('=' * 62)
FakeClient.reset({'sinajs': FakeResp(text=SINA_OK), 'apizero': RECYCLE_OK})
fresh()
got, rec = M.fetch_quotes()
check('抓到 5 个品种', len(got) == 5)
check('★ 回收价也抓到了（黄金回收价格 876.50）', str(rec) == '876.50', str(rec))
n_hits = len(FakeClient.hits)
M.fetch_quotes()
check('★★ 30 秒内再问一次：**不再打网络**（缓存生效）',
      len(FakeClient.hits) == n_hits, '打了 %d 次' % (len(FakeClient.hits) - n_hits))

FakeClient.reset({'sinajs': RuntimeError('连不上')})
fresh()
try:
    M.fetch_quotes()
    check('★★ 行情抓不到 → 抛错（由调用方回「获取失败」）', False)
except Exception:
    check('★★ 行情抓不到 → 抛错（由调用方回「获取失败」）', True)

FakeClient.reset({'sinajs': FakeResp(text=SINA_OK),
                  'apizero': RuntimeError('回收价接口挂了')})
fresh()
got3, rec3 = M.fetch_quotes()
check('★★ 回收价接口挂了 → **不影响行情价**，那一行不显示',
      len(got3) == 5 and rec3 is None, str(rec3))

FakeClient.reset({'sinajs': FakeResp(text=SINA_OK),
                  'apizero': FakeResp(payload={'data': {'domestic': []}})})
fresh()
got4, rec4 = M.fetch_quotes()
check('★ 回收价接口返回空 → 也是 None，不炸', rec4 is None and len(got4) == 5)

print()
print('=' * 62)
print('四、触发词')
print('=' * 62)
YES = ['G', 'g', '贵金属', '金价', '金属价', '金属价格', '贵金属价格', '回收价',
       'G ', ' G', '/metal']
NO = ['币价', 'z0', 'bj', '+100', '1000/9', '账单', '账单 ', '', '  ', 'G价',
      '黄金', '白银', '群主', 'gogo']
for t in YES:
    check('「%s」→ 出卡片' % t, M.is_metal_command(t))
for t in NO:
    check('「%s」→ 不触发' % (t or '（空）'), not M.is_metal_command(t))

print()
print('=' * 62)
print('五、接线：群里发 G（端到端）')
print('=' * 62)


class FakeAPI:
    def __init__(self):
        self.calls = []

    def call(self, method, **p):
        self.calls.append((method, p))
        return {'message_id': len(self.calls)}

    def call_file(self, method, field, fn, obj, **p):
        self.calls.append((method, p))
        return {'message_id': len(self.calls)}

    def last_of(self, method):
        for m, p in reversed(self.calls):
            if m == method:
                return p
        return {}


class FakeMgr:
    def save(self):
        pass

    def is_expired(self, b):
        return False


CHAT = -1001234567890


def mk():
    r = LedgerRunner(FakeMgr(), {'id': 'metaltest', 'token': '1:FAKE',
                                 'admin_ids': [111], 'owner_id': 111,
                                 'enabled': True, 'type': 'ledger'})
    r.api = FakeAPI()
    r.me = {'id': 999, 'username': 'testbot'}
    return r


def upd(text, uid=222, mid=1, chat_type='supergroup', chat_id=CHAT):
    return {'update_id': mid, 'message': {
        'message_id': mid, 'date': int(time.time()),
        'chat': {'id': chat_id, 'type': chat_type, 'title': '测试群'},
        'from': {'id': uid, 'first_name': '张', 'username': 'zs'},
        'text': text}}


# ★★ 线程这条要**卡住网络**才测得准 —— 假网络秒回的话，
#    后台线程可能在 handle() 返回前就干完了，看着像「同步」的
gate = threading.Event()
FakeClient.reset({'sinajs': BlockingResp(gate, SINA_OK), 'apizero': RECYCLE_OK})
fresh()
r = mk()
r.handle(upd('G'))
check('★★★ 网络卡住时 handle() 照样**立刻返回**（记账的本职不能被查价拖住）',
      not r.api.calls, str(r.api.calls)[:80])
gate.set()
for _ in range(60):
    if r.api.calls:
        break
    time.sleep(0.1)
p = r.api.last_of('sendMessage')
check('★★ 发到**群里**（不是私聊）', p.get('chat_id') == CHAT, str(p.get('chat_id')))
check('★ 卡片内容对', '贵金属实时价格' in (p.get('text') or '')
      and '黄金9999' in (p.get('text') or ''))
check('★ 用的是 HTML（卡片里有 <b>）', p.get('parse_mode') == 'HTML')

# 抓不到的时候：实话实说，绝不编一个价格
FakeClient.reset({'sinajs': RuntimeError('断了')})
fresh()
r2 = mk()
r2.handle(upd('G', mid=2))
for _ in range(60):
    if r2.api.calls:
        break
    time.sleep(0.1)
t = r2.api.last_of('sendMessage').get('text') or ''
check('★★★ 抓不到 → 回「获取失败」，**绝不编一个价格出来**',
      '失败' in t and '891' not in t, t[:50])

# 不能抢记账 / 算式的活
FakeClient.reset({'sinajs': FakeResp(text=SINA_OK), 'apizero': RECYCLE_OK})
fresh()
r3 = mk()
r3.handle(upd('+100 张三', mid=3))
_ent = r3.store.entries(CHAT)
check('★ `+100 张三` 还是记账，没被贵金属抢走',
      len(_ent) == 1 and '贵金属' not in (r3.api.last_of('sendMessage').get('text') or ''),
      '%d 笔' % len(_ent))
r3.handle(upd('1+1', mid=4, chat_id=-100999))
check('★ `1+1` 还是算式', '2' in (r3.api.last_of('sendMessage').get('text') or ''))

# 群里怎么发都行，私聊也行（跟币价一样不设权限）
r4 = mk()
r4.handle(upd('贵金属', uid=333, mid=5, chat_type='private', chat_id=333))
for _ in range(60):
    if r4.api.calls:
        break
    time.sleep(0.1)
check('★ 私聊发「贵金属」也能查（谁都能问，不设权限）',
      r4.api.last_of('sendMessage').get('chat_id') == 333)

r.close_db()
r2.close_db()
r3.close_db()
r4.close_db()
shutil.rmtree(TMP, ignore_errors=True)
print()
print('=' * 62)
print('结果：%d 项通过，%d 项失败' % (len(OK), len(BAD)))
for x in BAD:
    print('  ❌', x)
print('=' * 62)
sys.exit(1 if BAD else 0)
