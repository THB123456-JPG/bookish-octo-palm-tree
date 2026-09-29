# -*- coding: utf-8 -*-
"""能量上游适配层

统一接口，方便换供应商。现在只有 MockProvider 是真能跑的，
真实供应商（MerxProvider 等）的 HTTP 调用等用户选定后填 —— 照
usdt.py 里 `Rate` 类的模式写：独立 Session + 定制 headers +
失败抛 TgError + 实例级 TTL 缓存。

计费口径：上游报价都是「多少 TRX」（租金成本），
我们在上面加 markup 得到对客户的人民币/USDT 售价。
"""
import ipaddress
import socket
import time
import urllib.parse

import requests

from core import TgError, log
from tron import is_address

TRX_USDT_FALLBACK = 0.32        # 拿不到实时价时的兜底（1 TRX ≈ ? USDT）


def norm_username(u):
    """把 @xxx / xxx / https://t.me/xxx 统一成不带符号的 xxx"""
    s = str(u or '').strip()
    low = s.lower()
    for pre in ('https://t.me/', 'http://t.me/', 't.me/',
                'https://telegram.me/', 'telegram.me/'):
        if low.startswith(pre):
            s = s[len(pre):]
            break
    return s.lstrip('@').strip().rstrip('/')


# ============ SSRF 防护：只准连公网 ============
# ★★ 为什么需要这个：自定义上游的接口地址是**商户自己填的**。
#    不校验的话，填 `http://169.254.169.254/latest/meta-data/`（云厂商的
#    元数据接口，能拿到机器上的临时密钥）或者 `http://10.0.0.5:6379/`
#    （内网服务），就成了拿我们服务器当跳板去打内网。
#    `{addr}` 那种占位符替换**不构成防护** —— 主机名部分完全由填的人决定。

# 除了 Python 自带的「非公网」判定，再单独堵这几个：
#   ★ 100.64.0.0/10 —— 运营商 NAT 段，`is_global` 认为它是公网，
#     但云上这个段经常能通到内网（Python 的 ipaddress 里没收录）
_EXTRA_BAD = (ipaddress.ip_network('100.64.0.0/10'),)


def _ip_bad(ip):
    """这个 IP 能不能连？返回 (坏不坏, 原因)。
    ★ 「坏」= 内网 / 回环 / 链路本地 / 保留 / 组播 / 未指定。"""
    a = ipaddress.ip_address(str(ip).split('%')[0])   # 去掉 fe80::1%eth0 那种后缀
    if not a.is_global:
        return True, '不是公网地址'
    if a.is_multicast or a.is_link_local or a.is_reserved or a.is_loopback \
            or a.is_private or a.is_unspecified:
        return True, '不是公网地址'
    for net in _EXTRA_BAD:
        if a.version == net.version and a in net:
            return True, '运营商 NAT 段（云上可能通到内网）'
    return False, ''


def _default_resolve(host):
    """把主机名解析成一组 IP。测试里会换成假的，所以单独抽出来"""
    return sorted({ai[4][0] for ai in socket.getaddrinfo(host, None)})


def check_public_url(url, resolve=None):
    """接口地址必须是 http/https，且**解析出来的每一个 IP 都是公网**。

    返回 (True, '') 放行；返回 (False, 中文原因) 拦截。

    ★ 为什么要查「每一个」：DNS 一次可以返回多条 A 记录，
      只查第一条的话，把内网地址放第二条就绕过去了。
    ★ resolve 可注入 —— 测试不用联网就能造出「全是私网」「公私混合」的样例。
    ★ 诚实说明边界：「解析 → 校验 → 再连接」之间理论上还有 DNS rebinding 的
      窗口（校验时给公网 IP、真连接时给内网 IP）。彻底堵死要把连接**钉在
      已校验的 IP** 上，本轮没做。它挡不住的是这种**高级**攻击；
      挡得住的是「直接填内网地址」和「302 跳内网」，也就是绝大多数真实利用。
    """
    resolve = resolve or _default_resolve
    u = str(url or '').strip()
    if not u:
        return False, '接口地址是空的'
    try:
        p = urllib.parse.urlsplit(u)
    except Exception as e:
        return False, '接口地址看不懂：%s' % e
    if p.scheme not in ('http', 'https'):
        return False, '只允许 http:// 或 https://（现在填的是 %s）' % (
            (p.scheme + '://') if p.scheme else '（没写协议）')
    if not p.hostname:
        return False, '接口地址里没有主机名'
    if p.username or p.password:
        return False, ('接口地址里不能带用户名密码 —— '
                       '`http://xx@主机` 这种写法会被用来绕过检查')
    try:
        p.port        # 端口不是数字时这里会抛
    except ValueError:
        return False, '端口号不合法'
    try:
        ips = resolve(p.hostname)
    except Exception as e:
        return False, '解析不了这个域名（%s）' % e
    if not ips:
        return False, '这个域名解析不出 IP'
    for ip in ips:
        try:
            bad, why = _ip_bad(ip)
        except ValueError:
            return False, '解析出来的地址看不懂：%s' % (ip,)
        if bad:
            # ★ 不把具体 IP 原样回显给商户也没必要 —— 这个信息只他自己看得到，
            #   而且明确告诉他「你填的是内网」最有用
            return False, ('这个地址指向内网或保留地址（%s 是%s），'
                           '自定义上游只能填公网地址' % (ip, why))
    return True, ''


class UncertainError(TgError):
    """★ 结果不明：请求发出去了，但没拿到上游的明确答复。

    超时、断网、返回非 JSON 都属于这一类 —— 上游**可能已经扣费发货了**。
    调用方绝不能把它当普通失败去重试，重试会再扣一次钱。
    正确做法：停下来，人工到上游后台核对（下单时带过 traceId，能查）。
    """


class EnergyProvider:
    """所有上游都实现这几个方法。抛 TgError 表示可预期的失败。"""

    name = 'base'
    label = '未实现'

    def __init__(self, api_key='', premium_key='', cfg=None):
        self.api_key = (api_key or '').strip()
        # 会员可以走另一个账户（比如 ApiTrx 的 gift 域名，USDT 计价）。
        # 留空就用上面那个 key。
        self.premium_key = (premium_key or '').strip()
        # 商城配置（自定义上游要从中读接口地址等）
        self.cfg = dict(cfg or {})

    def configured(self):
        return False

    # ---- 必须实现 ----
    def quote(self, energy, hours=1):
        """询价。返回 (需要多少 TRX, 原始数据dict)"""
        raise TgError('%s 还没实现询价' % self.label)

    def order(self, address, energy, hours=1, trace=''):
        """下单委托能量。返回 (上游订单号, 原始数据dict)

        trace: 对账标识（一般传客户的付款 txid）。支持的上游会回显，
               方便事后核对「这笔钱到底发没发」。
        """
        raise TgError('%s 还没实现下单' % self.label)

    def status(self, provider_order_id):
        """查订单。返回 ('pending'|'done'|'failed', 原始数据dict)"""
        raise TgError('%s 还没实现查单' % self.label)

    # ---- 笔数（智能笔数等）----
    def auto_buy(self, address, count, auto_type=1, callback_url=''):
        """买笔数。返回 (上游订单号, 原始数据)"""
        raise TgError('%s 不支持笔数套餐' % self.label)

    def auto_list(self, receiver='', current=1, page_size=20):
        """笔数列表（查剩余笔数）。返回 [{...}]"""
        raise TgError('%s 不支持笔数查询' % self.label)

    def auto_records(self, order_id, current=1, page_size=20):
        """某笔笔数订单的消费明细。返回 [{...}]"""
        raise TgError('%s 不支持笔数消费查询' % self.label)

    def auto_remain(self, address):
        """某个地址还剩几笔（名下所有订单的剩余加起来）。查不到返回 None"""
        try:
            rows = self.auto_list(address)
        except Exception as e:
            log('%s 查笔数失败：%s' % (self.label, e))
            return None
        remain = bought = used = 0
        for x in (rows or []):
            try:
                remain += int(x.get('remainingCount') or 0)
                bought += int(x.get('totalCount') or 0)
                used += int(x.get('finishCount') or 0)
            except (TypeError, ValueError):
                continue
        return {'remain': remain, 'bought': bought,
                'used': used, 'orders': rows or []}

    # ---- TG 会员（不支持的上游会抛错，上层会回「暂不支持」）----
    def premium_kind(self):
        """会员按什么计价：填了 premium_key 就是 usdt（gift 域名），否则 trx"""
        return 'usdt' if self.premium_key else 'trx'

    def premium_ready(self):
        return bool(self.premium_key or self.api_key)

    def premium_query(self, username):
        """查用户名能不能开通会员。返回 {'username','nickname','photo'}"""
        raise TgError('%s 不支持开通 TG 会员' % self.label)

    def premium_open(self, username, month, callback_url=''):
        """开通会员。返回 (上游订单号, 原始数据)"""
        raise TgError('%s 不支持开通 TG 会员' % self.label)

    # ---- 可选 ----
    def balance(self):
        """上游账户余额（TRX 或 USD），拿不到返回 None"""
        return None

    def self_test(self):
        """面板上的「测试连接」。返回 (是否成功, 说明)"""
        if not self.configured():
            return False, '还没配置 API Key'
        return False, '%s 的自检还没实现' % self.label


class MockProvider(EnergyProvider):
    """本地测试用：不花钱、立即成功。用来验证订单全链路。"""

    name = 'mock'
    label = '模拟上游（测试用）'

    # 模拟价格：每 1000 能量 0.03 TRX（约等于市场 30 SUN）
    SUN_PER_ENERGY = 30

    def configured(self):
        return True

    def quote(self, energy, hours=1):
        energy = int(energy or 0)
        trx = energy * self.SUN_PER_ENERGY / 1e6
        # 租期越长越贵（照市场规律，14 天约 2~3.5 倍）
        mult = {1: 1.0, 24: 1.6, 72: 2.2}.get(int(hours or 1), 1.0)
        return round(trx * mult, 6), {'mock': True, 'energy': energy, 'hours': hours}

    def order(self, address, energy, hours=1, trace=''):
        oid = 'MOCK%d' % int(time.time() * 1000)
        log('[Mock] 假装给 %s 委托 %s 能量 %s 小时（单号 %s）'
            % (address, energy, hours, oid))
        return oid, {'mock': True, 'order_id': oid}

    def status(self, provider_order_id):
        return 'done', {'mock': True}

    def auto_buy(self, address, count, auto_type=1, callback_url=''):
        a = (address or '').strip()
        if not is_address(a):
            raise TgError('地址不像波场地址：%s' % (a or '（空）'))
        count = int(count or 0)
        if count < 2:
            raise TgError('笔数最少买 2 笔，现在填的是 %d' % count)
        oid = 'MOCKA%d' % int(time.time() * 1000)
        log('[Mock] 假装给 %s 买了 %d 笔（单号 %s）' % (a[:12], count, oid))
        return oid, {'mock': True, 'orderId': oid, 'amount': 0.0,
                     'balance': 999999.0}

    def auto_list(self, receiver='', current=1, page_size=20):
        return [{'orderId': 1001, 'orderType': 1,
                 'receiver': receiver or 'TXxx',
                 'totalCount': 10, 'finishCount': 3, 'stayCount': 0,
                 'remainingCount': 7, 'status': 1,
                 'createdAt': int(time.time())}]

    def auto_records(self, order_id, current=1, page_size=20):
        return [{'orderId': order_id, 'usageQuantity': 65000,
                 'currentCount': 1, 'txid': 'MOCKTX',
                 'createdAt': int(time.time())}]

    def premium_query(self, username):
        u = norm_username(username)
        if not u or len(u) < 4:
            raise TgError('用户名看着不对：%s' % (username or '（空）'))
        return {'username': u, 'nickname': '模拟用户', 'photo': ''}

    def premium_open(self, username, month, callback_url=''):
        u = norm_username(username)
        oid = 'MOCKP%d' % int(time.time() * 1000)
        log('[Mock] 假装给 @%s 开通 %s 个月会员（单号 %s）' % (u, month, oid))
        return oid, {'mock': True, 'orderId': oid, 'status': 1,
                     'amount': 0.0, 'remark': '模拟开通成功'}

    def balance(self):
        return 999999.0

    def self_test(self):
        trx, _ = self.quote(65000, 1)
        return True, '模拟上游正常。65,000 能量报价 %.4f TRX' % trx


class ApitrxProvider(EnergyProvider):
    """ApiTrx 波场能量租赁（https://apitrx.com）—— 实测可直接跑

    接口（照官网 https://apitrx.com/pages/api.html）：
        GET /getenergy?apikey=&add=<收能量地址>&value=<能量>&hour=<小时>
            → {code, data:{amount 本次扣费TRX, balance 剩余TRX, txid 代理哈希}}
              code: 200 成功 / 500 失败 / 501 账户禁用 / 502 余额不足
        GET /balance?apikey=        → data.balance（预付余额，单位 TRX）
        GET /estimate?apikey=&receiver=&destination=
            自动判断要 65k 还是 131k 并**直接下单**（不是询价接口，别乱调）

    ★ 一定要直连：本类关掉了 trust_env，忽略系统里的 HTTP_PROXY。
      实测走代理反而被 Cloudflare 挡成 429，直连稳定。
    ★ 上游是预付制：先在 @XXTrxBot 充值 TRX，下单按次扣余额。
      余额见底 = 全部订单发不出去，所以 self_test 会重点查余额。
    """

    name = 'apitrx'
    label = 'ApiTrx 波场能量'

    BASE = 'https://web.apitrx.com'      # TRX 计价（能量 + 会员都能用）
    GIFT = 'https://gift.apitrx.com'     # USDT 计价（会员走这里）
    TIMEOUT = 25
    # 只读接口的容错：上游偶尔慢一下或者限流，重试就行（绝不扣钱）
    READONLY_TRIES = 3
    READONLY_WAIT = (1.0, 3.0)
    MIN_ENERGY = 32000          # 上游硬性下限，低于这个直接拒单
    BASE_ENERGY = 65000         # 官方公示价格的基准能量

    # 官方公示价：65000 能量各租期要多少 TRX（2026-09 官网价格表）
    PRICES = {1: 1.1, 24: 4.8, 72: 11.7, 168: 27.3, 336: 54.6, 720: 117.0}
    HOUR_LABEL = {1: '1 小时', 24: '1 天', 72: '3 天',
                  168: '7 天', 336: '14 天', 720: '30 天'}

    def __init__(self, api_key='', premium_key='', cfg=None):
        super().__init__(api_key, premium_key, cfg)
        self._sess = None

    def _session(self):
        if self._sess is None:
            s = requests.Session()
            s.headers.update({
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
                'Accept': 'application/json',
            })
            s.trust_env = False     # ★ 忽略系统代理，走了会被 Cloudflare 挡
            self._sess = s
        return self._sess

    def configured(self):
        return bool(self.api_key)

    # -------- 底层请求 --------
    def _call(self, path, params, method='GET', base=None, key=None,
              readonly=False):
        """调上游接口。

        ★ readonly 只给**查数据**的接口用（查余额/查笔数/查明细/查用户名）：
          这些调一百遍也不会扣钱，所以超时、429、5xx 都可以放心重试。
          不重试的话，上游偶尔抽一下，面板就显示「查不到余额」、
          自检还会误报「检查 API Key 对不对」—— 全是假警报。

        ★★ 下单接口（getenergy / auto / premium）**绝不能**传 readonly：
          请求已经出去了，上游可能已经扣费，重试就是再扣一次钱。
        """
        use_key = self.api_key if key is None else key
        if not use_key:
            raise TgError('还没填 %s 的 API Key' % self.label)
        p = dict(params)
        p['apikey'] = use_key
        url = (base or self.BASE) + path
        tries = self.READONLY_TRIES if readonly else 1

        for attempt in range(tries):
            last = attempt == tries - 1
            # ↓ 以下三种都算「结果不明」：请求已经出去了，上游可能已经扣费。
            #   用 UncertainError 标记，让上层停下来等人工，而不是重试。
            try:
                if method == 'POST':
                    # 会员接口是 POST + JSON，能量接口是 GET
                    r = self._session().post(url, json=p, timeout=self.TIMEOUT)
                else:
                    r = self._session().get(url, params=p, timeout=self.TIMEOUT)
            except Exception as e:
                if not last:
                    time.sleep(self.READONLY_WAIT[attempt])
                    continue
                raise UncertainError('连不上 %s（%s），这笔结果不明'
                                     % (self.label, e))
            # 429 限流、5xx 上游自己抽了 —— 只读的话重试就没问题
            if r.status_code == 429 or r.status_code >= 500:
                if not last:
                    log('%s 返回 HTTP %s，%s重试'
                        % (self.label, r.status_code,
                           '只读接口，' if readonly else ''))
                    time.sleep(self.READONLY_WAIT[attempt])
                    continue
            if r.status_code != 200:
                raise UncertainError('%s 返回 HTTP %s，这笔结果不明'
                                     % (self.label, r.status_code))
            try:
                j = r.json()
            except Exception:
                # Cloudflare 偶尔会回一页 HTML 过来，重试往往就好了
                if not last:
                    time.sleep(self.READONLY_WAIT[attempt])
                    continue
                raise UncertainError('%s 返回的不是 JSON（%s），这笔结果不明'
                                     % (self.label, (r.text or '')[:120]))
            break

        # 到这里上游已经明确答复了，后面都是「确定失败」，重发是安全的
        code = j.get('code')
        if code != 200:
            msg = str(j.get('message') or '').strip()
            if code == 502:
                # 余额见底是最常见的故障，必须让人一眼看见
                log('⚠️⚠️ %s 上游余额不足，所有订单都发不出去，赶紧充值'
                    % self.label)
                raise TgError('%s 余额不足，请先给上游充值 TRX' % self.label)
            # 上游的 message 往往比通用提示有用得多（比如「会员api权限未开启」），
            # 所以优先把它原样带出来
            hint = {501: '上游账户被禁用，联系上游客服'}.get(code, '')
            raise TgError('%s 失败：%s %s'
                          % (self.label, msg or ('code=%s' % code), hint))
        return j.get('data') or {}

    # -------- 询价（估算用，不花钱）--------
    def _base_price(self, hours):
        """65000 能量在某个租期下的官方价"""
        hours = int(hours or 1)
        if hours in self.PRICES:
            return self.PRICES[hours]
        near = min(self.PRICES, key=lambda h: abs(h - hours))
        return self.PRICES[near] * hours / float(near)

    def quote(self, energy, hours=1):
        """成本估算。官方只公示了 65000 一档，所以：
             ≤65000 → 按一档的价（保守估高，不会让我们亏）
             >65000 → 按比例放大
           真实扣费以下单返回的 amount 为准。
        """
        energy = int(energy or 0)
        base = self._base_price(hours)
        trx = (base if energy <= self.BASE_ENERGY
               else base * energy / float(self.BASE_ENERGY))
        return round(trx, 4), {
            'provider': self.name, 'energy': energy, 'hours': int(hours or 1),
            'base_energy': self.BASE_ENERGY, 'base_trx': base,
            'estimated': True,
        }

    # -------- 下单（花钱）--------
    def order(self, address, energy, hours=1, trace=''):
        address = (address or '').strip()
        if not is_address(address):
            raise TgError('接收地址不像波场地址：%s' % (address or '（空）'))
        energy = int(energy or 0)
        if energy < self.MIN_ENERGY:
            raise TgError('%s 最低要 %d 能量，现在要发 %d'
                          % (self.label, self.MIN_ENERGY, energy))
        hours = int(hours or 1)
        if hours not in self.PRICES:
            raise TgError('%s 的租期只能是 %s（小时）'
                          % (self.label,
                             '/'.join(str(h) for h in sorted(self.PRICES))))

        params = {'add': address, 'value': energy, 'hour': hours}
        if trace:
            params['traceId'] = str(trace)[:36]     # 带上客户付款 txid，方便对账
        data = self._call('/getenergy', params)

        txid = str(data.get('txid') or '').strip()
        if not txid:
            # ★ 上游回了「成功」却没给交易哈希 —— 钱大概率已经扣了。
            #   算「结果不明」，绝不能重试（重试会再扣一次钱）。
            raise UncertainError(
                '%s 回了成功但没给交易哈希，这笔可能已经扣费发货了。'
                '请先到上游后台核对' % self.label)
        log('[ApiTrx] 发货 → %s 能量/%s，扣 %.4f TRX，上游余额 %.4f TRX，单号 %s'
            % (address[:12], self.HOUR_LABEL.get(hours) or '%s 小时' % hours,
               float(data.get('amount') or 0),
               float(data.get('balance') or 0), txid[:20]))
        return txid, data

    # -------- 笔数（智能笔数）--------
    # ★ 只有 web 域名有（TRX 计价），gift 那个账户做不了笔数。
    #   所以笔数业务的客户得付 TRX。
    #   客户买完 N 笔后，他那个地址以后转账时上游自动从笔数里扣，
    #   我们这边不用管。
    AUTO_TYPE = {0: '笔数套餐（用多少都算1笔）',
                 1: '智能笔数（65k算1笔，131k算2笔）',
                 2: '账号代扣（不管多少都扣1笔）',
                 3: '账号代扣（65k扣1笔，131k扣2笔）'}

    def auto_buy(self, address, count, auto_type=1, callback_url=''):
        """买笔数。返回 (上游订单号, 原始数据)"""
        address = (address or '').strip()
        if not is_address(address):
            raise TgError('地址不像波场地址：%s' % (address or '（空）'))
        count = int(count or 0)
        if count < 2:
            raise TgError('笔数最少买 2 笔，现在填的是 %d' % count)
        auto_type = int(auto_type if auto_type is not None else 1)
        if auto_type not in self.AUTO_TYPE:
            raise TgError('autoType 只能是 0/1/2/3，现在给的是 %s' % auto_type)

        p = {'add': address, 'count': count, 'autoType': auto_type}
        if callback_url:
            p['callback_url'] = callback_url
        d = self._call('/auto', p)          # 这个是 GET

        oid = str(d.get('orderId') or '').strip()
        if not oid:
            raise UncertainError(
                '%s 回了成功但没给订单号，这笔可能已经买了笔数，'
                '请先到上游后台核对' % self.label)
        log('[ApiTrx] 买笔数 %s × %d 笔（%s），扣 %.4f TRX，余额 %.4f TRX，单号 %s'
            % (address[:12], count, self.AUTO_TYPE.get(auto_type, ''),
               float(d.get('amount') or 0),
               float(d.get('balance') or 0), oid))
        return oid, d

    def auto_list(self, receiver='', current=1, page_size=20):
        """笔数列表。传 receiver 就只看那个地址的，不传查全部。"""
        p = {'current': int(current or 1), 'pageSize': int(page_size or 20)}
        if receiver:
            p['receiver'] = str(receiver).strip()
        d = self._call('/list', p, method='POST', readonly=True)
        return d if isinstance(d, list) else []

    def auto_records(self, order_id, current=1, page_size=20):
        """某笔笔数订单的消费明细"""
        d = self._call('/record',
                       {'orderId': int(order_id),
                        'current': int(current or 1),
                        'pageSize': int(page_size or 20)},
                       method='POST', readonly=True)
        return d if isinstance(d, list) else []

    # -------- TG 会员（Premium）--------
    # 和能量共用同一个 API Key、同一个账户余额，只是端点不同。
    # 注意：得先去 @XXTrxBot 把「会员API权限」打开，
    #      否则会报「会员api权限未开启，请前往机器人开启」。
    def _premium_call(self, path, params, method='POST', readonly=False):
        """会员接口。填了 premium_key 就走 gift 域名（USDT 计价）。

        ★ 实测两个 key 都有效，只是属于不同域名，"串门"会报 apikey错误：
          · web 域名  ← 能量那个 key（@XXTrxBot，TRX 计价，和能量共用余额）
          · gift 域名 ← @GiftAPIBot 的 key（USDT 计价，另一个独立账户）
        """
        if self.premium_key:
            return self._call(path, params, method=method,
                              base=self.GIFT, key=self.premium_key,
                              readonly=readonly)
        return self._call(path, params, method=method, readonly=readonly)

    def premium_query(self, username):
        """查这个用户名能不能开通会员（不花钱）。返回资料 dict"""
        u = norm_username(username)
        if not u or len(u) < 4:
            raise TgError('用户名看着不对：%s' % (username or '（空）'))
        d = self._premium_call('/query', {'username': u}, readonly=True)
        return {
            'username': d.get('username') or u,
            'nickname': str(d.get('nickname') or '').strip(),
            'photo': str(d.get('photo') or '').strip(),
        }

    def premium_open(self, username, month, callback_url=''):
        """开通会员。返回 (上游订单号, 原始数据)

        返回的 data.status：0 开通中 / 1 已开通 / 2 开通失败
        """
        u = norm_username(username)
        if not u:
            raise TgError('没给用户名，开不了')
        month = int(month or 0)
        if month not in (3, 6, 12):
            raise TgError('会员只能开 3 / 6 / 12 个月，现在要开 %s 个月' % month)

        p = {'username': u, 'month': str(month)}
        if callback_url:
            p['callback_url'] = callback_url
        d = self._premium_call('/premium', p)

        oid = str(d.get('orderId') or '').strip()
        if not oid:
            # 回成功却没给订单号 —— 可能已经开通了，不能当失败去重试
            raise UncertainError(
                '%s 回了成功但没给订单号，这笔可能已经开通了，'
                '请先到上游后台核对' % self.label)
        log('[ApiTrx] 开通会员 %s %s个月，扣 %.4f TRX，余额 %.4f TRX，'
            '状态 %s，单号 %s'
            % (u, month, float(d.get('amount') or 0),
               float(d.get('balance') or 0), d.get('status'), oid))
        return oid, d

    def status(self, provider_order_id):
        """getenergy 是同步接口：返回 200 就代表已经委托出去了，
        没有异步查单接口，所以拿到 txid 就算完成。"""
        return 'done', {'provider': self.name,
                        'txid': provider_order_id, 'sync': True}

    def balance(self):
        try:
            d = self._call('/balance', {}, readonly=True)
            self._bal_err = ''
            return float(d.get('balance') or 0)
        except Exception as e:
            # ★ 记下来给 self_test 用 —— 否则「上游限流了」和「Key 填错了」
            #   都会变成一句「查不到余额」，用户会跑去反复改 Key，白折腾
            self._bal_err = str(e)
            log('%s 查余额失败：%s' % (self.label, e))
            return None

    # 说明 Key/权限有问题的关键词 —— 只有这些才值得让人去改配置
    KEY_HINTS = ('apikey', 'api key', '密钥', '权限', '账户禁用', '禁用',
                 '未开启', '不存在', '无效', 'invalid', 'unauthorized')

    def self_test(self):
        if not self.configured():
            return False, '还没填 API Key'
        try:
            bal = self.balance()
        except Exception as e:
            return False, '连接失败：%s' % e
        if bal is None:
            err = getattr(self, '_bal_err', '') or ''
            low = err.lower()
            if any(k in low for k in self.KEY_HINTS):
                return False, 'API Key 可能不对：%s' % (err[:80] or '上游拒绝了')
            return False, ('上游暂时连不上（网络或限流，不是 Key 的问题，'
                           '稍后再试一次）：%s' % (err[:80] or '读不到余额'))
        trx, _ = self.quote(self.BASE_ENERGY, 1)
        msg = ('连接正常。上游余额 %.4f TRX；%s 能量 1 小时成本约 %.2f TRX'
               % (bal, format(self.BASE_ENERGY, ','), trx))
        if bal < trx:
            msg += '。⚠️ 余额不够发一单，请先充值'
        return True, msg


class MerxProvider(EnergyProvider):
    """MERX 聚合器（https://merx.exchange）—— 待填 HTTP 调用

    需要：登录后台拿 API Key，看 https://merx.exchange/blog/zh/c09-energy-cost-estimation-api
    已知端点（2026-09 调研）：
        POST /api/v1/estimate              成本估算（公开，无需 Key）
        GET  /api/v1/orders/preview        订单预览（含平台费、路由供应商）
    """

    name = 'merx'
    label = 'MERX'

    def configured(self):
        return bool(self.api_key)


class TronRentalProvider(EnergyProvider):
    """TronRental（https://docs.tronrental.com）—— 待填 HTTP 调用

    已知端点（2026-09 调研）：
        GET  /v1/prices                    查价
        POST /v1/energy/buy                下单 {target_address, volume, duration}
        GET  /v1/orders                    查订单
        GET  /v1/account/deposit           充值地址（支持 TRX / USDT）
    请求头：X-API-Key
    注意：只提供 1 小时租期；另收 0.2 TRX 固定费
    """

    name = 'tronrental'
    label = 'TronRental'

    def configured(self):
        return bool(self.api_key)


class GetBlockProvider(EnergyProvider):
    """GetBlock（https://getblock.io/tron-energy/）—— 待填 HTTP 调用

    已知端点（2026-09 调研）：
        POST https://products-api.getblock.io/tronEnergy/delegateEnergy
        认证：Authorization: Bearer <key>
        duration 支持 1h/1d/3d/7d/14d，能量 30K~5M
        按 Credits 计费，委托确认后才扣费
    """

    name = 'getblock'
    label = 'GetBlock'

    def configured(self):
        return bool(self.api_key)


class CustomProvider(EnergyProvider):
    """自定义上游 —— 让商户照着自己上游的文档填接口。

    能覆盖大部分「一个 URL + apikey/地址/能量/时长」形状的上游：
        GET  https://你的上游/buy?key={key}&addr={addr}&energy={energy}&hour={hours}
    地址里用占位符，发请求时会被换成真值。

    判定成功：默认看返回 JSON 的 code == 200（可改）。
    取订单号：默认从 data.orderId 取（可改）。

    ★ 接不了的：要签名的、要加密的、要多步换 token 的 ——
      那种得单独写适配器（把文档发过来）。
    ★ 查不了余额：这类接口一般没有余额查询，所以「每单成本」只能靠
      实测扣费来估（发货成功时会把上游返回的 amount 记下来）。
    """

    name = 'custom'
    label = '自定义上游（照文档填）'
    TIMEOUT = 25

    def __init__(self, api_key='', premium_key='', cfg=None):
        super().__init__(api_key, premium_key)
        self.cfg = dict(cfg or {})
        self._sess = None

    def configured(self):
        return bool((self.cfg.get('custom_url') or '').strip()) and bool(self.api_key)

    def _session(self):
        if self._sess is None:
            s = requests.Session()
            s.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; '
                                            'Win64; x64)'})
            # ★ 忽略系统里的 HTTP_PROXY 环境变量 —— 跟 ApitrxProvider 一致。
            #   不关的话，「走哪个代理」就跑到我们的校验之外了
            #   （代理可能在内网，等于绕开了 check_public_url）。
            s.trust_env = False
            self._sess = s
        return self._sess

    def _url(self, address, energy, hours):
        tpl = str(self.cfg.get('custom_url') or '').strip()
        return (tpl.replace('{key}', self.api_key)
                   .replace('{addr}', address)
                   .replace('{energy}', str(energy))
                   .replace('{hours}', str(hours)))

    @staticmethod
    def dig(obj, path):
        """按 'data.orderId' 这种路径从返回里取值。路径空 → None"""
        p = str(path or '').strip()
        if not p:
            return None
        cur = obj
        for part in p.split('.'):
            if not part:
                continue
            if isinstance(cur, dict):
                cur = cur.get(part)
            elif isinstance(cur, list) and part.isdigit():
                try:
                    cur = cur[int(part)]
                except (IndexError, ValueError):
                    return None
            else:
                return None
        return cur

    def quote(self, energy, hours=1):
        """这类接口一般没有询价 —— 返回 0 表示「成本不知道」，
        面板会拿实测扣费来估"""
        return 0.0, {'custom': True, 'unknown': True}

    def order(self, address, energy, hours=1, trace=''):
        if not self.configured():
            raise TgError('%s 还没配好（接口地址 + API Key）' % self.label)
        address = (address or '').strip()
        if not is_address(address):
            raise TgError('接收地址不像波场地址：%s' % (address or '（空）'))
        energy = int(energy or 0)
        hours = int(hours or 1)
        url = self._url(address, energy, hours)
        method = str(self.cfg.get('custom_method') or 'GET').upper()

        # ★★ 真正的那道门：请求前再验一次目标是不是公网。
        #    存的时候验过没用 —— 域名可以事后改解析（DNS rebinding / 换记录），
        #    所以**每次真发请求之前**都要按当下的解析结果判一次。
        #    ★ 这一条拦截是**明确失败**（TgError 不是 UncertainError）：
        #      请求根本没发出去，上游不可能已经扣费 → 允许重发。
        ok, why = check_public_url(url)
        if not ok:
            raise TgError('自定义上游的接口地址不能用：%s' % why)

        # ↓ 结果不明一律 UncertainError，绝不能当失败去重试（会重复扣钱）
        try:
            # ★ allow_redirects=False：不跟重定向。
            #   否则商户填一个「302 → http://169.254.169.254/」的公网地址，
            #   check_public_url 放行、requests 自己跟过去，等于白验。
            if method == 'POST':
                r = self._session().post(url, timeout=self.TIMEOUT,
                                         allow_redirects=False)
            else:
                r = self._session().get(url, timeout=self.TIMEOUT,
                                        allow_redirects=False)
        except Exception as e:
            raise UncertainError('连不上自定义上游（%s），这笔结果不明' % e)
        if r.status_code in (301, 302, 303, 307, 308):
            # 不跟随重定向，所以这里得显式给个说法，不能让 3xx 掉进下面的分支。
            # ★ 仍然算**结果不明**：请求实实在在发到对面了，对面到底处理没处理
            #   我们不知道。按「不确定」处置 → 转人工核对，绝不自动重发。
            #   （代价是偶发一次人工核对，换来的是**绝不会重复扣钱**）
            raise UncertainError(
                '自定义上游返回了跳转（HTTP %s → %s）。'
                '接口地址要填**最终**那个地址（不要填会跳转的短链）。'
                '这笔结果不明，请先核对'
                % (r.status_code,
                   r.headers.get('Location', '（没给目标）')[:80]))
        if r.status_code != 200:
            raise UncertainError('自定义上游返回 HTTP %s，这笔结果不明'
                                 % r.status_code)
        try:
            j = r.json()
        except Exception:
            raise UncertainError('自定义上游返回的不是 JSON（%s），这笔结果不明'
                                 % (r.text or '')[:120])

        ok_path = str(self.cfg.get('custom_ok_path') or 'code').strip() or 'code'
        ok_val = str(self.cfg.get('custom_ok_value') or '200').strip() or '200'
        got = self.dig(j, ok_path)
        if str(got) != ok_val:
            msg = self.dig(j, 'message') or self.dig(j, 'msg') or ''
            raise TgError('上游说没成功（%s=%s %s）'
                          % (ok_path, got, str(msg)[:60]))

        oid_path = str(self.cfg.get('custom_order_path') or 'data.orderId').strip()
        oid = self.dig(j, oid_path)
        if oid is None:
            # 配的路径取不到就按常见写法挨个试 —— 别因为位置没填准就白白卡住
            for fb in ('data.orderId', 'orderId', 'data.order_id', 'order_id',
                       'data.txid', 'txid', 'data.data.orderId',
                       'result.orderId', 'data.id', 'id'):
                oid = self.dig(j, fb)
                if oid is not None:
                    break
        oid = str(oid or '').strip()
        if not oid:
            raise UncertainError(
                '上游回了成功，但按「%s」取不到订单号。'
                '检查一下「订单号在返回里的位置」填对没，这笔请先核对'
                % oid_path)
        log('[自定义上游] %s 能量/%s 小时 → 单号 %s'
            % (address[:12], hours, oid[:24]))
        return oid, j

    def balance(self):
        """这类接口没有余额查询，返回 None（面板会显示「查不到」）"""
        return None

    def self_test(self):
        u = str(self.cfg.get('custom_url') or '').strip()
        if not u:
            return False, '还没填接口地址'
        if not self.api_key:
            return False, '还没填 API Key'
        if '{addr}' not in u:
            return False, '接口地址里必须有 {addr} 占位符（不然不知道发给谁）'
        # ★ 自检跟真实发货走**同一道校验**（不然自检过了、发货时被拦，更懵）
        ok, why = check_public_url(u)
        if not ok:
            return False, why
        return True, ('填好了。提醒：自定义上游查不了余额，'
                      '真实成本要等第一笔发货后看实测扣费')


PROVIDERS = {
    'apitrx': ApitrxProvider,
    'custom': CustomProvider,
    'mock': MockProvider,
    'merx': MerxProvider,
    'tronrental': TronRentalProvider,
    'getblock': GetBlockProvider,
}


def get_provider(name, api_key='', premium_key='', cfg=None):
    """按名字拿一个 provider 实例，认不出来就退回 Mock 并记日志"""
    cls = PROVIDERS.get((name or '').strip().lower())
    if not cls:
        log('未知的能量上游 %r，退回模拟上游' % name)
        cls = MockProvider
    return cls(api_key, premium_key, cfg)


def provider_list():
    """给面板下拉用"""
    return [{'value': k, 'label': v.label} for k, v in PROVIDERS.items()]


def trx_to_usdt(trx, trx_price=None):
    """TRX 换算成 USDT。拿不到实时价就用兜底价"""
    try:
        price = float(trx_price) if trx_price else TRX_USDT_FALLBACK
    except (TypeError, ValueError):
        price = TRX_USDT_FALLBACK
    return round(float(trx) * price, 6)
