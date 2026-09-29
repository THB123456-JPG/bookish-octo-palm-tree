# -*- coding: utf-8 -*-
"""每个机器人的定制文件（codes/<机器人id>.py）

★ 三条底线，必须锁死：
  1. **不建这个文件 = 什么都没发生** —— 行为跟以前一模一样，
     不能因为加了这个机制就让所有机器人都变个样
  2. **只影响那一个机器人** —— 给 A 写了定制，B、C 一点都不许变
  3. ★★ **写坏了绝不能把机器人搞挂** —— 语法错、import 时抛异常、
     钩子里抛异常，三种都得记日志然后当作没有这个文件，机器人照常收发

第 3 条是重点：这个文件是手写的，用户（或我）手滑一次，
不能让一个正在收钱的机器人停摆。
"""
import os
import shutil
import sys
import threading
import time

sys.stdout.reconfigure(encoding='utf-8')

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = os.path.join(HERE, '_tmp_botcode')
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(os.path.join(TMP, 'data'), exist_ok=True)
CODES = os.path.join(TMP, 'codes')
os.makedirs(CODES, exist_ok=True)

import core                                            # noqa: E402
core.BASE_DIR = TMP
core.BOTS_FILE = os.path.join(TMP, 'bots.json')
core.DATA_DIR = os.path.join(TMP, 'data')
core.CODES_DIR = CODES
core.LOG_FILE = os.path.join(TMP, '运行日志.txt')
# 日志写文件就够了，别刷屏（我们要检查日志内容，见下面第 3 节）
_LOGGED = []
core.log = lambda msg: _LOGGED.append(str(msg))

from runners.kefu import KefuRunner                    # noqa: E402

OK, BAD = [], []


def check(name, cond, extra=''):
    (OK if cond else BAD).append(name)
    print('  %s %s%s' % ('✅' if cond else '❌', name,
                         ('  → %s' % (extra,)) if extra else ''))


def write(name, body):
    with open(os.path.join(CODES, '%s.py' % name), 'w', encoding='utf-8') as f:
        f.write(body)


# ===================== 搭一个能真跑的机器人 =====================
class FakeMgr:
    def __init__(self):
        self.lock = threading.RLock()
        self.saved = 0

    def is_expired(self, bot):
        return False

    def save(self):
        self.saved += 1


class FakeAPI:
    """喂一批 update，第二次来拉的时候就让它停"""

    def __init__(self, updates):
        self.updates = list(updates)
        self.n = 0
        self.runner = None
        self.on_sent = None

    def call(self, method, **params):
        if method == 'getMe':
            return {'id': 1, 'username': 'testbot', 'first_name': 'T'}
        if method == 'getUpdates':
            self.n += 1
            if self.n == 1:
                return self.updates
            self.runner.stop_evt.set()
            return []
        return {}

    def call_file(self, method, field, filename, fileobj, **params):
        return {}


def mk(bid='botA'):
    bot = {'id': bid, 'token': 'fake:token', 'type': 'kefu', 'note': bid}
    r = KefuRunner(FakeMgr(), bot)
    return r


def run_once(r, updates):
    """真的把 _loop 跑一遍（在子线程里），返回它处理过的 update"""
    api = FakeAPI(updates)
    api.runner = r
    r.api = api
    handled = []
    r.handle = lambda u: handled.append(u)
    t = threading.Thread(target=r._loop, daemon=True)
    t.start()
    t.join(10)
    return handled


U = lambda i: {'update_id': i, 'message': {          # noqa: E731
    'message_id': i, 'date': int(time.time()),
    'chat': {'id': 5, 'type': 'private'},
    'from': {'id': 9, 'first_name': '客', 'is_bot': False},
    'text': '你好 %d' % i}}


print('=' * 62)
print('① 不建这个文件 → 什么都没发生')
print('=' * 62)
check('没有文件时 load_code 返回 None', core.load_code('nobody') is None)
r = mk('botA')
check('★ runner.code 是 None', r.code is None)
check('★ 没有钩子时 code_call 安全返回 None',
      r.code_call('on_message', r, U(1)) is None)
check('★★ 没文件也照常处理消息', len(run_once(mk('botA'), [U(1), U(2)])) == 2)

print()
print('=' * 62)
print('② 有文件 → 钩子能跑；★ 只影响那一个机器人')
print('=' * 62)
write('botA', '''
CALLS = []

def on_start(runner):
    CALLS.append(('start', runner.bid))

def on_message(runner, u):
    CALLS.append(('msg', u['message']['text']))
    if u['message']['text'] == '拦住我':
        return True

def on_tick(runner):
    CALLS.append(('tick', runner.bid))
''')
mod = core.load_code('botA')
check('拿得到模块', mod is not None)
check('★ 只有 A 有定制，B 没有',
      core.load_code('botA') is not None and core.load_code('botB') is None)
check('★ B 的 runner.code 还是 None（没被 A 带跑偏）', mk('botB').code is None)

rA = mk('botA')
check('A 的 runner.code 拿到了', rA.code is not None)
check('钩子被调用、返回值传得回来',
      rA.code_call('on_message', rA, U(1)) is None
      and rA.code.CALLS == [('msg', '你好 1')],
      str(getattr(rA.code, 'CALLS', None)))

print()
print('=' * 62)
print('③ ★★ on_message 返回 True 才拦，返回别的不能拦')
print('=' * 62)
write('botC', '''
def on_message(runner, u):
    t = u['message']['text']
    if t == '拦住我':
        return True
    if t == '返回真值但不是True':
        return 1          # 故意：真值但不是 True
    return None
''')
handled = run_once(mk('botC'), [U(1), U(2)])
check('★ 返回 None → 默认流程照走（2 条都到了 handle）',
      len(handled) == 2, '%d 条' % len(handled))

handled = run_once(mk('botC'), [
    {'update_id': 7, 'message': {'message_id': 7, 'date': int(time.time()),
                                 'chat': {'id': 5, 'type': 'private'},
                                 'from': {'id': 9, 'first_name': '客'},
                                 'text': '拦住我'}}])
check('★★ 返回 True → 拦住了，handle 没被调用', len(handled) == 0,
      '%d 条' % len(handled))

handled = run_once(mk('botC'), [
    {'update_id': 8, 'message': {'message_id': 8, 'date': int(time.time()),
                                 'chat': {'id': 5, 'type': 'private'},
                                 'from': {'id': 9, 'first_name': '客'},
                                 'text': '返回真值但不是True'}}])
check('★★ 返回 1（真值但不是 True）→ **不拦**，默认流程照走',
      len(handled) == 1,
      '这是故意的：随手 return 一个真值不该把消息全吞了')

print()
print('=' * 62)
print('④ ★★★ 写坏了不能把机器人搞挂')
print('=' * 62)

write('syn', 'def on_message(runner, u)\n    return True\n')     # 少个冒号
check('语法错 → load_code 返回 None（不抛异常）', core.load_code('syn') is None)
r = mk('syn')
check('★ 语法错的机器人 code 是 None', r.code is None)
check('★★ 语法错的机器人**照常处理消息**',
      len(run_once(mk('syn'), [U(1), U(2), U(3)])) == 3)

write('boom', 'raise RuntimeError("我在 import 的时候就炸了")\n')
check('import 时抛异常 → load_code 返回 None', core.load_code('boom') is None)
check('★★ import 炸了的机器人照常处理消息',
      len(run_once(mk('boom'), [U(1), U(2)])) == 2)

write('badhook', '''
def on_message(runner, u):
    raise ValueError("我在钩子里炸了")

def on_start(runner):
    raise ValueError("on_start 也炸了")

def on_tick(runner):
    raise ValueError("on_tick 也炸了")
''')
r = mk('badhook')
check('钩子会炸的机器人，文件本身能加载', r.code is not None)
check('★★ 钩子抛异常 → code_call 吞掉、返回 None（不往外抛）',
      r.code_call('on_message', r, U(1)) is None)
check('★★ on_start 抛异常也不往外抛', r.code_call('on_start', r) is None)
check('★★★ 钩子炸了的机器人**照常处理消息**',
      len(run_once(mk('badhook'), [U(1), U(2)])) == 2)

print()
print('=' * 62)
print('⑤ 出错要留痕（不然用户说「没生效」时无从查起）')
print('=' * 62)
core.load_code('syn')
core.load_code('boom')
_j = '\n'.join(_LOGGED)
check('★ 语法错的日志里有文件名', 'codes/syn.py' in _j)
check('★ 日志明说「已忽略、机器人照常跑」', '已忽略' in _j and '照常跑' in _j)
check('★ import 炸的也进了日志', 'codes/boom.py' in _j)

r = mk('badhook')
r.code_call('on_message', r, U(1))
check('★ 钩子炸了也进了日志（带方法名）',
      'on_message()' in '\n'.join(_LOGGED))

print()
print('=' * 62)
print('⑥ 钩子拿到的是 runner 本身（能发消息、能读配置）')
print('=' * 62)
write('ctx', '''
SEEN = {}

def on_message(runner, u):
    SEEN['bid'] = runner.bid
    SEEN['note'] = runner.note()
    SEEN['has_send'] = callable(runner.send)
    SEEN['type'] = runner.bot.get('type')
''')
r = mk('ctx')
r.code_call('on_message', r, U(1))
check('★ runner.bid 传对了', r.code.SEEN.get('bid') == 'ctx',
      str(r.code.SEEN))
check('★ 能拿到机器人配置', r.code.SEEN.get('note') == 'ctx')
check('★ 能调 runner.send（回消息）', r.code.SEEN.get('has_send') is True)
check('★ 能拿到类型', r.code.SEEN.get('type') == 'kefu')

print()
print('=' * 62)
print('⑦ 钩子没写全也没事（三个都是可选的）')
print('=' * 62)
write('only', 'def on_tick(runner):\n    pass\n')
r = mk('only')
check('只写 on_tick，文件能加载', r.code is not None)
check('★ 缺 on_message → 安全返回 None',
      r.code_call('on_message', r, U(1)) is None)
check('★ 缺 on_start → 安全返回 None', r.code_call('on_start', r) is None)
check('★ 没有的钩子名 → 也安全', r.code_call('不存在的钩子', r) is None)

shutil.rmtree(TMP, ignore_errors=True)

print()
print('=' * 62)
print('结果：%d 项通过，%d 项失败' % (len(OK), len(BAD)))
for b in BAD:
    print('  ❌', b)
print('=' * 62)
sys.exit(1 if BAD else 0)
