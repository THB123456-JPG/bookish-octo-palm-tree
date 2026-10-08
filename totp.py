# -*- coding: utf-8 -*-
"""谷歌验证码（TOTP，RFC 6238）—— **只用标准库**

为什么要自己写：项目要打包成 exe，**多一个依赖就多几 MB**。
TOTP 本身就是一个 HMAC-SHA1 + 取中间几位，标准库的
`hmac` / `hashlib` / `struct` / `base64` 全都够，没必要引第三方库
（`pyotp` 也是三十行的事）。

和 Google Authenticator / 微软 Authenticator / 1Password 都兼容 ——
它们认的就是「base32 密钥 + 30 秒一步 + 6 位数字」这套。

★ 密钥是**必填的安全凭据**：拿到它 + 知道算法，就能一直算出正确的码。
  所以它存在 config.json 里，跟面板密码一个级别，别外传。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import struct
import time

STEP = 30          # 一步 30 秒（所有验证器 App 的默认）
DIGITS = 6         # 6 位数字


def new_secret(nbytes: int = 20) -> str:
    """生成一个新的 base32 密钥

    ★ 20 字节 = 160 位，是 RFC 4226 推荐的长度，Google Authenticator 认。
      别用太短的 —— 短了能被暴力猜。
    ★ 末尾的 `=` 去掉：验证器 App 手动输入时不认那个，解码时我们自己补。
    """
    return base64.b32encode(os.urandom(nbytes)).decode('ascii').rstrip('=')


def _key(secret: str) -> bytes:
    s = ''.join(str(secret or '').split()).upper()
    # base32 解码要求长度是 8 的倍数，补 `=`
    return base64.b32decode(s + '=' * (-len(s) % 8))


def code(secret: str, when: float | None = None) -> str:
    """算某一时刻的 6 位验证码"""
    counter = int((time.time() if when is None else when) // STEP)
    mac = hmac.new(_key(secret), struct.pack('>Q', counter),
                   hashlib.sha1).digest()
    # 动态截断（RFC 4226 规定的取法）：最后 4 位决定从哪儿切
    off = mac[-1] & 0x0F
    val = struct.unpack('>I', mac[off:off + 4])[0] & 0x7FFFFFFF
    return str(val % (10 ** DIGITS)).zfill(DIGITS)


def verify(secret: str, user_code: str, window: int = 1) -> bool:
    """校验用户输入的验证码

    ★ window=1：**前后各容忍 1 个时间窗**（也就是 ±30 秒）。
      手机时间跟服务器差一点很正常，不容忍的话用户会莫名其妙「验证码不对」。
      ★ 别开到 2 以上 —— 那等于把有效期拉长到 2 分半，安全性明显下降。
    """
    if not secret:
        return False
    s = ''.join(str(user_code or '').split())      # 允许用户输入带空格
    if not s.isdigit() or len(s) != DIGITS:
        return False
    now = time.time()
    for i in range(-window, window + 1):
        # ★ 必须用 compare_digest：普通的 == 会因为比较耗时不同而泄漏信息
        if hmac.compare_digest(code(secret, now + i * STEP), s):
            return True
    return False


def otpauth_url(secret: str, label: str = 'TG面板',
                issuer: str = 'TG机器人面板') -> str:
    """给验证器 App 用的绑定链接（也可以直接手输密钥）

    otpauth://totp/<显示名>?secret=<密钥>&issuer=<发行方>
    """
    from urllib.parse import quote
    return ('otpauth://totp/%s?secret=%s&issuer=%s'
            % (quote(label), secret, quote(issuer)))


def mask_ip(ip: str) -> str:
    """把 IP 的**后两段**藏起来：1.2.3.4 → 1.2.*.*

    ★ IPv4：藏后两段（能看出是哪个地区/运营商，但认不出具体是谁）
      IPv6：只留前两组
      其它（空、私有地址标记等）：原样返回
    """
    s = str(ip or '').strip()
    if not s:
        return s
    if ':' in s:                       # IPv6
        parts = [p for p in s.split(':') if p]
        return ':'.join(parts[:2]) + ':****' if len(parts) >= 2 else '****'
    parts = s.split('.')
    if len(parts) == 4:
        return '%s.%s.*.*' % (parts[0], parts[1])
    return s
