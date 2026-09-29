# -*- coding: utf-8 -*-
"""登录记录的打码 + 谷歌验证码（走真 HTTP）

★ 最要命的一条：**没解锁的时候，打码的接口绝不能把真实 IP 漏出去**。
  「藏起来」要是只藏了显示、数据还在响应里，那等于没藏 ——
  打开浏览器 F12 就看见了。所以这里专门断言
  **整个响应体里搜不到真实 IP**。
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
TMP = os.path.join(HERE, '_tmp_totp')
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(os.path.join(TMP, 'data'), exist_ok=True)

import core                                          # noqa: E402
core.BASE_DIR = TMP
core.BOTS_FILE = os.path.join(TMP, 'bots.json')
core.DATA_DIR = os.path.join(TMP, 'data')
core.CONFIG_FILE = os.path.join(TMP, 'config.json')
json.dump({}, io.open(core.CONFIG_FILE, 'w', encoding='utf-8'))

from manager import BotManager                       # noqa: E402
from merchants import MerchantStore                  # noqa: E402
from panel import PanelHandler                       # noqa: E402
import login_log                                     # noqa: E402
import totp                                          # noqa: E402

OK, BAD = [], []
ADMIN_PW = 'ADMINPW123'
REAL_IP = '203.0.113.77'          # RFC 5737 测试用地址段，不会撞到真地址


def check(name, cond, extra=''):
    (OK if cond else BAD).append(name)
    print('  %s %s%s' % ('✅' if cond else '❌', name,
                         ('  → %s' % (extra,)) if extra else ''))


PanelHandler.mgr = BotManager({'pass': ADMIN_PW})
PanelHandler.password = ADMIN_PW
PanelHandler.merch = MerchantStore()
PanelHandler.token_secret = 'S' * 32
PanelHandler.page_cache = None
PanelHandler.totp_ok_until = 0

# 造一条登录记录
login_log.add(REAL_IP, '（管理员）', 'admin', True, ua='Mozilla/5.0 (Windows NT)')

httpd = ThreadingHTTPServer(('127.0.0.1', 0), PanelHandler)
BASE = 'http://127.0.0.1:%d' % httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()
time.sleep(0.3)

H = {'X-Panel-Pass': ADMIN_PW}


def get(p, h=None):
    return requests.get(BASE + p, headers=h or H, timeout=10)


def post(p, body, h=None):
    return requests.post(BASE + p, json=body, headers=h or H, timeout=10)


print('=' * 62)
print('① 默认：IP 是打码的，而且**响应里不能有真实 IP**')
print('=' * 62)
r = get('/api/logins')
check('接口通', r.status_code == 200, str(r.status_code))
d = r.json()
check('ok=True', d.get('ok') is True)
rows = d.get('logins') or []
check('有记录', len(rows) >= 1, '%d 条' % len(rows))
check('★★ 返回的是打码后的（203.0.*.*）',
      any(x.get('ip_masked') == '203.0.*.*' for x in rows),
      str([x.get('ip_masked') for x in rows][:3]))
check('★★ 响应里**没有** ip 字段了（不是藏了显示、数据还在）',
      all('ip' not in x for x in rows),
      str([k for k in (rows[0] if rows else {}) if 'ip' in k]))
check('★★★ 整个响应体里搜不到真实 IP（F12 也看不到）',
      REAL_IP not in r.text,
      '泄漏了！' if REAL_IP in r.text else '')
check('城市字段还在（不影响用来判断大概位置）', 'city' in (rows[0] if rows else {}))

print()
print('=' * 62)
print('② 还没绑定验证码时：不拦（不然用户把自己锁在外面）')
print('=' * 62)
r = get('/api/totp').json()
check('拿得到密钥', bool(r.get('secret')), str(r.get('secret'))[:8] + '…')
check('★ 密钥是 base32（验证器 App 认）',
      all(c in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ234567' for c in r['secret']))
check('★ 密钥够长（20 字节 = 32 位）', len(r['secret']) == 32, str(len(r['secret'])))
check('有 otpauth 链接（可选扫码）', r.get('url', '').startswith('otpauth://totp/'))
check('初始状态：未绑定', r.get('bound') is False)
SEC = r['secret']

r = post('/api/logins/reveal', {}).json()
check('★ 没绑定时看真实 IP 不拦', r.get('ok') is True, str(r)[:60])
check('★ 而且给的是真实 IP',
      any(x.get('ip') == REAL_IP for x in (r.get('logins') or [])))

print()
print('=' * 62)
print('③ 绑定（先让你输一次码，确认没绑错）')
print('=' * 62)
r = post('/api/totp/bind', {'code': '000000' if totp.code(SEC) != '000000'
                            else '111111'})
check('★ 错的码绑不上', r.status_code == 400 or not r.json().get('ok'),
      '%s %s' % (r.status_code, r.text[:50]))
r = post('/api/totp/bind', {'code': totp.code(SEC)}).json()
check('★★ 对的码绑定成功', r.get('ok') is True, str(r)[:60])
check('状态变成已绑定', get('/api/totp').json().get('bound') is True)

print()
print('=' * 62)
print('④ ★★ 绑定后：看真实 IP / 清空 都要验证码')
print('=' * 62)
PanelHandler.totp_ok_until = 0          # 清掉 5 分钟宽限，模拟"刚打开"
r = post('/api/logins/reveal', {})
check('★★ 不给码 → 403', r.status_code == 403, str(r.status_code))
check('★★ 而且提示要输码', r.json().get('need_code') is True, r.text[:60])
check('★★ 被拒时**也没漏真实 IP**', REAL_IP not in r.text)

r = post('/api/logins/reveal', {'code': '000000'})
check('★★ 错的码 → 403', r.status_code == 403, str(r.status_code))
check('★★ 错的码也没漏真实 IP', REAL_IP not in r.text)

r = post('/api/logins/reveal', {'code': totp.code(SEC)})
check('★★ 对的码 → 通过', r.status_code == 200 and r.json().get('ok'),
      str(r.status_code))
check('★★ 这时候才给真实 IP',
      any(x.get('ip') == REAL_IP for x in r.json().get('logins') or []))

print()
print('   —— 5 分钟宽限 ——')
PanelHandler.totp_ok_until = 0
post('/api/logins/reveal', {'code': totp.code(SEC)})
r = post('/api/logins/reveal', {})       # 不再给码
check('★ 验过之后短时间内不用再输（不然看 5 条要输 5 遍）',
      r.status_code == 200, str(r.status_code))
check('★ 打码接口也标了 unlocked',
      get('/api/logins').json().get('unlocked') is True)

print()
print('   —— 清空 ——')
PanelHandler.totp_ok_until = 0
before = len(login_log.recent(999))
r = post('/api/logins/clear', {})
check('★★ 不给码 → 403（清空是毁审计痕迹，也要码）',
      r.status_code == 403, str(r.status_code))
check('★★ 记录还在（没被清掉）', len(login_log.recent(999)) == before,
      '%d → %d' % (before, len(login_log.recent(999))))

r = post('/api/logins/clear', {'code': totp.code(SEC)}).json()
check('★★ 给对码 → 清空成功', r.get('ok') is True, str(r)[:60])
check('★ 真清掉了', len(login_log.recent(999)) == 0)

print()
print('=' * 62)
print('⑤ 换密钥（防止拿面板密码就能把验证码拆掉）')
print('=' * 62)
PanelHandler.totp_ok_until = 0
r = post('/api/totp/reset', {})
check('★★ 已绑定时不输码换不了密钥（不然 2FA 形同虚设）',
      r.status_code == 403, str(r.status_code))
r = post('/api/totp/reset', {'code': totp.code(SEC)}).json()
check('★ 给对码能换', r.get('ok') is True, str(r)[:60])
NEW = r.get('secret') or ''
check('★ 换了个不一样的新密钥', NEW and NEW != SEC,
      '一样的话就没换')
check('★ 换完之后回到「未绑定」状态',
      get('/api/totp').json().get('bound') is False)
check('★ 旧密钥算出来的码已经失效（换手机场景）',
      totp.verify(NEW, totp.code(SEC)) is False)

print()
print('=' * 62)
print('⑥ 商户碰不到这些东西（管理端专属）')
print('=' * 62)
mA, _ = PanelHandler.merch.add('张三', 'zhangsan')
PanelHandler.merch.set_pass(mA['mid'], 'zhangsan123', must_change=False)
tok = requests.post(BASE + '/api/login',
                    json={'user': 'zhangsan', 'pass': 'zhangsan123'},
                    timeout=10).json().get('token') or ''
HM = {'X-Panel-Token': tok}
check('商户登录成功', bool(tok))
for name, resp in (('读登录记录', get('/api/logins', HM)),
                   ('看真实 IP', post('/api/logins/reveal', {}, HM)),
                   ('清空记录', post('/api/logins/clear', {}, HM)),
                   ('看验证码密钥', get('/api/totp', HM)),
                   ('换密钥', post('/api/totp/reset', {}, HM))):
    check('★★ 商户「%s」→ 被挡' % name,
          resp.status_code != 200 or not resp.json().get('ok'),
          '%s %s' % (resp.status_code, resp.text[:40]))
check('★★ 商户的响应里也没有真实 IP', REAL_IP not in get('/api/logins', HM).text)

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
