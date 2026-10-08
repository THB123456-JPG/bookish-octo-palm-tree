# -*- coding: utf-8 -*-
"""TRON 主网查询（TronGrid v1）—— 余额 + 转账记录

★ 数据源跟服务器上那套**完全一致**（TronGrid 的 `/v1/accounts/...`），
  所以面板上看到的数字跟 tronscan、跟你服务器上那套都对得上。

★ 为什么不用仓库里现成的 `tron.py` 那个客户端：
  它是**拉交易再解析 log** 的路子，一次只能拿一样；
  而 TronGrid 的**账户接口一次就给全**：TRX 余额 + 所有 TRC20 余额
  + 创建时间 + 最近操作 —— 卡片上那三行就是它给的。
  `tron.py` 继续给 USDT 助手/商城用，两边互不影响。

★★ 两条铁律：
  1. **金额一律用整数「微单位」算**（1 USDT = 1e6，1 TRX = 1e6 SUN），
     别转 float —— 链上金额动辄几万 U，float 会掉精度，而这是钱的事。
  2. **查不到就说查不到**，绝不返回 0 冒充余额。
     卡片上写的「余额暂不可用，不代表余额为零」就是这个意思 ——
     客户看见 0 会当成钱没了，那是要出事的。
"""
import hashlib
import re
import time
from decimal import Decimal

import requests

TRONGRID = 'https://api.trongrid.io/v1/accounts/'
# 官方 USDT-TRC20 合约（Tether 发行的那一个）
USDT = 'TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t'
# ★ (连接超时, 读取超时)：连不上要**快速失败**，别让用户在那儿干等；
#   读可以宽松点 —— 链上接口偶尔慢，但慢也得有个头。
TIMEOUT = (5, 15)
PAGE_LIMIT = 200
MAX_PAGES = 100          # 一页 200 条，100 页 = 2 万条，正常绝对够

# ★★ 限流退避：**必须是 3 秒起**，别写 1.5s
#    实测（2026-09-28 在服务器上量的）：TronGrid 被限流后**约 6 秒才恢复**，
#    背靠背连发第 2 个请求就 429；退避比恢复窗口短 = 白等，第二次照样 429。
RETRY_WAIT = {'rate': (3.0, 6.0),      # 限流：两轮退避
              'timeout': (1.5,),       # 超时：再给一次机会
              'net': (1.5,),           # 连不上：同上
              'server': (1.5,)}        # 对面 5xx：同上


class ChainError(Exception):
    """链上查询失败。`kind` 说明是**哪一类**失败。

    ★★ 为什么要分类：原来所有失败都长一个样，用户看到的就是
      「查询失败，请稍后再试」。可实际上：
        · 网络抖了 / 限流  → 等一下再试**有用**
        · Key 无效 / 没权限 → 试一万次也没用，得去换 Key
        · 账户未激活        → 得让客户先去激活，重试是浪费时间
      不分类的话，用户只能反复点，既焦虑又白刷额度。

    kind 取值：'rate'(限流) 'auth'(Key无效/没权限) 'timeout'(超时)
              'net'(连不上) 'server'(对面故障) 'bad'(数据看不懂)
    """

    def __init__(self, kind, message):
        super().__init__(message)
        self.kind = kind

ADDR_RE = re.compile(r'^T[1-9A-HJ-NP-Za-km-z]{33}$')
ALPHABET = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'


# ================= 地址 =================
def address_valid(value):
    """TRON 地址校验。★ **带校验和**，不是只看长相 ——
    少打一个字符、写错一位都能当场查出来，不然会拿个错地址去查，
    查回来「未激活」用户还以为钱没了。"""
    value = (value or '').strip()
    if not ADDR_RE.match(value):
        return False
    number = 0
    for char in value:
        number = number * 58 + ALPHABET.index(char)
    raw = number.to_bytes(25, 'big')
    if raw[0] != 65:
        return False
    check = hashlib.sha256(hashlib.sha256(raw[:-4]).digest()).digest()[:4]
    return check == raw[-4:]


def base58(hex_address):
    """41 开头的 21 字节 hex → base58 地址（带校验和）"""
    raw = bytes.fromhex(hex_address or '')
    if len(raw) != 21 or raw[0] != 65:
        raise ValueError('地址格式不对')
    raw += hashlib.sha256(hashlib.sha256(raw).digest()).digest()[:4]
    number = int.from_bytes(raw, 'big')
    out = ''
    while number:
        number, rem = divmod(number, 58)
        out = ALPHABET[rem] + out
    for byte in raw[:-4]:
        if byte:
            break
        out = '1' + out
    return out


# ================= 金额 =================
def amount(value):
    """微单位整数 → 人看的字符串。1088000000 → '1088'；42536594906 → '42536.594906'"""
    try:
        value = int(value)
    except (TypeError, ValueError):
        return '0'
    if value % 1000000:
        return format(Decimal(value) / 1000000, 'f').rstrip('0').rstrip('.')
    return str(value // 1000000)


def units(text):
    """人输入的金额 → 微单位整数。0.5 → 500000。非法抛 ValueError"""
    try:
        number = Decimal(str(text).strip())
        if not number.is_finite() or number < 0 or number > Decimal('1000000'):
            raise ValueError
        raw = number * 1000000
        if raw != raw.to_integral_value():
            raise ValueError
        return int(raw)
    except Exception:
        raise ValueError('金额要大于等于 0、最多 6 位小数、不超过 100 万。') from None


def when(ms):
    """毫秒时间戳 → 北京时间字符串"""
    from datetime import datetime, timedelta, timezone
    try:
        if type(ms) is not int or ms <= 0:
            return '暂无数据'
        return datetime.fromtimestamp(ms / 1000,
                                      timezone(timedelta(hours=8))
                                      ).strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError, OverflowError, OSError):
        return '暂无数据'


# ================= HTTP =================
def _headers(api_key=''):
    return {'TRON-PRO-API-KEY': api_key} if api_key else {}


def probe(api_key=''):
    """★ 拿一个 Key 去**真查一次**，看它到底能不能用。返回 (能用的布尔值, 中文说明)。

    验证要**两个接口都过**：账户接口 + 交易记录接口。
    ★ 为什么两个都要：这两个在 TronGrid 上是分开计费/授权的，
      只测一个的话，可能存进去以后发现另一种查询用不了。

    ★ 只用**一个公开地址**（USDT 官方合约的发行账户里随便挑一个活跃地址即可）
      —— 这里用波场最出名的那个「黑洞」转账地址，它一定有记录。
    """
    # ★ 用 USDT 官方合约那个账户：它一定存在、一定有交易。
    #   ★ 保留重试：被限流（429）就判定「Key 不能用」是**误伤** ——
    #     限流是暂时的，退避一下就能过。Key 本身错（401）不会重试，直接判死。
    addr = 'TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t'
    try:
        chain_get(addr, {'only_confirmed': 'true'}, api_key)
    except ChainError as e:
        return False, _why(e)
    except Exception as e:                       # noqa: BLE001
        return False, '账户查询失败：%s' % e
    try:
        recent(addr, 'USDT', 1, api_key)
    except ChainError as e:
        return False, _why(e)
    except Exception as e:                       # noqa: BLE001
        return False, '交易查询失败：%s' % e
    return True, '账户查询和交易查询都通了'


def _why(e):
    """把失败翻译成人话（按类型给不同的话）"""
    kind = getattr(e, 'kind', '')
    return {
        'rate': '查询接口限流（429）—— 请求太密了',
        'auth': 'API Key 无效或没有权限（401/403）',
        'timeout': '接口响应超时（网络慢）',
        'net': '连不上查询接口（网络不通）',
        'server': '查询服务自己故障了（5xx）',
        'bad': '返回的数据看不懂',
    }.get(kind) or str(e)


def _get(url, params, api_key, retry=True):
    """发一次 GET，失败**按类型**决定要不要退避重试。返回解析好的 body。

    ★ 只重试「重试有用」的那几类（限流 / 超时 / 连不上 / 对面 5xx）。
      Key 错了、参数错了 —— 重试一万次也还是错，直接抛，别浪费时间。
    ★ 这几个请求全是**只读**的（查余额、查记录），重试不会造成任何副作用。
    """
    last = None
    kind = ''
    attempt = 0
    waits = ()
    while True:
        if attempt:
            time.sleep(waits[attempt - 1])
        try:
            r = requests.get(url, params=params or {},
                             headers=_headers(api_key), timeout=TIMEOUT)
        except requests.exceptions.Timeout:
            kind, last = 'timeout', ChainError('timeout', 'TRX/USDT 查询接口响应超时')
        except requests.exceptions.RequestException as e:
            kind = 'net'
            last = ChainError('net', '连不上 TRX/USDT 查询接口（%s）' % e.__class__.__name__)
        else:
            if r.status_code in (401, 403):
                # ★ Key 的问题：重试没用，直接抛，让用户去换 Key
                raise ChainError('auth', 'API Key 无效或没有权限（HTTP %d）'
                                         % r.status_code)
            if r.status_code == 429:
                kind, last = 'rate', ChainError('rate', '查询接口限流了（429）')
            elif r.status_code >= 500:
                kind = 'server'
                last = ChainError('server', '查询接口故障（HTTP %d）' % r.status_code)
            elif r.status_code != 200:
                raise ChainError('bad', '查询接口返回 HTTP %d' % r.status_code)
            else:
                try:
                    body = r.json()
                except ValueError:
                    raise ChainError('bad', '查询接口返回的不是 JSON') from None
                if (not isinstance(body, dict) or body.get('success') is not True
                        or not isinstance(body.get('data'), list)):
                    raise ChainError('bad', '查询接口返回异常，不代表余额为零。')
                return body
        # 走到这儿说明是「可重试」的失败
        waits = list(RETRY_WAIT.get(kind, ())) if retry else []
        if attempt >= len(waits):
            raise last
        attempt += 1


def chain_get(path, params=None, api_key='', retry=True):
    """读 TronGrid 的账户数据。★ 返回**列表**，任何异常都抛出去

    ★★ 绝不把「查询失败」悄悄变成空列表 —— 空列表在调用方眼里就是
      「这个地址没数据」，而实际上可能只是网络抖了一下。
      钱的事，宁可报错也不能让人误以为余额是 0。
    """
    return _get(TRONGRID + path, params or {}, api_key, retry)['data']


def balances(rows, address):
    """账户接口返回 → (「TRX：x / USDT：y」两行, 原始 row)

    ★ 会核对返回的确实是**这个地址**（接口偶尔会返回别的），对不上就报错，
      不然会把别人的余额显示成你的。
    """
    if not rows:
        return '未查询到已激活账户；请核对地址。'
    if len(rows) != 1 or not isinstance(rows[0], dict):
        raise ValueError('账户数据格式异常')
    row = rows[0]
    identity = row.get('address', '')
    if identity != address:
        # 接口给的是 41 开头的 hex，转成 base58 再比
        if (not isinstance(identity, str) or len(identity) != 42
                or base58(identity) != address):
            raise ValueError('查回来的账户对不上，不能显示')
    values = {'TRX': row.get('balance', 0), 'USDT': 0}
    tokens = row.get('trc20', [])
    if not isinstance(tokens, list) or any(not isinstance(t, dict) for t in tokens):
        raise ValueError('代币余额格式异常')
    for token in tokens:
        if USDT in token:
            values['USDT'] = token[USDT]
    for value in values.values():
        if isinstance(value, bool) or not str(value).isdigit():
            raise ValueError('余额格式异常')
    return '\n'.join('%s：%s' % (coin, amount(int(value)))
                     for coin, value in values.items()), row


def normalize(row, coin, address):
    """TronGrid 的一条记录 → 统一的转账 dict；不是有效转账就返回 None

    ★ 校验很严（跟服务器上那套一样）：认合约地址、认精度、认方向，
      认不准的一律不算 —— 宁可漏一条，也不能把不相干的交易算成你的账。
    """
    try:
        if coin == 'USDT':
            if row.get('type') != 'Transfer':
                return None
            if (row.get('token_info') or {}).get('address') != USDT:
                return None
            if int((row.get('token_info') or {})['decimals']) != 6:
                return None
            sender, receiver = row['from'], row['to']
            value, txid = int(row['value']), row['transaction_id']
        else:
            if not row.get('ret') or any(item.get('contractRet') != 'SUCCESS'
                                         for item in row['ret']):
                return None
            contracts = (row.get('raw_data') or {}).get('contract') or []
            if len(contracts) != 1 or contracts[0].get('type') != 'TransferContract':
                return None
            data = contracts[0]['parameter']['value']
            sender = base58(data['owner_address'])
            receiver = base58(data['to_address'])
            value, txid = int(data['amount']), row['txID']
        if address not in (sender, receiver) or sender == receiver or value <= 0:
            return None
        return {'id': coin + ':' + txid, 'hash': txid, 'coin': coin,
                'from': sender, 'to': receiver, 'amount': value,
                'timestamp': int(row['block_timestamp'])}
    except (ValueError, KeyError, TypeError, OverflowError):
        return None


def recent(address, coin, limit=20, api_key='', retry=True):
    """**最近 N 笔**转账（倒序，一页就够）—— 卡片上显示用的。

    ★ 跟 `transfers()` 是两条路，别混：
      · 这个   倒序 + 限量 → 只想看「最近发生了什么」，一次请求就回来
      · 那个   升序 + 游标 → 扫链用，要一条不漏地从上次的位置往后追
      用错的话要么卡片慢得离谱（升序翻几百页），要么扫链漏记录。
    """
    path = address + '/transactions' + ('/trc20' if coin == 'USDT' else '')
    params = {'only_confirmed': 'true', 'limit': max(1, min(int(limit), 50)),
              'order_by': 'block_timestamp,desc', 'only_transfers': 'true'}
    if coin == 'USDT':
        params['contract_address'] = USDT
    body = _get(TRONGRID + path, params, api_key, retry)
    out = [tx for tx in (normalize(item, coin, address) for item in body['data'])
           if tx]
    out.sort(key=lambda t: t['timestamp'], reverse=True)
    return out


def transfers(address, coin, since, api_key=''):
    """拉 since（毫秒）之后的转账。返回 (记录列表, 处理到哪个时间戳)

    ★ 升序翻页（`order_by=block_timestamp,asc`）+ fingerprint 游标：
      中途断了也不会漏 —— 已经处理到哪儿就存哪儿。
    """
    path = address + '/transactions' + ('/trc20' if coin == 'USDT' else '')
    until = int(time.time() * 1000)
    params = {'only_confirmed': 'true', 'limit': PAGE_LIMIT,
              'order_by': 'block_timestamp,asc',
              'min_timestamp': int(since), 'max_timestamp': until}
    if coin == 'USDT':
        params['contract_address'] = USDT
    records = []
    for _ in range(MAX_PAGES):
        body = _get(TRONGRID + path, params, api_key)
        for item in body['data']:
            tx = normalize(item, coin, address)
            if tx and int(since) <= tx['timestamp'] <= until:
                records.append(tx)
        fingerprint = (body.get('meta') or {}).get('fingerprint')
        if not fingerprint:
            return records, max([tx['timestamp'] for tx in records]
                                or [int(since)])
        if fingerprint == params.get('fingerprint'):
            raise ValueError('翻页没有前进')
        params['fingerprint'] = fingerprint
    raise ValueError('待处理的记录太多，一轮扫不完（游标已保留）')
