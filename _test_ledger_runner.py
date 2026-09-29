# -*- coding: utf-8 -*-
"""记账机器人的接线层测试 —— 灌真实的 update dict，验证分发正确

不联网、不启动线程：只构造 runner，直接调 handle()。
"""
import os
import shutil
import sys
import time

sys.stdout.reconfigure(encoding='utf-8')

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = os.path.join(HERE, '_tmp_lgr')
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(os.path.join(TMP, 'data'), exist_ok=True)

import core
core.BASE_DIR = TMP
core.DATA_DIR = os.path.join(TMP, 'data')

from runners.ledger import LedgerRunner

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

    def methods(self):
        return [c[0] for c in self.calls]


class FakeMgr:
    def save(self):
        pass

    def is_expired(self, b):
        return False


CHAT = -1001234567890
BOSS = 111
MEMBER = 222
BOT = {'id': 'ledgertest', 'token': '1:FAKE', 'note': '记账测试',
       'admin_ids': [BOSS], 'owner_id': BOSS, 'enabled': True}


_LIVE = []


def mk(bot=None):
    r = LedgerRunner(FakeMgr(), dict(bot or BOT))
    r.api = FakeAPI()
    _LIVE.append(r)
    return r


def cleanup():
    """★ 先关 sqlite 再删目录 —— Windows 上文件被占着删不掉，
       rmtree(ignore_errors=True) 会静默吞掉失败，每跑一次留一个临时目录"""
    for r in _LIVE:
        try:
            r.close_db()
        except Exception:
            pass
    _LIVE.clear()
    shutil.rmtree(TMP, ignore_errors=True)


def upd(text, uid=MEMBER, chat_type='supergroup', chat_id=CHAT,
        reply=None, mid=1):
    m = {'message_id': mid, 'date': int(time.time()),
         'chat': {'id': chat_id, 'type': chat_type, 'title': '测试群'},
         'from': {'id': uid, 'first_name': '张', 'last_name': '三',
                  'username': 'zhangsan', 'is_bot': False},
         'text': text}
    if reply:
        m['reply_to_message'] = reply
    return {'update_id': mid, 'message': m}


print('=' * 62)
print('一、★ 群消息能进来，而且回复发到群里')
print('=' * 62)
r = mk()
r.handle(upd('+100 李四'))
check('群消息被处理了', len(r.api.calls) > 0, '%d 次调用' % len(r.api.calls))
check('★ 回复发到【群 chat_id】，不是发言人的私聊',
      r.api.last().get('chat_id') == CHAT,
      'chat_id=%s（群是 %s，人是 %s）' % (r.api.last().get('chat_id'),
                                        CHAT, MEMBER))
check('用的是 sendMessage', r.api.methods()[-1] == 'sendMessage')

print()
print('=' * 62)
print('二、私聊也能用')
print('=' * 62)
r = mk()
r.handle(upd('+50', chat_type='private', chat_id=MEMBER))
check('私聊消息被处理', len(r.api.calls) > 0)
check('回复发到那个人的私聊', r.api.last().get('chat_id') == MEMBER)

print()
print('=' * 62)
print('三、其它三种机器人仍然不收群消息（行为不变）')
print('=' * 62)
from runners.kefu import KefuRunner
from runners.shop import ShopRunner
from runners.usdt import UsdtRunner
for cls in (KefuRunner, UsdtRunner, ShopRunner):
    check('%s.allow_groups 是 False' % cls.__name__, cls.allow_groups is False,
          str(cls.allow_groups))
check('只有记账是 True', LedgerRunner.allow_groups is True)

print()
print('=' * 62)
print('四、记账 + 账单 + 按钮')
print('=' * 62)
r = mk()
r.handle(upd('+100 李四', mid=1))
r.handle(upd('+200 王五', mid=2))
r.handle(upd('账单', mid=3))
t = r.api.last().get('text') or ''
check('账单出得来', '入款' in t, t[:46].replace('\n', ' '))
check('用了 HTML（不然标签会露出来）', r.api.last().get('parse_mode') == 'HTML')
check('关了链接预览', r.api.last().get('disable_web_page_preview') is True)
kb = r.api.last().get('reply_markup') or {}
check('带按钮', bool(kb.get('inline_keyboard')))
check('按钮是 ledger: 前缀（不跟会员/笔数撞）',
      all(b['callback_data'].startswith('ledger:')
          for row in (kb.get('inline_keyboard') or []) for b in row),
      str(kb)[:70])

print()
print('=' * 62)
print('五、点账单按钮')
print('=' * 62)
r = mk()
r.handle(upd('+100 李四', mid=1))
r.api.calls.clear()
r.handle({'update_id': 9, 'callback_query': {
    'id': 'cb1', 'from': {'id': MEMBER, 'first_name': '张'},
    'message': {'message_id': 5,
                'chat': {'id': CHAT, 'type': 'supergroup'}},
    'data': 'ledger:today'}})
ms = r.api.methods()
check('★ 先回 answerCallbackQuery（否则按钮一直转圈）',
      'answerCallbackQuery' in ms, str(ms))
check('刷新了账单（编辑或重发）',
      'editMessageText' in ms or 'sendMessage' in ms, str(ms))

print()
print('=' * 62)
print('六、多个机器人互不串味')
print('=' * 62)
a = mk()
b = mk({'id': 'otherbot', 'token': '2:FAKE', 'note': '另一个',
        'admin_ids': [BOSS], 'owner_id': BOSS, 'enabled': True})
a._welcome_at[CHAT] = 123
check('A 的入群记录不影响 B', CHAT not in b._welcome_at)
a.handle(upd('+100 李四', mid=1))
check('A 记了账', len(a.store.entries(CHAT)) >= 1,
      '%d 笔' % len(a.store.entries(CHAT)))
check('★ B 的库是空的（各用各的 sqlite）', len(b.store.entries(CHAT)) == 0,
      '%d 笔' % len(b.store.entries(CHAT)))

print()
print('=' * 62)
print('七、TRC20 地址 → 回核对图')
print('=' * 62)
r = mk()
r.api.calls.clear()
r.handle(upd('TBKw17Fxy5kBbev8MMZ16yVKuQTQfsETta'))
check('调了 sendPhoto（发图）', 'sendPhoto' in r.api.methods(),
      str(r.api.methods()))

print()
print('=' * 62)
print('八、算式直接算')
print('=' * 62)
r = mk()
r.api.calls.clear()
r.handle(upd('1+2*3'))
t = r.api.last().get('text') or ''
check('算式出结果 7', '7' in t, t[:40])

print()
print('=' * 62)
print('九、回复别人的消息记账（reply_user 解析）')
print('=' * 62)
r = mk()
r.handle(upd('+100', uid=MEMBER, mid=1,
             reply={'message_id': 99, 'text': '+50',
                    'from': {'id': 333, 'first_name': '王', 'last_name': '五',
                             'username': 'wangwu'}}))
e = r.store.entries(CHAT)
check('回复式记账也记上了', len(e) >= 1, '%d 笔' % len(e))

print()
print('=' * 62)
print('十、乱输入不炸')
print('=' * 62)
r = mk()
for weird in ('', '   ', '🤖', '++++', '+abc', '账单' * 30, 'x' * 3000,
              '/start', '/admin', '/id'):
    try:
        r.handle(upd(weird, mid=abs(hash(weird)) % 100000 + 1))
        check('不炸：%r' % (weird[:12] or '（空）'), True)
    except Exception as ex:
        check('不炸：%r' % (weird[:12] or '（空）'), False,
              '%s: %s' % (type(ex).__name__, ex))

print()
print('=' * 62)
print('十一、★★ 币价 / 设置实时汇率（原先是**死代码**，压根没接上）')
print('=' * 62)
# 2026-09-29：用户实测「z0 和 bj 输了没反应」。查出来是完整版里这两个
# 函数是 async 的（写在服务端单体里），搬过来只抄了函数、**没有任何调用**。
from decimal import Decimal                            # noqa: E402
from runners.ledger import price as P                  # noqa: E402

_real_fetch = P.fetch_prices
FIVE = [Decimal('7.25'), Decimal('7.26'), Decimal('7.27'),
        Decimal('7.28'), Decimal('7.29')]


def stub_fetch(prices=None, source='OKX C2C卖单'):
    P.fetch_prices = lambda timeout=0: (prices or FIVE, source)


def boom(timeout=0):
    raise OSError('网络不通')


def wait_calls(r, timeout=3.0):
    t = time.time()
    while time.time() - t < timeout:
        if r.api.calls:
            return True
        time.sleep(0.02)
    return False


stub_fetch()
r = mk()
r.api.calls.clear()
r.handle(upd('币价', uid=MEMBER, mid=1))
check('★ 币价有反应了（以前一个字都不回）', wait_calls(r),
      '%d 次调用' % len(r.api.calls))
t = r.api.last().get('text') or ''
check('  报的是抓来的价', '7.25' in t, t.replace('\n', ' ')[:52])
check('  带上来源', '来源' in t, t.replace('\n', ' ')[:52])

for w in ('bj', 'BJ', 'z0', 'Z0', '/price'):
    r.api.calls.clear()
    r.handle(upd(w, mid=hash(w) % 9999 + 1))
    check('  %s 也认' % w, wait_calls(r), '%d 次调用' % len(r.api.calls))

P.fetch_prices = boom
r.api.calls.clear()
r.handle(upd('bj', mid=77))
wait_calls(r)
check('★★ 抓不到就直说，绝不编一个价格出来',
      '失败' in (r.api.last().get('text') or ''),
      (r.api.last().get('text') or '')[:34])

stub_fetch()
r = mk()
r.store.set_rate(CHAT, '1')
r.api.calls.clear()
r.handle(upd('设置实时汇率', uid=BOSS, mid=1))
check('★ 群主发 → 汇率改成实时价了',
      r.store.get_settings(CHAT)[0] == Decimal('7.25'),
      str(r.store.get_settings(CHAT)[0]))
check('★★ 用的是**最新 1 档**（7.25，不是第 2 档 7.26）',
      r.store.get_settings(CHAT)[0] == Decimal('7.25'))
check('★ 标记成实时汇率了（账单里显示的样式跟手填的不一样）',
      r.store.is_realtime_rate(CHAT) is True)
check('  回复里说清了用的哪一档',
      '最新 1 档' in (r.api.last().get('text') or ''),
      (r.api.last().get('text') or '').replace('\n', ' ')[:60])

r.store.set_rate(CHAT, '1')
r.api.calls.clear()
r.handle(upd('设置实时汇率', uid=MEMBER, mid=2))
check('★ 普通成员 → 无权限',
      '无权限' in (r.api.last().get('text') or ''),
      (r.api.last().get('text') or '')[:30])
check('★★ 而且汇率纹丝不动', r.store.get_settings(CHAT)[0] == Decimal('1'),
      str(r.store.get_settings(CHAT)[0]))

# ★★★ 这条是钱的事：抓价失败时**绝不能**往汇率里写任何东西
P.fetch_prices = boom
r.store.set_rate(CHAT, '1')
r.api.calls.clear()
r.handle(upd('设置实时汇率', uid=BOSS, mid=3))
check('★★★ 抓价失败时汇率保持原样（写错价 = 账单算错 = 赔钱）',
      r.store.get_settings(CHAT)[0] == Decimal('1'),
      str(r.store.get_settings(CHAT)[0]))
check('★★ 报错里带上原汇率，客户一眼看到钱没被动过',
      '已保留' in (r.api.last().get('text') or ''),
      (r.api.last().get('text') or '')[:48])

r.store.set_rate(CHAT, '1')
r.api.calls.clear()
r.handle(upd('设置实时汇率', uid=BOSS, chat_type='private', chat_id=BOSS))
check('★ 私聊里发 → 提示去群里（跟「设置汇率」一个规矩）',
      '请在群内' in (r.api.last().get('text') or ''),
      (r.api.last().get('text') or '')[:30])

P.fetch_prices = _real_fetch

cleanup()
print()
print('=' * 62)
print('结果：%d 项通过，%d 项失败' % (len(OK), len(BAD)))
for x in BAD:
    print('  ❌', x)
print('=' * 62)
sys.exit(1 if BAD else 0)
