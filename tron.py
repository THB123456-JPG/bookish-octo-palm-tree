# -*- coding: utf-8 -*-
"""TRON / TRC20 通用工具（**共享库**，不属于任何一个机器人）

    · USDT 助手（runners/usdt/）—— 查余额、查转账、查汇率
    · 商城机器人（runners/shop/）—— 收 USDT 付款、核对到底到没到账

★★ 因为两个类型都在用，**改这里会同时影响两边**，动之前先想清楚。

为什么单独拆出来：这些代码原来塞在 usdt.py 里，
    结果「商城」要 import「USDT 助手」的文件才能收钱 ——
    动 USDT 助手就有把商城搞挂的风险（2026-09-29 拆出）。

TronGrid 免费额度按 IP 限流，_get() 里的退避重试是必需的，别删。
"""
import hashlib
import os
import re
import time
from datetime import datetime

import requests

from core import TgError, log


# TRON 地址：T 开头，base58，共 34 位
ADDR_RE = re.compile(r'^T[1-9A-HJ-NP-Za-km-z]{33}$')

TRONSCAN_TX = 'https://tronscan.org/#/transaction/%s'

# 汇率数据源：欧易(OKX)场外商家的实时挂单价
OKX_C2C = 'https://www.okx.com/v3/c2c/tradingOrders/books'
RATE_TTL = 60          # 汇率缓存秒数（太频繁会被限流）


def is_address(s):
    return bool(ADDR_RE.match((s or '').strip()))


# ================= TRON 地址编解码 =================
# TronGrid 返回的原生交易里，地址是 16 进制（41 开头 21 字节），
# 而我们展示和调上游 API 都要 base58。两个方向都要能转。
_B58 = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'


def b58_encode(raw):
    n = int.from_bytes(raw, 'big')
    out = ''
    while n > 0:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    pad = 0
    for byte in raw:
        if byte == 0:
            pad += 1
        else:
            break
    return '1' * pad + out


def b58_decode(s):
    n = 0
    for ch in s:
        n = n * 58 + _B58.index(ch)
    raw = n.to_bytes((n.bit_length() + 7) // 8, 'big') if n else b''
    pad = len(s) - len(s.lstrip('1'))
    return b'\x00' * pad + raw


def addr_to_hex(addr):
    """base58 地址 → 41 开头的 21 字节 hex（TronGrid 原生交易用的格式）"""
    return b58_decode((addr or '').strip())[:21].hex()


def hex_to_addr(h):
    """41 开头的 hex → base58 地址（带 4 字节校验和）"""
    raw = bytes.fromhex((h or '').strip())
    if len(raw) != 21:
        return ''
    check = hashlib.sha256(hashlib.sha256(raw).digest()).digest()[:4]
    return b58_encode(raw + check)


def short(addr, head=6, tail=6):
    addr = addr or ''
    if len(addr) <= head + tail + 3:
        return addr
    return '%s...%s' % (addr[:head], addr[-tail:])


def fmt_time(ms):
    try:
        return datetime.fromtimestamp(int(ms) / 1000).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        return '未知'


def fmt_amount(v):
    """金额去掉多余的小数点，但保留足够精度"""
    s = '%.6f' % v
    s = s.rstrip('0').rstrip('.')
    return s if s else '0'


# ================= 链上数据 =================
class Tron:
    BASE = 'https://api.trongrid.io'
    USDT = 'TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t'   # 官方 USDT-TRC20 合约

    def __init__(self):
        self.session = requests.Session()
        self.session.headers['User-Agent'] = 'Mozilla/5.0'
        # ★ 可选：TronGrid 的免费 API Key（环境变量 TRON_API_KEY）
        #
        #   不填也能用，但免费额度**按 IP 限流**，实测很紧：
        #   背靠背第 2 个请求就 429、要等约 6 秒才恢复。
        #   填了之后额度高很多，查询基本不用等。
        #
        #   申请（免费）：https://www.trongrid.io/
        #   服务器上用的话在 systemd 里加一行：
        #       Environment=TRON_API_KEY=你的key
        key = (os.environ.get('TRON_API_KEY') or '').strip()
        if key:
            self.session.headers['TRON-PRO-API-KEY'] = key

    def _get(self, url, params=None, tries=3, timeout=20):
        """带退避重试的 GET

        ★★ 为什么必须重试：TronGrid 的免费额度是**按 IP 限流**的。
           实测（`_probe_429.py`）：
             · 背靠背连发 —— **第 2 个请求就 429**，第 3、4 个也是；
             · **等 6 秒左右就恢复**，之后再发又是 200；
             · 隔 2 秒发一次的话，6/6 全成功。
           典型场景就是「查一次余额，紧接着查转账」—— 必中。

           不重试的话，用户查一次就显示「取不到」，看着像功能坏了；
           服务器上尤其明显（机房 IP 是很多人共用的）。

        ★★ 退避必须 ≥3 秒。一开始写 1.5s/3s，实测**还是过不去** ——
           因为恢复窗口就是 6 秒左右，退避比它短等于白等。
           现在 3s、6s。

        ★ 只在 **429/403（限流）** 和网络抖动时重试；
          400/404 这种重试也没用，直接抛出去。
        """
        last = None
        for i in range(tries):
            if i:
                time.sleep(min(3 * i, 8))    # 3s、6s（实测要 ~6s 才恢复）
            try:
                r = self.session.get(url, params=params, timeout=timeout)
            except requests.RequestException as e:
                last = '网络错误：%s' % e
                continue
            if r.status_code == 200:
                return r
            if r.status_code in (429, 403):
                last = '查询太频繁（HTTP %s）' % r.status_code
                log('[USDT] TronGrid 限流，第 %d 次重试…' % (i + 1))
                continue
            raise TgError('查询失败（HTTP %s）' % r.status_code)
        raise TgError(last or '查询失败')

    def balance(self, address):
        """返回 (USDT余额, TRX余额)，取不到就返回 None"""
        url = '%s/v1/accounts/%s' % (self.BASE, address)
        r = self._get(url, timeout=20)

        d = r.json() or {}
        rows = d.get('data') or []
        if not rows:
            return 0.0, 0.0          # 地址还没激活过

        row = rows[0]
        usdt = 0.0
        for item in row.get('trc20') or []:
            if self.USDT in item:
                try:
                    usdt = int(item[self.USDT]) / 1e6
                except (TypeError, ValueError):
                    usdt = 0.0
                break
        try:
            trx = int(row.get('balance') or 0) / 1e6
        except (TypeError, ValueError):
            trx = 0.0
        return usdt, trx

    def trx_transfers(self, address, limit=30, min_ts=None):
        """取某个地址的【原生 TRX】转入记录。

        返回 [{'txid','from','amount','ts'}]，amount 单位是 TRX（不是 SUN）。
        只保留 to == address 的 TransferContract（真正的 TRX 转账），
        过滤掉 TRC10 转账、能量委托、合约调用那些。
        """
        addr_hex = addr_to_hex(address)
        if not addr_hex:
            raise TgError('地址不对，转不出 hex')
        url = '%s/v1/accounts/%s/transactions' % (self.BASE, address)
        params = {
            'limit': min(int(limit), 200),
            'only_to': 'true',          # 只看转入，能过滤掉一大半
            'only_confirmed': 'true',
        }
        if min_ts:
            params['min_timestamp'] = int(min_ts)
        # ★ 走带重试的 _get：TronGrid 按 IP 限流，偶发 429/403（详见 _get 注释）
        r = self._get(url, params=params, timeout=25)
        try:
            d = r.json()
        except ValueError:
            raise TgError('返回数据异常')

        out = []
        for t in d.get('data') or []:
            try:
                c = (t.get('raw_data') or {}).get('contract') or [{}]
                c = c[0]
                if c.get('type') != 'TransferContract':   # 只要原生 TRX 转账
                    continue
                v = (c.get('parameter') or {}).get('value') or {}
                if v.get('to_address') != addr_hex:
                    continue
                amount = int(v.get('amount') or 0) / 1e6   # SUN → TRX
                src = hex_to_addr(v.get('owner_address') or '')
                if not src:
                    continue
                out.append({
                    'txid': t.get('txID') or '',
                    'from': src,
                    'amount': amount,
                    'ts': int(t.get('block_timestamp') or 0),
                })
            except Exception:
                continue
        return out

    def transfers(self, address, limit=20, min_ts=None):
        """取某个地址的 USDT 转账（只看 Transfer，不含授权等操作）"""
        url = '%s/v1/accounts/%s/transactions/trc20' % (self.BASE, address)
        params = {
            'limit': min(int(limit), 200),
            'only_confirmed': 'true',
            'contract_address': self.USDT,
        }
        if min_ts:
            params['min_timestamp'] = int(min_ts)

        # ★ 走带重试的 _get：TronGrid 按 IP 限流，偶发 429/403（详见 _get 注释）
        r = self._get(url, params=params, timeout=25)

        try:
            d = r.json()
        except ValueError:
            raise TgError('返回数据异常')

        if d.get('success') is False:
            raise TgError(d.get('error') or '查询失败')

        out = []
        for t in d.get('data') or []:
            # 只保留 USDT 的普通转账，过滤掉 Approve / TransferFrom 之类的
            if t.get('type') != 'Transfer':
                continue
            info = t.get('token_info') or {}
            if info.get('address') != self.USDT:
                continue
            try:
                dec = int(info.get('decimals') or 6)
                amt = int(t.get('value') or 0) / (10.0 ** dec)
            except Exception:
                continue
            out.append({
                'txid': t.get('transaction_id') or '',
                'from': t.get('from') or '',
                'to': t.get('to') or '',
                'amount': amt,
                'ts': int(t.get('block_timestamp') or 0),
            })
        return out


# ================= 汇率（欧易 OKX 场外商家实时挂单价） =================
class Rate:
    """买 U 的实时价格（商家卖 U 的挂单），来自欧易(OKX)场外。

    side=sell → 商家在卖 U，这个价格就是「你买入 U」要付的钱。
    列表按价格从低到高，就是买 U 最划算的 TOP N。
    """

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                          'AppleWebKit/537.36 (KHTML, like Gecko) '
                          'Chrome/126.0 Safari/537.36',
            'Referer': 'https://www.okx.com/p2p-markets/cny/buy-usdt',
        })
        self._cache = None
        self._cache_at = 0.0

    def top(self, n=10):
        """返回买U最便宜的 n 个挂单：[{price, name, amount}, ...]"""
        r = self.session.get(OKX_C2C, timeout=20, params={
            't': int(time.time() * 1000),      # 带时间戳避免拿到缓存
            'quoteCurrency': 'cny',
            'baseCurrency': 'usdt',
            'side': 'sell',                    # 商家在卖 U = 你买入
            'paymentMethod': 'all',
            'userType': 'all',
            'receivingAds': 'false',
            'showTrade': 'false',
            'showFollow': 'false',
            'showAlreadyTraded': 'false',
            'isAbleFilter': 'false',
        })
        if r.status_code != 200:
            raise TgError('欧易接口返回 HTTP %s' % r.status_code)
        d = r.json() or {}
        if d.get('code') not in (0, '0'):
            raise TgError(d.get('msg') or '欧易接口返回异常')
        ads = (d.get('data') or {}).get('sell') or []
        if not ads:
            raise TgError('欧易没有返回挂单')

        out = []
        for a in ads:
            try:
                price = float(a.get('price'))
            except (TypeError, ValueError):
                continue
            try:
                amt = float(a.get('availableAmount') or 0)
            except (TypeError, ValueError):
                amt = 0.0
            out.append({
                'price': price,
                'name': (a.get('nickName') or '').strip() or '匿名商家',
                'amount': amt,
            })
            if len(out) >= n:
                break
        if not out:
            raise TgError('欧易返回的价格解析不了')
        return out

    def get(self, force=False):
        """带缓存（欧易限流，不用每次点都请求）"""
        now = time.time()
        if not force and self._cache and now - self._cache_at < RATE_TTL:
            return self._cache
        try:
            rows = self.top(10)
        except Exception as e:
            if self._cache:          # 取不到就先用上次的
                return self._cache
            return {'rows': [], 'at': now, 'err': str(e)}
        out = {'rows': rows, 'at': now, 'err': None}
        self._cache = out
        self._cache_at = now
        return out


# ================= TRX 现货价（会员报价换算用） =================
_TRX_CACHE = {'at': 0.0, 'price': 0.0}
TRX_FALLBACK = 0.32        # 拿不到实时价时的兜底：1 TRX ≈ ? USDT


def trx_price(force=False):
    """1 TRX 值多少 USDT（欧易现货）。拿不到就返回兜底价，绝不抛异常。

    会员是按 USDT 定价的（上游 gift 域名），客户付的却是 TRX，
    所以每次报价都得按实时汇率折算 —— 折错了就是自己亏。
    """
    now = time.time()
    if not force and _TRX_CACHE['price'] and now - _TRX_CACHE['at'] < 120:
        return _TRX_CACHE['price']
    try:
        r = requests.get('https://www.okx.com/api/v5/market/ticker',
                         params={'instId': 'TRX-USDT'},
                         headers={'User-Agent': 'Mozilla/5.0',
                                  'Referer': 'https://www.okx.com/'},
                         timeout=15)
        d = (r.json().get('data') or [{}])[0]
        p = float(d.get('last') or 0)
        if p > 0:
            _TRX_CACHE['price'] = p
            _TRX_CACHE['at'] = now
            return p
    except Exception as e:
        log('取 TRX 价格失败：%s' % e)
    return _TRX_CACHE['price'] or TRX_FALLBACK


def usdt_to_trx(usdt):
    """多少 USDT 折成多少 TRX"""
    p = trx_price()
    if p <= 0:
        return 0.0
    return round(float(usdt) / p, 6)


# ================= 机器人 =================
