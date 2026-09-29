# -*- coding: utf-8 -*-
"""上游调用的重试规则测试 —— 只读能重试，下单绝不重试

★ 这是**钱**的边界，必须锁死：
  · 查余额/查笔数/查明细/查用户名 → 调一百遍也不扣钱，超时/429/5xx 尽管重试
  · getenergy / auto / premium   → 请求出去了就可能已经扣费，
                                    重试 = 再扣一次 = 真金白银的损失

之前的问题：所有接口都只有一次机会。上游偶尔抽一下（实测 25 秒读超时、
或者第二次直接 429），面板就显示「查不到余额」，自检还误报
「检查 API Key 对不对」，让人跑去反复改 Key。
"""
import os
import sys
import time

sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import core
from runners.shop.providers import ApitrxProvider, UncertainError

OK, BAD = [], []


def check(name, cond, extra=''):
    (OK if cond else BAD).append(name)
    print('  %s %s%s' % ('✅' if cond else '❌', name,
                         ('  → %s' % (extra,)) if extra else ''))


class FakeResp:
    def __init__(self, status=200, payload=None, text=''):
        self.status_code = status
        self._payload = payload
        self.text = text or (str(payload) if payload is not None else '')

    def json(self):
        if self._payload is None:
            raise ValueError('not json')
        return self._payload


class FakeSession:
    """按脚本回放：script 里每一项是 FakeResp 或一个异常实例"""
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def _next(self, url, **kw):
        self.calls.append(url)
        if not self.script:
            raise AssertionError('脚本用完了，但还在请求：%s' % url)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def get(self, url, **kw):
        return self._next(url, **kw)

    def post(self, url, **kw):
        return self._next(url, **kw)


def mk(script, premium_key=''):
    p = ApitrxProvider('K' * 20, premium_key)
    p._sess = FakeSession(script)
    return p


OK_BAL = FakeResp(200, {'code': 200, 'data': {'balance': 12.5}})
OK_ORDER = FakeResp(200, {'code': 200, 'data': {
    'orderId': 'ORDER1', 'amount': 1.1, 'balance': 10.0}})

print('=' * 62)
print('一、只读接口：超时会重试，最终成功')
print('=' * 62)
p = mk([TimeoutError('read timed out'), OK_BAL])
bal = p.balance()
check('★ 第一次超时后重试成功', bal == 12.5, str(bal))
check('确实打了两次', len(p._sess.calls) == 2, '%d 次' % len(p._sess.calls))

print()
print('=' * 62)
print('二、只读接口：429 限流会重试')
print('=' * 62)
p = mk([FakeResp(429, text='rate limited'), OK_BAL])
check('★ 429 之后重试成功', p.balance() == 12.5)

p = mk([FakeResp(502, text='bad gateway'), OK_BAL])
check('★ 502 之后重试成功', p.balance() == 12.5)

p = mk([FakeResp(200, None, text='<html>Cloudflare</html>'), OK_BAL])
check('★ 回了 HTML（不是 JSON）也会重试', p.balance() == 12.5)

print()
print('=' * 62)
print('三、只读接口：一直失败才放弃，而且不会无限打')
print('=' * 62)
p = mk([TimeoutError('t')] * 3)
check('三次都失败 → 返回 None（不抛）', p.balance() is None)
check('★ 只打 3 次，不会死循环', len(p._sess.calls) == 3,
      '%d 次' % len(p._sess.calls))

p = mk([FakeResp(403, text='forbidden')])
check('★ 403 不重试（Key/权限问题，重试没意义）', p.balance() is None)
check('403 只打 1 次', len(p._sess.calls) == 1, '%d 次' % len(p._sess.calls))

print()
print('=' * 62)
print('四、★★ 下单接口：一次失败立刻停，绝不重试')
print('=' * 62)
p = mk([TimeoutError('read timed out'), OK_ORDER])
try:
    p.order('TBKw17Fxy5kBbev8MMZ16yVKuQTQfsETta', 65000, 1)
    check('★ 下单超时必须抛 UncertainError（不是重试！）', False, '居然成功了')
except UncertainError as e:
    check('★ 下单超时 → UncertainError', True, str(e)[:46])
check('★★ 只打了 1 次（重试就是再扣一次钱）',
      len(p._sess.calls) == 1, '%d 次' % len(p._sess.calls))

p = mk([FakeResp(429, text='rate limited'), OK_ORDER])
try:
    p.order('TBKw17Fxy5kBbev8MMZ16yVKuQTQfsETta', 65000, 1)
    check('★ 下单撞 429 也不能重试', False, '居然成功了')
except UncertainError:
    check('★ 下单撞 429 → UncertainError', True)
check('★★ 下单撞 429 也只打 1 次', len(p._sess.calls) == 1,
      '%d 次' % len(p._sess.calls))

p = mk([TimeoutError('x'), OK_ORDER])
try:
    p.auto_buy('TBKw17Fxy5kBbev8MMZ16yVKuQTQfsETta', 10, 1)
    check('★ 买笔数超时不能重试', False, '居然成功了')
except UncertainError:
    check('★ 买笔数超时 → UncertainError', True)
check('★★ 买笔数只打 1 次', len(p._sess.calls) == 1, '%d 次' % len(p._sess.calls))

p = mk([TimeoutError('x'), OK_ORDER], premium_key='P' * 20)
try:
    p.premium_open('someone', 3)
    check('★ 开会员超时不能重试', False, '居然成功了')
except UncertainError:
    check('★ 开会员超时 → UncertainError', True)
check('★★ 开会员只打 1 次', len(p._sess.calls) == 1, '%d 次' % len(p._sess.calls))

print()
print('=' * 62)
print('五、只读的其它接口也享受重试')
print('=' * 62)
p = mk([TimeoutError('x'), FakeResp(200, {'code': 200, 'data': [
    {'orderId': '1', 'receiver': 'TXXX'}]})])
check('★ 查笔数列表会重试', len(p.auto_list('TBKw17Fxy5kBbev8MMZ16yVKuQTQfsETta')) == 1)

p = mk([TimeoutError('x'), FakeResp(200, {'code': 200, 'data': [
    {'amount': 1}]})])
check('★ 查消费明细会重试', len(p.auto_records('123')) == 1)

p = mk([TimeoutError('x'), FakeResp(200, {'code': 200, 'data': {
    'username': 'zhangsan', 'nickname': '昵称'}})])
d = p.premium_query('zhangsan')
check('★ 查用户名会重试', d.get('nickname') == '昵称', str(d))

print()
print('=' * 62)
print('六、自检要分清「网络抽了」和「Key 不对」')
print('=' * 62)
p = mk([TimeoutError('t')] * 3)
ok, msg = p.self_test()
check('网络问题 → 明确说不是 Key 的问题',
      ok is False and '不是 Key 的问题' in msg, msg[:70])

p = mk([FakeResp(200, {'code': 500, 'message': 'apikey错误，请检查'})])
ok, msg = p.self_test()
check('★ Key 真错了 → 才提示去查 Key',
      ok is False and 'API Key 可能不对' in msg, msg[:70])

p = mk([OK_BAL])
ok, msg = p.self_test()
check('正常时能报出余额', ok is True and '12.5' in msg, msg[:70])

print()
print('=' * 62)
print('七、重试次数是常数，不会随调用累积')
print('=' * 62)
p = mk([TimeoutError('a'), TimeoutError('b'), OK_BAL,
        TimeoutError('c'), OK_BAL])
check('第一次调用用掉 3 次机会', p.balance() == 12.5)
check('第二次调用重新有 3 次机会', p.balance() == 12.5)
check('总共打了 5 次', len(p._sess.calls) == 5, '%d 次' % len(p._sess.calls))

print()
print('=' * 62)
print('结果：%d 项通过，%d 项失败' % (len(OK), len(BAD)))
for x in BAD:
    print('  ❌', x)
print('=' * 62)
sys.exit(1 if BAD else 0)
