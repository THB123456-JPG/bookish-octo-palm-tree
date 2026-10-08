# -*- coding: utf-8 -*-
"""商户（客户）账号

用户把机器人卖给客户之后，客户要自己进后台配自己的货源
（自己的上游 key、自己的收款地址、自己的定价），但**绝不能看到别人的**。

每个客户 = 一个商户账号；机器人靠 `bot['mid']` 归属到商户，
面板按这个字段做隔离（见 panel.py 的 _bot_of）。

★ 零新依赖：密码用标准库 PBKDF2，登录态用 HMAC 签名的无状态 token。
  （不要引 cryptography —— 那会让 exe 体积和打包风险都上一个台阶）
"""
import hashlib
import hmac
import os
import secrets
import threading
import time

import core

PBKDF2_ROUNDS = 120000
MIN_PASS_LEN = 6
TOKEN_TTL = 30 * 86400          # 登录 30 天有效
USER_RE = r'^[a-z0-9_]{3,20}$'


def merchants_file():
    """按项目惯例：每次算一遍路径，测试里改 core.BASE_DIR 就能隔离"""
    return os.path.join(core.BASE_DIR, 'merchants.json')


def safe_eq(a, b):
    """常数时间比较。

    ★ 必须先 encode —— HTTP 头是 latin-1 解出来的，攻击者塞个非 ASCII 字节
      会让 compare_digest 抛 TypeError，那就等于白送一个 500 让人探测。
    """
    try:
        return hmac.compare_digest(str(a).encode('utf-8'),
                                   str(b).encode('utf-8'))
    except Exception:
        return False


def hash_pw(pw, salt=None, it=PBKDF2_ROUNDS):
    """算密码哈希。返回 (salt_hex, hash_hex)"""
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac('sha256', str(pw or '').encode('utf-8'),
                             bytes.fromhex(salt), int(it))
    return salt, dk.hex()


def check_pw(pw, salt, want, it=PBKDF2_ROUNDS):
    try:
        _, got = hash_pw(pw, salt, it)
        return hmac.compare_digest(got, str(want or ''))
    except Exception:
        return False


def pass_ok(pw):
    """密码强度（够用就行，别太苛刻 —— 这是给普通客户用的）"""
    s = str(pw or '')
    if len(s) < MIN_PASS_LEN:
        return False, '密码至少 %d 位' % MIN_PASS_LEN
    if len(s) > 64:
        return False, '密码太长了'
    if s.isdigit() or s.isalpha():
        return False, '密码别只用纯数字或纯字母'
    return True, ''


# ================= 登录态（无状态签名 token）=================
def _sig(secret, body):
    return hmac.new(str(secret).encode('utf-8'), body.encode('utf-8'),
                    hashlib.sha256).hexdigest()[:32]


def make_token(m, secret, ttl=TOKEN_TTL):
    """token = mid.sess_ver.过期时间.签名

    无状态的好处：重启 exe 商户不用重新登录（内存表方案会全掉线）。
    吊销能力没丢 —— 改密码/重置会把 sess_ver +1，旧 token 当场失效。
    """
    body = '%s.%d.%d' % (m.get('mid'), int(m.get('sess_ver') or 1),
                         int(time.time()) + int(ttl))
    return body + '.' + _sig(secret, body)


def token_mid(tok, secret):
    """验签 + 查过期。返回 mid，无效返回 ''（不查 enabled，交给调用方）"""
    p = str(tok or '').split('.')
    if len(p) != 4:
        return ''
    if not safe_eq(p[3], _sig(secret, '.'.join(p[:3]))):
        return ''
    try:
        ver, exp = int(p[1]), int(p[2])
    except ValueError:
        return ''
    if exp < time.time():
        return ''
    return p[0], ver          # mid, sess_ver


class MerchantStore:
    """商户账号的增删改查

    merchants.json:
        {"merchants": [{mid, name, user, note, salt, hash, iter,
                        must_change, enabled, sess_ver, created, last_login}]}
    """

    def __init__(self, path=None):
        self.path = path or merchants_file()
        self.lock = threading.Lock()
        self.data = core.load_json(self.path, {})
        if not isinstance(self.data, dict):
            self.data = {}
        self.data.setdefault('merchants', [])

    # -------- 存取 --------
    def save(self):
        with self.lock:
            d = os.path.dirname(self.path)
            if d:
                os.makedirs(d, exist_ok=True)
            core.save_json(self.path, self.data)

    @property
    def merchants(self):
        return self.data.setdefault('merchants', [])

    def count(self):
        return len(self.merchants)

    def get(self, mid):
        for m in self.merchants:
            if m.get('mid') == mid:
                return m
        return None

    def by_user(self, user):
        u = str(user or '').strip().lower()
        if not u:
            return None
        for m in self.merchants:
            if str(m.get('user') or '').strip().lower() == u:
                return m
        return None

    # -------- 新建 / 删除 --------
    def add(self, name, user, note=''):
        """新建商户。返回 (商户 dict, 初始密码)。用户名重复抛 ValueError"""
        import re
        u = str(user or '').strip().lower()
        if not re.match(USER_RE, u):
            raise ValueError('用户名只能用小写字母/数字/下划线，3~20 位')
        if self.by_user(u):
            raise ValueError('用户名 %s 已经有人用了' % u)

        pw = core.gen_code(8)
        salt, h = hash_pw(pw)
        m = {
            'mid': self._new_mid(),
            'name': str(name or '').strip() or u,
            'user': u,
            'note': str(note or '').strip(),
            'salt': salt,
            'hash': h,
            'iter': PBKDF2_ROUNDS,
            'must_change': True,        # 首次登录必须改密码
            'enabled': True,
            'sess_ver': 1,              # 改密码时 +1，旧 token 全失效
            'created': time.strftime('%Y-%m-%d %H:%M'),
            'last_login': '',
        }
        self.merchants.append(m)
        self.save()
        core.log('新建商户 %s（%s）' % (m['name'], m['user']))
        return m, pw

    def _new_mid(self):
        for _ in range(20):
            mid = core.gen_id(8)
            if not self.get(mid):
                return mid
        raise RuntimeError('生成商户 ID 失败')

    def remove(self, mid):
        m = self.get(mid)
        if not m:
            return False
        self.merchants.remove(m)
        self.save()
        core.log('删除商户 %s（%s）' % (m.get('name'), m.get('user')))
        return True

    # -------- 登录 / 密码 --------
    def verify(self, user, pw):
        """密码对不对。对了返回商户 dict，错了或停用返回 None

        ★ 不管用户名存不存在，都要走一次哈希计算 —— 否则响应时间会
          泄露「这个用户名存不存在」。
        """
        m = self.by_user(user)
        fake = ('0' * 32, '0' * 64, PBKDF2_ROUNDS)
        salt, want, it = ((m.get('salt'), m.get('hash'),
                           m.get('iter') or PBKDF2_ROUNDS) if m else fake)
        ok = check_pw(pw, salt, want, it)
        if not (m and ok):
            return None
        if not m.get('enabled', True):
            return None
        m['last_login'] = time.strftime('%Y-%m-%d %H:%M')
        self.save()
        return m

    def set_pass(self, mid, pw, must_change=False):
        """改密码。★ 同时 sess_ver+1，把之前发出去的 token 全作废"""
        ok, why = pass_ok(pw)
        if not ok:
            return False, why
        m = self.get(mid)
        if not m:
            return False, '找不到这个商户'
        m['salt'], m['hash'] = hash_pw(pw)
        m['iter'] = PBKDF2_ROUNDS
        m['must_change'] = bool(must_change)
        m['sess_ver'] = int(m.get('sess_ver') or 1) + 1
        self.save()
        return True, ''

    def reset_pass(self, mid):
        """重置成随机密码。返回 (新密码, 错误说明)

        ★★ 生成的密码**必须先过 pass_ok 那一关**，否则 set_pass 会拒收，
          而这里以前照样把密码报给操作者 —— 商户拿到一个**用不了的密码**，
          表现就是「重置完登录说密码不对」。

          `gen_code` 的字符集是「26 个大写字母 + 10 个数字」，
          纯字母的概率 (26/36)^8 ≈ **7.7%** —— 大约每重置 13 次就中一次。
          很容易被当成「偶发」放过去，所以在这儿循环到合格为止。
        """
        m = self.get(mid)
        if not m:
            return '', '找不到这个商户'
        pw = ''
        for _ in range(20):
            pw = core.gen_code(8)
            if pass_ok(pw)[0]:
                break
        else:
            # 兜底：手工拼一个保证「有字母也有数字」的
            pw = 'Aa1' + core.gen_code(5)
        self.set_pass(mid, pw, must_change=True)     # 重置后也要求他改
        core.log('重置了商户 %s 的密码' % m.get('user'))
        return pw, ''

    def set_enabled(self, mid, on):
        m = self.get(mid)
        if not m:
            return False, '找不到这个商户'
        m['enabled'] = bool(on)
        self.save()
        core.log('%s商户 %s' % ('启用' if on else '停用', m.get('user')))
        return True, ''

    # -------- 登录态 --------
    def identify(self, token, secret):
        """token -> 商户 dict（无效 / 过期 / 停用 都返回 None）"""
        got = token_mid(token, secret)
        if not got:
            return None
        mid, ver = got
        m = self.get(mid)
        if not m or not m.get('enabled', True):
            return None
        if int(m.get('sess_ver') or 1) != int(ver):
            return None            # 改过密码，这个 token 作废了
        return m

    # -------- 给面板看的 --------
    def public(self, m, bot_count=0):
        """★ 绝不能带 salt / hash"""
        return {
            'mid': m.get('mid'),
            'name': m.get('name'),
            'user': m.get('user'),
            'note': m.get('note') or '',
            'enabled': m.get('enabled', True),
            'must_change': m.get('must_change', False),
            'created': m.get('created') or '',
            'last_login': m.get('last_login') or '',
            'bot_count': bot_count,
        }

    def snapshot(self, counts=None):
        counts = counts or {}
        return [self.public(m, counts.get(m.get('mid'), 0))
                for m in self.merchants]
