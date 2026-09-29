# -*- coding: utf-8 -*-
"""记账机器人 · 私聊查 TRC20 地址 + 监听

不联网：链上接口（tron_chain.chain_get / recent / transfers）全部换成假的。
★ 最要命的一条是「**群里发地址还是回防篡改图**」——
  那是客户拿来核对收款地址的老功能，被这次改动碰坏就麻烦了。

★ 各小节共用同一个 sqlite 文件（按机器人 id 命名），所以每段开头都要
  `reset_watches()` —— 不清的话上一段加的监听会漏到下一段（第一版就踩了）。
"""
import io
import os
import re
import shutil
import sqlite3
import sys
import threading
import time

sys.stdout.reconfigure(encoding='utf-8')

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = os.path.join(HERE, '_tmp_lgtron')
DB = os.path.join(TMP, 'data', 'trontest.watches.sqlite3')
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(os.path.join(TMP, 'data'), exist_ok=True)

import core                                    # noqa: E402
core.BASE_DIR = TMP
core.DATA_DIR = os.path.join(TMP, 'data')

from runners.ledger import LedgerRunner        # noqa: E402
from runners.ledger import tron_chain as tc    # noqa: E402
from runners.ledger import tron_watch as tw    # noqa: E402

OK, BAD = [], []


def check(name, cond, extra=''):
    (OK if cond else BAD).append(name)
    print('  %s %s%s' % ('✅' if cond else '❌', name,
                         ('  → %s' % (extra,)) if extra else ''))


class FakeAPI:
    def __init__(self):
        self.calls = []
        # ★ method -> 报错文本。用来演「发送失败」（比如用户屏蔽了机器人）
        self.fail = {}

    def call(self, method, **p):
        self.calls.append((method, p))
        if method in self.fail:
            raise core.TgError(self.fail[method])
        return {'message_id': len(self.calls)}

    def call_file(self, method, field, fn, obj, **p):
        self.calls.append((method, p))
        return {'message_id': len(self.calls)}

    def last(self):
        return self.calls[-1][1] if self.calls else {}

    def methods(self):
        return [c[0] for c in self.calls]

    def all_text(self):
        return '\n'.join(c[1].get('text') or '' for c in self.calls)


class FakeMgr:
    def save(self):
        pass

    def is_expired(self, b):
        return False


CHAT = -1001234567890
BOSS = 111
GUEST = 222
BOT = {'id': 'trontest', 'token': '1:FAKE', 'note': '链上测试',
       'admin_ids': [BOSS], 'owner_id': BOSS, 'enabled': True}

# 三个**校验和都对**的真地址（改一位就会被拒 —— 第三节专门测）
ADDR = 'TFMsoNbmzTEU7toDB8LvUhicPHhVQN1KWf'
OTHER = 'T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb'
THIRD = 'TNXoiAJ3dct8Fjg4M9fkLFh9S2v9TXc32G'

_LIVE = []


def mk():
    r = LedgerRunner(FakeMgr(), dict(BOT))
    r.api = FakeAPI()
    _LIVE.append(r)
    return r


def reset_watches():
    """清空监听表。★ 各段共用同一个库文件，不清就会互相污染"""
    if os.path.exists(DB):
        c = sqlite3.connect(DB)
        c.execute('DELETE FROM watches')
        c.execute('DELETE FROM watch_events')
        c.commit()
        c.close()


def cleanup():
    for r in _LIVE:
        try:
            r.close_db()
        except Exception:
            pass
    _LIVE.clear()
    shutil.rmtree(TMP, ignore_errors=True)


def upd(text, uid=GUEST, chat_type='private', chat_id=None, mid=1):
    m = {'message_id': mid, 'date': int(time.time()),
         'chat': {'id': chat_id if chat_id is not None else uid,
                  'type': chat_type, 'title': '测试群'},
         'from': {'id': uid, 'first_name': '张', 'last_name': '三',
                  'username': 'zhangsan', 'is_bot': False},
         'text': text}
    return {'update_id': mid, 'message': m}


def cq(data, uid=GUEST, mid=5):
    return {'id': 'cb1', 'data': data, 'from': {'id': uid},
            'message': {'message_id': mid,
                        'chat': {'id': uid, 'type': 'private'}}}


def wait_calls(r, timeout=3.0):
    t = time.time()
    while time.time() - t < timeout:
        if r.api.calls:
            return True
        time.sleep(0.02)
    return False


def wait_text(r, needle, timeout=5.0):
    """等到某句话出现（后台线程干完活）"""
    t = time.time()
    while time.time() - t < timeout:
        if needle in r.api.all_text():
            return True
        time.sleep(0.02)
    return False


# ★★ 「验证 Key」的桩：**不能真联网**。真实现在 tron_chain.probe() 里，
#    它打的是真 TronGrid。测试里换成假的，同时**记下被调了几次**，
#    用来证明「保存前确实验过」。
probe_mode = {'good': True, 'why': '账户查询和交易查询都通了'}
_probed = {'n': 0}


def fake_probe(api_key=''):
    _probed['n'] += 1
    return probe_mode['good'], probe_mode['why']


tc.probe = fake_probe


def wait_card(r, timeout=5.0):
    """★ 等**查询结果**回来。

    ★ 为什么要等：`send_card()` 先回一句「⏳ 正在查询…」，
      查完再**改**那条消息。所以「有调用了」不等于「卡片好了」——
      只等 `wait_calls` 的话，读到的是那句占位提示。
      等它出现卡片才有的「创建时间：」就说明改好了。
    """
    t = time.time()
    while time.time() - t < timeout:
        if '创建时间：' in r.api.all_text() or '查询失败' in r.api.all_text():
            return True
        time.sleep(0.02)
    return False


# ================= 假的链上接口 =================
NOW = int(time.time() * 1000)
_STATE = {'usdt': 42536594906, 'trx': 1981069, 'recent': [], 'scan': {}}


def fake_chain_get(address, params=None, api_key=''):
    return [{'address': address, 'balance': _STATE['trx'],
             'trc20': [{tc.USDT: str(_STATE['usdt'])}],
             'create_time': 1757384709000,
             'latest_opration_time': 1759074078000}]


def fake_recent(address, coin, limit=20, api_key=''):
    return list(_STATE['recent'])


def fake_transfers(address, coin, since, api_key=''):
    return list(_STATE['scan'].get(coin, [])), int(time.time() * 1000)


def dead(*a, **k):
    raise ValueError('挂了')


def stub_chain(ok=True):
    tc.chain_get = fake_chain_get if ok else dead
    tc.recent = fake_recent if ok else dead
    tc.transfers = fake_transfers


def tx(direction, amount, ts, txid, coin='USDT', address=None):
    a = address or ADDR
    return {'id': coin + ':' + txid, 'hash': txid, 'coin': coin,
            'from': OTHER if direction == 'in' else a,
            'to': a if direction == 'in' else OTHER,
            'amount': amount, 'timestamp': ts}


stub_chain()

print('=' * 62)
print('一、★ 私聊发地址 → 查余额卡片 + 5 个按钮')
print('=' * 62)
_STATE['recent'] = [tx('out', 1088 * 10 ** 6, NOW - 3600_000, 'a' * 64),
                    tx('in', 83 * 10 ** 6, NOW - 7200_000, 'b' * 64)]
r = mk()
r.api.calls.clear()
r.handle(upd(ADDR, mid=1))
check('★ 私聊发地址有反应了', wait_calls(r), '%d 次调用' % len(r.api.calls))
check('★★ 先回一句「⏳ 正在查询…」（别让用户干等着以为机器人死了）',
      '正在查询' in r.api.all_text())
wait_card(r)
t = r.api.all_text()
check('★★ 余额跟服务器那套一样（USDT 42536.594906 / TRX 1.981069）',
      '42536.594906' in t and '1.981069' in t, t[:100].replace('\n', ' '))
check('★ 有「最近 20 笔 USDT 交易（已确认）」', '最近 20 笔 USDT 交易' in t)
check('★ 转账一行一条（时间 | 方向 | 金额）',
      '| 转出 | 1088 USDT' in t and '| 转入 | 83 USDT' in t,
      str([x for x in t.split('\n') if '|' in x][:2]))
check('★ 有创建时间和最近操作', '创建时间：' in t and '最近操作：' in t)
check('★ 有「UTC+8 · 仅 TRX/USDT」', '仅 TRX/USDT' in t)
kb = (r.api.last().get('reply_markup') or {}).get('inline_keyboard') or []
labels = [b['text'] for row in kb for b in row]
check('★★ 5 个按钮都在（照你截图那排法）',
      len(labels) == 5 and all(any(k in x for x in labels) for k in
                               ('加入监听', '刷新查询', '账单统计',
                                '地址簿', '首页')), str(labels))
stat_btn = [b for row in kb for b in row if '账单统计' in b['text']]
check('★ 「账单统计」是回调按钮（链上详情已按用户要求换成它）',
      bool(stat_btn) and stat_btn[0].get('callback_data', '').startswith(
          'tw:stats:'), str(stat_btn))

print()
print('=' * 62)
print('二、★★★ 群里发地址**还是防篡改核对图**（老功能一个字不能变）')
print('=' * 62)
r = mk()
r.api.calls.clear()
r.handle(upd(ADDR, chat_type='supergroup', chat_id=CHAT, mid=2))
check('★★★ 群里回的是**图片**（sendPhoto），不是查询卡片',
      r.api.methods() and r.api.methods()[-1] == 'sendPhoto',
      str(r.api.methods()))
check('★ 而且没把余额卡片发到群里',
      '最近 20 笔 USDT 交易' not in r.api.all_text(), r.api.all_text()[:50])
check('★ 图片发到群里（不是私聊）',
      str(r.api.last().get('chat_id')) == str(CHAT),
      str(r.api.last().get('chat_id')))

print()
print('=' * 62)
print('三、★ 地址校验位不对 → 明确报错，不去链上查')
print('=' * 62)
BAD_ADDR = ADDR[:-1] + 'C'          # 改最后一位，校验和就对不上了
r = mk()
r.api.calls.clear()
r.handle(upd(BAD_ADDR, mid=3))
time.sleep(0.4)
check('★ 报「不是有效的 TRC20 地址」', '不是有效的 TRC20 地址' in r.api.all_text(),
      r.api.all_text()[:56])
check('★ 没有去链上查（省得查回「未激活」吓人一跳）',
      '最近 20 笔' not in r.api.all_text())

print()
print('=' * 62)
print('四、★ 防刷：连发两次，第二次被挡')
print('=' * 62)
r = mk()
r.api.calls.clear()
r.handle(upd(ADDR, mid=4))
wait_calls(r)
r.api.calls.clear()
r.handle(upd(ADDR, mid=5))
time.sleep(0.3)
# ★ 用户 2026-09-29 要求：**不拦**，发一个查一个
#   （「我不怕额度刷光……我要发一个它查询一个，额度我多申请几个 key 就行了」）
check('★★ 连着发第二次**照样查**（不再提示「太频繁」）',
      '太频繁' not in r.api.all_text() and r.api.calls,
      r.api.all_text()[:40].replace('\n', ' '))

print()
print('=' * 62)
print('五、★ 加入监听 / 重复加 / 上限')
print('=' * 62)
reset_watches()
r = mk()
r.api.calls.clear()
r.on_callback(cq('tw:add:' + ADDR))
check('★ 点「加入监听」存下来了', len(r.tronw.mine(GUEST)) == 1,
      '%d 个' % len(r.tronw.mine(GUEST)))
check('★ 加完一句话确认（用户要求别啰嗦）',
      '已加入监听' in r.api.all_text(), r.api.all_text()[:40].replace('\n', ' '))
check('★ 加完直接显示「管理此地址」', '尚未完成首次同步' in r.api.all_text())
_t = r.api.all_text()
check('★★ 管理界面**不再有**那几句啰嗦提示（用户截图里点名要去掉的）',
      '不承诺秒级' not in _t and '按各币种原单位' not in _t
      and '**' not in _t, _t[:70].replace('\n', ' '))
r.api.calls.clear()
r.on_callback(cq('tw:add:' + ADDR))
check('★ 重复加会被拒（文案跟服务器那套一致）',
      '已在你的地址簿中' in r.api.all_text(), r.api.all_text()[:50])
# 上限：临时把上限压到 2，加满再加就该被拒
_old_max = tw.MAX_WATCHES
tw.MAX_WATCHES = 2
try:
    r.tronw.add(GUEST, OTHER)
    try:
        r.tronw.add(GUEST, THIRD)
        check('★ 超过上限会被拒', False, '居然加进去了')
    except ValueError as e:
        check('★ 超过上限会被拒', '最多' in str(e), str(e))
finally:
    tw.MAX_WATCHES = _old_max

# ★ 卡片上的「账单统计」在**没加监听**时也会被点到 —— 要给句明白话
reset_watches()
r2 = mk()
r2.api.calls.clear()
r2.on_callback(cq('tw:stats:' + ADDR, uid=GUEST))
check('★ 没加监听就点「账单统计」→ 告诉他先去加监听',
      '加入监听' in r2.api.all_text(), r2.api.all_text()[:50])

print()
print('=' * 62)
print('六、★★ 扫链：订阅前的历史一条都不推，订阅后的推给本人')
print('=' * 62)
reset_watches()
r = mk()
r.api.calls.clear()
r.on_callback(cq('tw:add:' + ADDR, uid=GUEST))
added = int(r.tronw.one(GUEST, ADDR)['added'])
_STATE['scan'] = {
    'USDT': [tx('in', 500 * 10 ** 6, added - 86400_000, 'c' * 64),
             tx('in', 700 * 10 ** 6, added + 60_000, 'd' * 64)],
    'TRX': []}
r.api.calls.clear()
r.tronw.scan()
got = r.api.all_text()
check('★★ 订阅**之前**那笔历史没推（不然一加监听就被淹）',
      '500' not in got, got[:60].replace('\n', ' '))
check('★★ 订阅**之后**那笔推了', '700' in got, got[:80].replace('\n', ' '))
check('★ 推给**加监听的那个人**（不是发群里）',
      str(r.api.last().get('chat_id')) == str(GUEST),
      str(r.api.last().get('chat_id')))
check('★ 提醒里带「查看这笔交易」按钮',
      '查看这笔交易' in str(r.api.last().get('reply_markup')))
# ★★ 用户 2026-09-29 要的：付款方/收款方（转入地址/转出地址）**简洁格式也要有**
check('★★ 提醒里有付款方和收款方（转入/转出地址）',
      '付款方' in got and '收款方' in got, got[:130].replace('\n', ' '))
check('★ 而且地址是真的（不是空的 <code></code>）',
      '<code>T' in got and got.count('<code>T') >= 2,
      '带 T 开头的地址 %d 个' % got.count('<code>T'))
r.api.calls.clear()
r.tronw.scan()
check('★ 同一条不会重复推（按 txid 去重）', '700' not in r.api.all_text(),
      r.api.all_text()[:40])

# ★★★ 提醒**没发出去**时绝不能标「已通知」（2026-09-29 修的）
#     标掉了就等于「这笔钱的提醒永远丢了」，用户还以为没人给他转账。
r.api.calls.clear()
_STATE['scan'] = {
    'USDT': [tx('in', 900 * 10 ** 6, added + 120_000, 'e' * 64)],
    'TRX': []}
r.api.fail['sendMessage'] = '400 Bad Request: bot was blocked by the user'
r.tronw.scan()
r.api.fail.pop('sendMessage', None)
check('★★★ 发提醒失败 → 事件**没有**被标成已通知',
      not r.tronw.seen(GUEST, ADDR, 'USDT:' + 'e' * 64))
check('★ 发送失败也没把扫链搞崩（synced 照常更新）',
      (r.tronw.one(GUEST, ADDR) or {}).get('error', '') == '')
# 下一轮扫描要**重推**（用户屏蔽解除了就该收到）
r.api.calls.clear()
r.tronw.scan()
check('★★★ 下一轮扫描会**重推**那条没发出去的提醒',
      '900' in r.api.all_text(), r.api.all_text()[:60].replace('\n', ' '))
check('★ 这次发出去了 → 才标成已通知',
      r.tronw.seen(GUEST, ADDR, 'USDT:' + 'e' * 64))
r.api.calls.clear()
r.tronw.scan()
check('★ 重推成功后不再重复推', '900' not in r.api.all_text())

print()
print('=' * 62)
print('七、★ 通知设置：《只看转入》和《最低金额》')
print('=' * 62)
reset_watches()
r = mk()
r.on_callback(cq('tw:add:' + ADDR, uid=GUEST))
r.tronw.set_field(GUEST, ADDR, 'direction', 'in')       # 只看转入
t2 = int(time.time() * 1000) + 10
_STATE['scan'] = {'USDT': [tx('out', 900 * 10 ** 6, t2, 'e' * 64)], 'TRX': []}
r.api.calls.clear()
r.tronw.scan()
check('★★ 设成「只看转入」后，转出不提醒', '900' not in r.api.all_text(),
      r.api.all_text()[:50].replace('\n', ' '))
r.tronw.set_field(GUEST, ADDR, 'direction', 'both')
r.tronw.set_field(GUEST, ADDR, 'minimum', 1000 * 10 ** 6)
t3 = t2 + 10
_STATE['scan'] = {'USDT': [tx('in', 500 * 10 ** 6, t3, 'f' * 64)], 'TRX': []}
r.api.calls.clear()
r.tronw.scan()
check('★★ 低于最低金额的不提醒', '500' not in r.api.all_text(),
      r.api.all_text()[:50].replace('\n', ' '))
t4 = t3 + 10
_STATE['scan'] = {'USDT': [tx('in', 2000 * 10 ** 6, t4, '0' * 64)], 'TRX': []}
r.api.calls.clear()
r.tronw.scan()
check('★ 高于最低金额的照常提醒', '2000' in r.api.all_text(),
      r.api.all_text()[:70].replace('\n', ' '))
# 币种：只推 TRX 时，USDT 的不推
r.tronw.set_field(GUEST, ADDR, 'minimum', 0)
r.tronw.set_field(GUEST, ADDR, 'coins', 'TRX')
t5 = t4 + 10
_STATE['scan'] = {'USDT': [tx('in', 3000 * 10 ** 6, t5, '1' * 64)], 'TRX': []}
r.api.calls.clear()
r.tronw.scan()
check('★ 设成「只推 TRX」后，USDT 的不推', '3000' not in r.api.all_text(),
      r.api.all_text()[:50].replace('\n', ' '))

print()
print('=' * 62)
print('八、★ 地址簿 / 账单统计 / 停止监听（要先确认）')
print('=' * 62)
reset_watches()
r = mk()
r.on_callback(cq('tw:add:' + ADDR, uid=GUEST))
r.api.calls.clear()
r.on_callback(cq('tw:book', uid=GUEST))
check('★ 地址簿列出地址', ADDR in r.api.all_text(), r.api.all_text()[:50])
check('★ 只列**自己**的（别人的监听不给你看）',
      '1/10 个' in r.api.all_text(), r.api.all_text()[:40])
r.api.calls.clear()
r.on_callback(cq('tw:stats:' + ADDR, uid=GUEST))
check('★ 账单统计出得来', '收支统计' in r.api.all_text(),
      r.api.all_text()[:44].replace('\n', ' '))
r.api.calls.clear()
r.on_callback(cq('tw:del:' + ADDR, uid=GUEST))
check('★ 停止监听要先确认（不能点一下就删）',
      '确认停止监听' in r.api.all_text() and bool(r.tronw.one(GUEST, ADDR)),
      r.api.all_text()[:40].replace('\n', ' '))
r.api.calls.clear()
r.on_callback(cq('tw:ok:' + ADDR, uid=GUEST))
check('★★ 确认之后才真的删掉', r.tronw.one(GUEST, ADDR) is None)
check('★ 而且本地统计也清了',
      r.tronw.totals(GUEST, ADDR, 0, 10 ** 14) == {})

print()
print('=' * 62)
print('九、★★ 备注输入流：不能被记账/算式吃掉')
print('=' * 62)
reset_watches()
r = mk()
r.on_callback(cq('tw:add:' + ADDR, uid=GUEST))
r.api.calls.clear()
r.on_callback(cq('tw:note:' + ADDR, uid=GUEST))
check('★ 点了「设置备注」会让他发内容', '备注' in r.api.all_text(),
      r.api.all_text()[:36].replace('\n', ' '))
# ★★ 关键一条：备注写「+100」—— 那是记账的格式，绝不能被记成一笔账
r.handle(upd('+100', uid=GUEST, mid=99))
time.sleep(0.2)
w = r.tronw.one(GUEST, ADDR)
check('★★ 备注「+100」存成了备注，没被记成一笔账',
      bool(w) and w['note'] == '+100', '备注=%r' % ((w or {}).get('note'),))
check('★★ 而且没有产生任何流水', len(r.store.entries(GUEST)) == 0,
      '%d 笔' % len(r.store.entries(GUEST)))
r.tronw.wait_input(GUEST, 'note', ADDR)
r.handle(upd('清空', uid=GUEST, mid=100))
time.sleep(0.2)
check('★ 发「清空」能去掉备注',
      (r.tronw.one(GUEST, ADDR) or {}).get('note') == '')
# 过期了不吃消息（别把人家正经记账吞了）
r.tronw.wait_input(GUEST, 'note', ADDR)
r.tronw._await[GUEST]['at'] = time.time() - 9999
r.api.calls.clear()
r.handle(upd('+50', uid=GUEST, mid=101))
check('★ 等输入超时后**不吃**消息（正经记账照走）',
      len(r.store.entries(GUEST)) >= 1, '%d 笔' % len(r.store.entries(GUEST)))

print()
print('=' * 62)
print('十、★ 链上接口挂了 → 照实说，绝不显示 0')
print('=' * 62)
reset_watches()
stub_chain(ok=False)
r = mk()
r.api.calls.clear()
r.handle(upd(ADDR, mid=1))
wait_card(r)
t = r.api.all_text()
check('★★ 查不到时明说「不代表余额为零」',
      '不代表余额为零' in t, t[:60].replace('\n', ' '))
check('★★ 绝没有把余额显示成 0', 'USDT 余额：0' not in t, t[:50])
# ★ 2026-09-29：失败原因要**分类说出来**（网络/限流/Key 不对……），
#   不能再是一句含糊的「查询失败」。这里桩的是普通异常，报原文即可。
check('★ 查不到时给了原因（不是干巴巴一句「失败」）',
      '余额暂时查不到（' in t)
stub_chain()

print()
print('=' * 62)
print('十一、★ 别的按钮前缀不受影响 / 主人能配链上密钥')
print('=' * 62)
r = mk()
r.api.calls.clear()
r.on_callback(cq('nope:add:' + ADDR))
check('★ 不认识的按钮前缀 → 不处理', not r.api.calls)
r.api.calls.clear()
r.handle(upd('设置密钥 abcdef123456', uid=GUEST, mid=1))
check('★ 普通人设置密钥 → 被拒', '只有机器人主人' in r.api.all_text(),
      r.api.all_text()[:30])
r.api.calls.clear()
r.handle(upd('设置密钥 abcdef123456', uid=BOSS, mid=2))
check('★★ 先回一句「正在验证」（验证要联网，不能堵住收消息）',
      '正在验证' in r.api.all_text())
wait_text(r, '已保存')
check('★ 主人设置成功，而且**不回显**密钥',
      '已保存' in r.api.all_text() and 'abcdef123456' not in r.api.all_text(),
      r.api.all_text()[:40].replace('\n', ' '))
check('★ 密钥存下来了', list(r.tronw.keys()) == ['abcdef123456'],
      str([k[:4] for k in r.tronw.keys()]))
check('★★ 保存前**确实验证过**（不是光看长相就存）',
      _probed['n'] == 1, '验证次数 %d' % _probed['n'])
check('★★ 带密钥的那条私聊消息被删掉了（删的正是那条）',
      any(m == 'deleteMessage' and p.get('message_id') == 2
          for m, p in r.api.calls), str(r.api.methods()))

# ★★ 验证不通过 → **不许保存**，而且原来那个 Key 必须还在
r.api.calls.clear()
probe_mode['good'] = False
probe_mode['why'] = 'API Key 无效或没有权限（401/403）'
r.handle(upd('设置密钥 BADKEY123456', uid=BOSS, mid=4))
wait_text(r, '用不了')
check('★★ Key 验证不过 → 不保存',
      list(r.tronw.keys()) == ['abcdef123456'],
      str(r.tronw.keys() or '(空)'))
check('★ 明确告诉他为什么（Key 无效）',
      '无效' in r.api.all_text() or '权限' in r.api.all_text())
check('★ 也说明旧 Key 没受影响', '没受影响' in r.api.all_text())
probe_mode['good'] = True

r.api.calls.clear()
r.handle(upd('设置密钥 清空', uid=BOSS, mid=5))
check('★ 能清空', not r.tronw.keys() and '已清除' in r.api.all_text())

# ★★ 没配 Key 的时候，卡片要**主动教怎么配**（第五节的要求）
reset_watches()
r2 = mk()
r2.api.calls.clear()
r2.data.pop('tron_key', None)
r2.handle(upd(ADDR, mid=1))
wait_card(r2)
_t = r2.api.all_text()
check('★★ 没绑 Key 时卡片给出中文引导', '还没绑定 TRX/USDT 查询密钥' in _t)
check('★★ 引导里说明「不是钱包私钥/助记词」（这句最要紧）',
      '不是钱包私钥' in _t and '助记词' in _t)
check('★ 引导里给了申请入口', 'trongrid.io' in _t)
check('★ 引导里给了命令写法', '设置密钥' in _t)
# 配了 Key 就不该再出现这段唠叨
r2.data['tron_key'] = 'K' * 20
r2.api.calls.clear()
r2.handle(upd(ADDR, mid=1))
wait_card(r2)
check('★ 配了 Key 就不再出现引导', '还没绑定 TRX/USDT 查询密钥' not in r2.api.all_text())
r2.data.pop('tron_key', None)
reset_watches()

print()
print('=' * 62)
print('十二、★ 帮助里要写清查U/监听（不写没人知道能用）')
print('=' * 62)
from runners.ledger import commands as C         # noqa: E402

check('★ 帮助里加了【查U · 监听】那节',
      '查U' in C.HELP_TEXT and '加入监听' in C.HELP_TEXT)
check('★★ 说清了是**私聊**发地址（群里是另一回事）',
      '私聊机器人' in C.HELP_TEXT, C.HELP_TEXT[-90:].replace('\n', ' '))
check('★ 群里那条没被改掉（还是防篡改核对图）',
      '防篡改核对图' in C.HELP_TEXT)
check('★ 长度没超（Telegram 上限 4096）', len(C.HELP_TEXT) < 4096,
      '%d 字符' % len(C.HELP_TEXT))
check('★ HTML 标签是闭合的（半截标签会让整条发不出去）',
      C.HELP_TEXT.count('<code>') == C.HELP_TEXT.count('</code>')
      and C.HELP_TEXT.count('<b>') == C.HELP_TEXT.count('</b>'))

print()
print('=' * 62)
print('十三、★★ 查询速度与稳定性（并行、部分成功、限流分类）')
print('=' * 62)
reset_watches()

# --- 13.1 两个接口**并行**查（不是串着等）---
# ★ 怎么证并行：两个桩都去等同一个「栅栏」。并行的话两边都能等到；
#   串起来的话第一个就把栅栏等超时了 —— 这个判据是**确定性**的，
#   不像「掐表看着快不快」那样在慢机器上会误报。
_bar = {'b': threading.Barrier(2, timeout=3.0), 'serial': 0}
_real_chain_get, _real_recent = tc.chain_get, tc.recent


def par_chain_get(address, params=None, api_key=''):
    try:
        _bar['b'].wait()
    except threading.BrokenBarrierError:
        _bar['serial'] += 1
    return fake_chain_get(address, params, api_key)


def par_recent(address, coin, limit=20, api_key=''):
    try:
        _bar['b'].wait()
    except threading.BrokenBarrierError:
        _bar['serial'] += 1
    return fake_recent(address, coin, limit, api_key)


tc.chain_get, tc.recent = par_chain_get, par_recent
r = mk()
r.api.calls.clear()
r.handle(upd(ADDR, mid=1))
wait_card(r)
check('★★ 余额和交易记录是**并行**查的（不是串着等两轮）',
      _bar['serial'] == 0, '有 %d 个没等到（说明是串行的）' % _bar['serial'])
tc.chain_get, tc.recent = _real_chain_get, _real_recent

# --- 13.2 一个接口挂了，另一个的数据**照样显示** ---
def dead_balance(address, params=None, api_key=''):
    raise tc.ChainError('timeout', 'TRX/USDT 查询接口响应超时')


tc.chain_get = dead_balance
r = mk()
r.api.calls.clear()
r.handle(upd(ADDR, mid=1))
wait_card(r)
t = r.api.all_text()
check('★★ 余额挂了 → 交易记录**照样显示**（保住一半数据）',
      '| 转出 | 1088 USDT' in t, t[:70].replace('\n', ' '))
check('★ 余额那块照实说「查不到」并**给出原因**',
      '余额暂时查不到' in t and '超时' in t)
check('★ 余额挂了也**绝不显示 0**', 'USDT 余额：0' not in t)
tc.chain_get = _real_chain_get


# --- 13.3 反过来：余额好、交易记录挂了 ---
def dead_recent(address, coin, limit=20, api_key=''):
    raise tc.ChainError('net', '连不上 TRX/USDT 查询接口（ConnectionError）')


tc.recent = dead_recent
r = mk()
r.api.calls.clear()
r.handle(upd(ADDR, mid=1))
wait_card(r)
t = r.api.all_text()
check('★★ 交易记录挂了 → 余额**照样显示**', '42536.594906' in t,
      t[:70].replace('\n', ' '))
check('★ 交易记录那块说清「是没查到，不是没有交易」',
      '不是「没有交易」' in t, t[:90].replace('\n', ' '))
check('★ 也给了原因（连不上）', '连不上' in t)
tc.recent = _real_recent

# --- 13.4 被限流 → 要说清是限流，并把「去配 Key」的引导顶出来 ---
def rate_limited(address, params=None, api_key=''):
    raise tc.ChainError('rate', '查询接口限流了（429）')


tc.chain_get = rate_limited
r = mk()
r.data.pop('tron_key', None)
r.api.calls.clear()
r.handle(upd(ADDR, mid=1))
wait_card(r)
t = r.api.all_text()
check('★★ 被限流 → 明说是限流（不是笼统的「失败」）', '限流' in t,
      t[:90].replace('\n', ' '))
check('★★ 被限流 + 没配 Key → 顺手教他去申请 Key',
      '还没绑定 TRX/USDT 查询密钥' in t and '被限流' in t)
tc.chain_get = _real_chain_get

# --- 13.5 重试次数是**有限**的（不能一直轰炸接口）---
# ★ 直接测 `_get()` 本身：桩掉 requests.get，让它一直回 429。
#   ★ 顺手把 sleep 也桩掉 —— 不然这条测试要真等 3+6=9 秒。
_calls = {'n': 0}
_real_get_http = tc.requests.get
_real_sleep = time.sleep


class _Resp429:
    status_code = 429
    text = 'rate limited'


def _always_429(url, **kw):
    _calls['n'] += 1
    return _Resp429()


tc.requests.get = _always_429
time.sleep = lambda s: None          # ★ 只为了别真等 9 秒
try:
    try:
        tc._get('https://api.trongrid.io/v1/accounts/x', {}, 'k')
        check('★ 限流持续 → 最终抛错（不会无限重试）', False, '居然没抛')
    except tc.ChainError as e:
        check('★★ 限流持续 → 最终抛错，而且认得是限流', e.kind == 'rate')
    check('★★ 重试次数有限：一共只打 3 次（首发 + 3s + 6s 两轮退避）',
          _calls['n'] == 3, '实际打了 %d 次' % _calls['n'])
finally:
    tc.requests.get = _real_get_http
    time.sleep = _real_sleep

# ★ Key 不对（401）**不许重试** —— 重试一万次也还是错，纯属浪费
_auth = {'n': 0}


class _Resp401:
    status_code = 401
    text = 'invalid key'


def _always_401(url, **kw):
    _auth['n'] += 1
    return _Resp401()


tc.requests.get = _always_401
try:
    try:
        tc._get('https://api.trongrid.io/v1/accounts/x', {}, 'bad')
        check('★ Key 不对 → 直接抛错', False, '居然没抛')
    except tc.ChainError as e:
        check('★★ Key 不对（401）认得出来', e.kind == 'auth')
    check('★★ Key 不对**不重试**（重试也是白搭）', _auth['n'] == 1,
          '打了 %d 次' % _auth['n'])
finally:
    tc.requests.get = _real_get_http
# ★ 真正的退避表：限流必须是 3 秒起（实测 6 秒才恢复，退避短了等于白等）
check('★★ 限流退避是 3 秒、6 秒（不是 1.5s 那种白等）',
      tc.RETRY_WAIT.get('rate') == (3.0, 6.0), str(tc.RETRY_WAIT.get('rate')))
check('★ 每个请求都带超时（连接+读取都要有）',
      isinstance(tc.TIMEOUT, tuple) and len(tc.TIMEOUT) == 2,
      str(tc.TIMEOUT))

# --- 13.5b ★★ 多把 Key 轮询（这是防限流的正经办法）---
# ★★ 铁律：密钥**只存 data/ 里，绝不写进代码**。
#    代码要传公开仓库，写死等于公开（totp_secret 就是这么出事的）。
_src = io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'runners', 'ledger', 'runner.py'),
               encoding='utf-8').read()
check('★★★ 代码里没有任何写死的 TronGrid Key',
      not re.search(r"tron_key['\"]?\s*[:=]\s*['\"][A-Za-z0-9_\-]{16,}", _src),
      '代码里出现了像密钥的赋值')

reset_watches()
r = mk()
r.api.calls.clear()
r.data.pop('tron_keys', None)
r.data.pop('tron_key', None)
# 加三把
# ★ `all_text()` 是**所有历史调用**拼起来的，所以每轮必须先 clear，
#   否则第二次 wait 会看到第一次的「已保存」直接返回 —— 等于没等。
for i, k in enumerate(('KEYAAAA11111', 'KEYBBBB22222', 'KEYCCCC33333')):
    r.api.calls.clear()
    r.handle(upd('添加密钥 ' + k, uid=BOSS, mid=10 + i))
    wait_text(r, '已保存')
check('★★ 能连着加三把（添加密钥）',
      r.tronw.keys() == ['KEYAAAA11111', 'KEYBBBB22222', 'KEYCCCC33333'],
      str(len(r.tronw.keys())))
r.api.calls.clear()
r.handle(upd('密钥列表', uid=BOSS, mid=20))
_lst = r.api.all_text()
check('★★ 密钥列表只显示后 4 位（不把密钥回显出来）',
      '1111' in _lst and 'KEYAAAA11111' not in _lst, _lst[:60].replace('\n', ' '))
check('★ 列表说清有几把', '3' in _lst)
r.api.calls.clear()
r.handle(upd('添加密钥 KEYAAAA11111', uid=BOSS, mid=21))
check('★ 加重复的会被挡（不会存两遍）',
      '已经在里面' in r.api.all_text() and len(r.tronw.keys()) == 3)
# 加满到上限，再加就该被拒
for i in range(3, tw.MAX_KEYS):
    r.api.calls.clear()
    r.handle(upd('添加密钥 KEYDDDD%d' % i, uid=BOSS, mid=30 + i))
    wait_text(r, '已保存')
check('★ 加满了（%d 把）' % tw.MAX_KEYS,
      len(r.tronw.keys()) == tw.MAX_KEYS, str(len(r.tronw.keys())))
r.api.calls.clear()
r.handle(upd('添加密钥 KEYEXTRA99999', uid=BOSS, mid=39))
check('★ 超过上限会被拒', '最多存' in r.api.all_text())

# 轮询：连着要几次，三把都要出现
_seen = set(r.tronw.next_key() for _ in range(tw.MAX_KEYS * 2))
check('★★ 取 Key 是**轮询**的（每一把都会轮到）',
      len(_seen) == len(r.tronw.keys()), '%d 把里用到 %d 把' % (len(r.tronw.keys()), len(_seen)))

# 冷却：某一把被限流 → 期间不再用它
r.tronw.cool_key('KEYBBBB22222')
_seen2 = set(r.tronw.next_key() for _ in range(tw.MAX_KEYS * 2))
check('★★ 被限流的那把会**冷却跳过**（自动换别的）',
      'KEYBBBB22222' not in _seen2, str(sorted(x[-3:] for x in _seen2)))

# 换 Key 重试：第一把 429 → 自动用第二把把这次查询做完
_try = {'n': 0}


def first_429(address, params=None, api_key=''):
    _try['n'] += 1
    if api_key == 'KEYAAAA11111':
        raise tc.ChainError('rate', '查询接口限流了（429）')
    return fake_chain_get(address, params, api_key)


tc.chain_get = first_429
r.tronw._key_cool.clear()
r.api.calls.clear()
r.handle(upd(ADDR, mid=30))
wait_card(r)
check('★★★ 一把被限流 → 自动换下一把接着查（用户不用干等）',
      '42536.594906' in r.api.all_text(), r.api.all_text()[:60].replace('\n', ' '))
check('★ 换过了（说明真的重试了，不是碰巧第一把就行）', _try['n'] >= 2,
      '调用 %d 次' % _try['n'])
tc.chain_get = _real_chain_get

r.data.pop('tron_keys', None)
r.data.pop('tron_key', None)

# --- 13.5c ★★ 用户看得见的字里**不许出现 TRON**（只许写 TRX/USDT）---
# ★ 用户已经说过很多次了：客户在机器人里看到的是 TRX/USDT，不是 TRON。
#   商城那边早就有这条守卫（`_test_shop_ui.py`），记账这边一直漏着 —— 补上。
#   ★ 只查**会发到聊天里的字符串**：`TRONGRID` 那个接口地址、
#     `TRON-PRO-API-KEY` 请求头、变量名都不算（它们是给机器看的，
#     改了反而会把接口调坏）。
_led = io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'runners', 'ledger', 'tron_chain.py'),
               encoding='utf-8').read()
_wat = io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'runners', 'ledger', 'tron_watch.py'),
               encoding='utf-8').read()
_usr = io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'runners', 'usdt', 'runner.py'),
               encoding='utf-8').read()


def _visible_bad(src):
    """挑出「用户会看到的中文字符串里」含 TRON 的（排除接口常量/请求头）"""
    out = []
    for m in re.finditer(r"'([^'\n]*[一-鿿][^'\n]*)'", src):
        txt = m.group(1)
        if 'TRON' in txt and 'TRON-PRO' not in txt:
            out.append(txt[:50])
    return out


check('★★ 记账的报错文案里没有 TRON（只写 TRX/USDT）',
      not _visible_bad(_led), str(_visible_bad(_led)[:2]))
check('★★ 卡片的文案里没有 TRON', not _visible_bad(_wat),
      str(_visible_bad(_wat)[:2]))
check('★★ USDT 助手的提示里没有 TRON', not _visible_bad(_usr),
      str(_visible_bad(_usr)[:2]))
check('★ 说明书里也没有 TRON',
      'TRON' not in io.open(
          os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       '记账机器人_客户使用说明.txt'), encoding='utf-8').read())

# --- 13.6 ★★ 发一个查一个（用户明确要求：不许拦）---
# ★ 原来这里测的是「第二次被冷却挡住」。用户 2026-09-29 要求去掉：
#   「我不怕额度刷光……我要发一个它查询一个，发一个查询一个，
#     额度怕刷光我多申请几个 api 的 key 就行了」
#   → 额度靠多把 Key 轮询解决，不靠少让用户查。
reset_watches()
r = mk()
r.api.calls.clear()
r.handle(upd(ADDR, mid=1))
wait_card(r)
r.api.calls.clear()
r.handle(upd(ADDR, mid=2))
wait_card(r)
check('★★ 连发两次 → **两次都真的查了**（发一个查一个）',
      '太频繁' not in r.api.all_text()
      and '42536.594906' in r.api.all_text(),
      r.api.all_text()[:50].replace('\n', ' '))
reset_watches()

cleanup()
print()
print('=' * 62)
print('结果：%d 项通过，%d 项失败' % (len(OK), len(BAD)))
for b in BAD:
    print('  ❌', b)
print('=' * 62)
sys.exit(1 if BAD else 0)
