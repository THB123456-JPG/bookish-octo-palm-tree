# -*- coding: utf-8 -*-
"""记账核心逻辑测试 —— 纯逻辑，不通网、不碰 TG

验证从「记账机器人专业版」搬过来的记账逻辑（storage + commands）仍然正确。
"""
import io
import os
import shutil
import sys

import core

sys.stdout.reconfigure(encoding='utf-8')

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = os.path.join(HERE, '_tmp_ledger')
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP)

from runners.ledger.commands import Actor, format_bill, handle_text
from runners.ledger.storage import LedgerStore

OK, BAD = [], []
CHAT = -1001234567890          # 群 ID（负数）
OWNER_ID = 111
BOSS = Actor(OWNER_ID, 'boss', '群主')
MEMBER = Actor(222, 'zhangsan', '张三')
_n = [0]


def check(name, cond, extra=''):
    (OK if cond else BAD).append(name)
    print('  %s %s%s' % ('✅' if cond else '❌', name,
                         ('  → %s' % (extra,)) if extra else ''))


_LIVE = []


def _track(st):
    _LIVE.append(st)
    return st


def cleanup():
    """★ 先关 sqlite 再删目录 —— Windows 上文件被占着删不掉，
       rmtree(ignore_errors=True) 会静默吞掉失败，每跑一次留一个临时目录"""
    for st in _LIVE:
        try:
            st.close()
        except Exception:
            pass
    _LIVE.clear()
    shutil.rmtree(TMP, ignore_errors=True)


def mk():
    _n[0] += 1
    return _track(LedgerStore(os.path.join(TMP, 'st%d.sqlite3' % _n[0])))


_mid = [0]


def T(st, text, actor=None, reply_user=None, reply_text=None, owners=None):
    # ★ message_id 每条必须不同 —— 原版按它去重（防同一条消息重复记账），
    #   测试里如果都传 1，第二笔之后会被当成「同一条消息」直接跳过
    _mid[0] += 1
    return handle_text(store=st, chat_id=CHAT, actor=actor or BOSS, text=text,
                       owner_ids=owners if owners is not None else {OWNER_ID},
                       reply_user=reply_user, reply_text=reply_text,
                       message_id=_mid[0], reply_message_id=_mid[0] + 100000)


def es(st):
    return st.entries(CHAT)


print('=' * 62)
print('一、基本记账')
print('=' * 62)
st = mk()
r = T(st, '+100 张三')
check('+100 有回执', r is not None and bool(r.text))
e = es(st)
check('库里记了一笔', len(e) == 1, '%d 笔' % len(e))
if e:
    check('类型是入款(income)', e[0].kind == 'income', e[0].kind)
    check('金额是 100', float(e[0].amount) == 100, str(e[0].amount))
    check('备注记下了「张三」', '张三' in (e[0].note or ''), e[0].note)

print()
print('=' * 62)
print('二、各种写法都能识别')
print('=' * 62)
CASES = [
    ('+50', 'income', '50'),
    ('-30', 'income', '-30'),          # 负数 = 减分（还是 income 类）
    ('入款 20', 'income', '20'),
    ('收款 25', 'income', '25'),
    ('上分 12', 'income', '12'),
    ('下发 15', 'payout', '15'),
]
for text, kind, amt in CASES:
    st = mk()
    T(st, text)
    e = es(st)
    ok = e and e[0].kind == kind and float(e[0].amount) == float(amt)
    check('%-10s → %s %s' % (text, kind, amt), bool(ok),
          ('%s %s' % (e[0].kind, e[0].amount)) if e else '没记上')

print()
print('=' * 62)
print('三、账单')
print('=' * 62)
st = mk()
T(st, '+100 张三')
T(st, '+200 李四')
T(st, '下发 50')
r = T(st, '账单')
txt = r.text if r else ''
check('出账单了', bool(txt))
check('带 HTML 标签（要 parse_mode=HTML）', '<' in txt and '>' in txt)
check('有 300 入款', '300' in txt, txt[:80].replace('\n', ' '))
check('有 50 下发', '50' in txt)

r = T(st, '昨日账单')
check('昨日账单也能出', r is not None and bool(r.text))
r = T(st, '完整账单')
check('完整账单能出', r is not None and bool(r.text))
r = T(st, '+0')
check('+0 是完整账单的暗号', r is not None and bool(r.text))

print()
print('=' * 62)
print('四、撤销 / 清账')
print('=' * 62)
st = mk()
T(st, '+100 张三')
T(st, '账单')                      # 让它生成一张可回复的账单
before = len([x for x in es(st) if not x.voided_at])
r = T(st, '撤销', reply_text='账单 #1')
after = len([x for x in es(st) if not x.voided_at])
check('撤销能减掉一笔（或给出提示）',
      after < before or (r is not None and '撤' in (r.text or '')),
      '撤销前 %d 笔 → 撤销后 %d 笔' % (before, after))

st = mk()
T(st, '+100 张三')
T(st, '+200 李四')
T(st, '清账')
left = len([x for x in es(st) if not x.voided_at])
check('清账把账清了', left == 0 or '清' in (T(st, '清账').text or ''),
      '还剩 %d 笔' % left)

print()
print('=' * 62)
print('五、开关记账')
print('=' * 62)
st = mk()
T(st, '关闭记账')
r = T(st, '+100 张三')
check('关了之后不再记账', r is None or not es(st), '库里 %d 笔' % len(es(st)))
T(st, '开启记账')
T(st, '+100 张三')
check('开了之后又能记', len(es(st)) >= 1, '库里 %d 笔' % len(es(st)))

# ★ 「上课 / 下课」是用户要的别名（群里喊一嗓子就开关，比「关闭记账」好打）
n_before = len(es(st))
T(st, '下课')
T(st, '+100 下课之后')
check('★★ 「下课」= 关闭记账', len(es(st)) == n_before,
      '库里 %d 笔（原来 %d 笔）' % (len(es(st)), n_before))
T(st, '上课')
T(st, '+100 上课之后')
check('★★ 「上课」= 开启记账', len(es(st)) == n_before + 1,
      '库里 %d 笔' % len(es(st)))

print()
print('=' * 62)
print('六、费率 / 汇率 / 日切')
print('=' * 62)
st = mk()
r = T(st, '设置汇率 7.2')
check('能设置汇率', r is not None and bool(r.text), (r.text or '')[:40])
r = T(st, '查看费率')
check('能查看费率', r is not None and bool(r.text))
r = T(st, '设置费率 10')
check('能设置费率', r is not None and bool(r.text))
r = T(st, '日切3')
check('能设置日切', r is not None and bool(r.text))
r = T(st, '查看日切')
check('能查看日切', r is not None and bool(r.text))

print()
print('=' * 62)
print('七、权限（操作员）')
print('=' * 62)
st = mk()
r = T(st, '添加权限', actor=MEMBER, reply_user=MEMBER, reply_text='+100')
check('普通人加不了权限', r is not None and '只有' in (r.text or ''),
      (r.text or '')[:40])
r = T(st, '添加权限', actor=BOSS, reply_user=MEMBER, reply_text='+100')
check('群主能加权限', r is not None and bool(r.text), (r.text or '')[:40])
r = T(st, '操作员')
check('能列操作员', r is not None and ('张三' in (r.text or '') or '操作员' in (r.text or '')),
      (r.text or '')[:50].replace('\n', ' '))

print()
print('=' * 62)
print('八、两个库互不干扰（多开不串味）')
print('=' * 62)
a, b = mk(), mk()
T(a, '+100 张三')
check('A 库 1 笔', len(es(a)) == 1, str(len(es(a))))
check('B 库 0 笔（没被 A 影响）', len(es(b)) == 0, str(len(es(b))))
T(b, '+200 李四')
check('各记各的', len(es(a)) == 1 and len(es(b)) == 1)

print()
print('=' * 62)
print('九、边界情况不炸')
print('=' * 62)
st = mk()
for weird in ('', '   ', '++++', '账单账单', '+', '-', '+abc', '设置汇率 abc',
              '日切99', '清账', '撤销', '🤖', '+100' * 50):
    try:
        T(st, weird)
        check('乱输入不炸：%r' % (weird[:14] or '（空）'), True)
    except Exception as ex:
        check('乱输入不炸：%r' % (weird[:14] or '（空）'), False,
              '%s: %s' % (type(ex).__name__, ex))

print()
print('=' * 62)
print('★ 运行日志不能无限涨（客户那台硬盘不大）')
print('=' * 62)
_logp = os.path.join(TMP, 'logtest.txt')
_save = (core.LOG_FILE, core.LOG_MAX_BYTES, core._log_writes)
core.LOG_FILE = _logp
core.LOG_MAX_BYTES = 20000
core._log_writes = 199
_so = sys.stdout
sys.stdout = io.StringIO()                       # log() 会 print，吞掉
for i in range(600):
    core.log('日志 %d %s' % (i, 'x' * 60))
sys.stdout = _so
_size = os.path.getsize(_logp)
_txt = io.open(_logp, encoding='utf-8').read()
_lines = [x for x in _txt.split('\n') if x.strip()]
check('★★ 日志涨到上限就自动瘦身（不会无限涨）', _size < 20000 * 2,
      '%d 字节（上限 20000）' % _size)
check('★ 瘦身之后**最近的**记录还在（出问题要看的是最近这些）',
      '日志 599 ' in _txt)
check('★ 最老的被清掉了', '日志 0 ' not in _txt)
check('★★ 不会留半截行（从中间切要先丢掉半行）',
      all(x.startswith('[') or x.startswith('（') for x in _lines),
      repr([x for x in _lines if not x.startswith('[')][:1]))
check('★ 开头有说明，不是莫名其妙少了一截',
      _lines[0].startswith('（日志太大'), _lines[0][:24])
core.LOG_FILE, core.LOG_MAX_BYTES, core._log_writes = _save

print()
print('=' * 62)
print('★ 单笔汇率：+9000/9 努力（2026-10-08 用户报的）')
print('=' * 62)
# 用户实测：群汇率是 1，他发「+1000 努力」（算对 1000U），
# 又发「+9000/9 努力」——他想按 9 算（9000÷9=1000U），
# 结果机器人按群汇率 1 算成了 9000U，而且**一点提示都没有**。
# 根因：`/9` 被当成备注的一部分吞了 —— 这个写法从来没被支持过。

st = mk()
T(st, '+9000/9 努力')
e = es(st)
check('★★★ 金额和汇率都解析出来了（不再是 9000 按 1 算）',
      e and float(e[0].amount) == 9000 and float(e[0].rate) == 9,
      '%s / %s' % (e[0].amount, e[0].rate) if e else '没记上')
check('★★★ 算出来是 1000U（这就是用户要的那笔）',
      e and float(e[0].payable_usdt) == 1000, str(e[0].payable_usdt) if e else '-')
check('★ 备注只剩「努力」，`/9` 不再混在里面',
      e and e[0].note == '努力', e[0].note if e else '-')
check('★ 账单上显示的算式是 9000/9=1000U',
      e and '9000/9=1000U' in (T(st, '账单').text or '').replace(' ', ''),
      (T(st, '账单').text or '')[:70].replace('\n', ' '))

# ★★★ 最要紧的一条：只改这一笔，**不能动群汇率**
st = mk()
T(st, '+9000/9 努力')
rate_after, _ = st.get_settings(CHAT)
check('★★★ 群汇率**没被改**（还是 1，不是 9）',
      float(rate_after) == 1, str(rate_after))
T(st, '+200 张三')
# ★ entries() 是**由旧到新**排的，这里要的是第二笔
e = [x for x in es(st) if float(x.amount) == 200]
check('★★ 下一笔照样用群汇率（200U，不是 22.2U）',
      e and float(e[0].rate) == 1 and float(e[0].payable_usdt) == 200,
      ('%s→%s' % (e[0].rate, e[0].payable_usdt)) if e else '没记上')

# 不写汇率 = 跟以前完全一样
st = mk()
T(st, '+1000 努力')
e = es(st)
check('★ 不写汇率：用群汇率，行为跟以前一模一样',
      e and float(e[0].rate) == 1 and float(e[0].payable_usdt) == 1000,
      '%s→%s' % (e[0].amount, e[0].payable_usdt) if e else '-')

# 几种写法
CASES2 = [
    ('+9000/9努力', 9000, 9, '努力'),        # 汉字紧贴、没空格
    ('+1000/7.2 定金', 1000, '7.2', '定金'),  # 小数汇率
    ('+9000/9', 9000, 9, ''),               # 没备注
    ('入款 500/5 测试', 500, 5, '测试'),      # 别名也要认
    ('+9000/1 努力', 9000, 1, '努力'),        # 写 1 就是按 1 算
]
for text, amt, rate, note in CASES2:
    st = mk()
    T(st, text)
    e = es(st)
    ok = e and float(e[0].amount) == float(amt) and float(e[0].rate) == float(rate)
    check('%-16s → %s ÷ %s' % (text, amt, rate), bool(ok),
          ('%s/%s' % (e[0].amount, e[0].rate)) if e else '没记上')

# ★ 备注里合法的斜杠不能被误当成汇率
st = mk()
T(st, '+9000 定金1/2')
e = es(st)
check('★★ 备注里的斜杠（`定金1/2`）不会被当成汇率',
      e and float(e[0].rate) == 1 and e[0].note == '定金1/2',
      '%s / %s' % (e[0].rate, e[0].note) if e else '-')

# 写错的汇率要**报错**，不能默默按群汇率算
for bad in ('+9000/0 坏', '+9000/-3 坏'):
    st = mk()
    r = T(st, bad)
    check('★★ %s → 报错提醒（不是静默记账）' % bad,
          r is not None and '汇率' in (r.text or '') and not es(st),
          (r.text or '（无回应）')[:40])

# 下发不参与汇率换算（数字本来就是 U）
st = mk()
T(st, '下发 300')
e = es(st)
check('★ 下发还是原样（300U，不除汇率）',
      e and e[0].kind == 'payout' and float(e[0].net_amount) == 300,
      str(e[0].net_amount) if e else '-')

# 单笔汇率的**快照**：以后改群汇率，老账不能被改动
st = mk()
T(st, '+9000/9 努力')
e1 = es(st)[0]
T(st, '设置汇率 7')
e2 = es(st)[0]
check('★★ 改群汇率之后，老那笔的快照还是 9（不会被改写）',
      float(e2.rate) == 9 and float(e2.payable_usdt) == 1000,
      '%s / %s' % (e2.rate, e2.payable_usdt))
check('★ 新账用新群汇率（7）',
      float(st.get_settings(CHAT)[0]) == 7)

# ---- 「这就是一条记账」的判据（算式 vs 记账的界）----
# ★ `+1000/9` **算式和记账都合法**，算式那关排在前面 →
#   判据卡的是**整串**：多一个字符就还给算式
ENTRY_CASES = [
    ('+1000/9', True), ('+1000', True), ('-30', True),
    ('+1000/7.2', True), ('+0', True),
    ('1000/9', False),        # 没符号 = 纯算式
    ('+1000+200', False),     # 带运算符 = 算式
    ('+1000*2', False),
    ('1+1', False),
    ('+1000/9 努力', False),   # 带备注的本来就走不到算式那关
    ('+1000/0', True),        # ★ 写得不对也要归记账，好让记账那边报错
]
from runners.ledger import commands as _C
for text_, want in ENTRY_CASES:
    check('「%s」%s算记账' % (text_, '' if want else '不'),
          _C.is_entry_text(text_) is want)

cleanup()
print()
print('=' * 62)
print('结果：%d 项通过，%d 项失败' % (len(OK), len(BAD)))
for x in BAD:
    print('  ❌', x)
print('=' * 62)
sys.exit(1 if BAD else 0)
