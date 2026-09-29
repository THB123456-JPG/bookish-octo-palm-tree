# -*- coding: utf-8 -*-
"""商城数据层：收款流水、地址档案、配置

★ 核心模型（照 AE86X/Tronbot 的思路）：**不依赖机器人交互**
   客户从自己钱包转 TRX 到你的收款地址 → 系统看到 from 是谁 →
   就把能量发给谁。地址配对信息就在转账记录里，不需要客户登记。

   没有「订单」，只有「收款流水」。金额按倍数折算：
       转 3.5 TRX（单价 3.5）→ 1 个单位 → 32000 能量
       转 7   TRX          → 2 个单位 → 64000 能量

配置存在 bots.json 的 `bot['shop']`（面板可改），
运行时数据存在 `data/<bot_id>.json`。
"""
import os
import random
import time
from datetime import datetime

import core

# 收款流水的处理状态
P_DONE = 'done'          # 已发能量
P_FAILED = 'failed'      # 发货失败（上游报错 / 余额不足）
P_SKIPPED = 'skipped'    # 金额不够一个单位，忽略
P_PENDING = 'pending'    # 已识别，还没发（一般看不到这个状态）

PAY_LABEL = {
    P_DONE: '已发货',
    P_FAILED: '发货失败',
    P_SKIPPED: '金额不足',
    P_PENDING: '待处理',
}
NEEDS_ATTENTION = (P_FAILED, P_PENDING)

MAX_PAYMENTS = 500       # 最多保留多少条流水

# ===== TG 会员（Premium）订单状态 =====
# 会员和能量不一样：能量是「谁转账就把能量发给谁」，地址即身份；
# 会员是「给某个 TG 用户名开通」，钱和用户名对不上号，
# 所以会员走**先下单、付唯一金额**的路子。
PR_CREATED = 'created'   # 已下单，等付款
PR_PAID = 'paid'         # 收到钱，正在开通
PR_DONE = 'done'         # 开通成功
PR_FAILED = 'failed'     # 开通失败（上游报错，需人工）
PR_EXPIRED = 'expired'   # 超时没付款

PR_LABEL = {
    PR_CREATED: '待付款',
    PR_PAID: '开通中',
    PR_DONE: '已开通',
    PR_FAILED: '开通失败',
    PR_EXPIRED: '已超时',
}
PR_NEEDS_ATTENTION = (PR_FAILED,)

PREMIUM_MONTHS = (3, 6, 12)      # 上游只支持这三档
PREMIUM_EXPIRE_MIN = 30          # 下单后多少分钟没付款就作废
MAX_PREMIUMS = 300


# ================= 默认配置 =================
DEFAULT_CONFIG = {
    'enabled': True,
    'trx_own': '',              # ★ 收 TRX 的地址（必填）
    'price': 3.5,               # 一个单位多少 TRX
    'energy': 32000,            # 一个单位给多少能量
    'max': 10,                  # 最高翻几倍（超过就只按 max 发）
    'min_units': 1,             # 最少几个单位才发（不够就忽略）

    'provider': 'mock',         # 能量上游
    'provider_key': '',         # 上游 API Key

    # ===== 自定义上游（上游下拉选「自定义」时用）=====
    # 客户有自己的货源、不是内置那几家时，照着他自己的文档填这里。
    # 地址里用占位符：{key} {addr} {energy} {hours}
    'custom_url': '',
    'custom_method': 'GET',                 # GET / POST
    'custom_ok_path': 'code',               # 怎么算成功：看返回的这个字段
    'custom_ok_value': '200',               # 等于这个值就算成功
    'custom_order_path': 'data.orderId',    # 订单号在返回里的位置

    'group_ids': '',            # 订单通知到哪些群（逗号分隔，可空）
    'group_bot_token': '',      # 用哪个机器人发通知（空=用本机器人）

    # 笔数套餐（暂未启用，先留字段）
    'auto_price': 0,            # 笔数单价，0 = 不启用
    'auto_values': [],          # 笔数按钮，如 [10, 20, 50, 100]

    'contact': '',              # 客服联系方式，客户问的时候回
    'notice': '',               # 转账页面顶部的自定义说明

    # ===== TG 会员（Premium）自助开通 =====
    'premium_enabled': False,   # 总开关，默认关（先去上游开权限 + 充值）
    # 会员专用的上游 Key。留空 = 用上面那个 provider_key（ApiTrx 的 web 域名，
    # TRX 计价、和能量共用余额）；填 @GiftAPIBot 的 key = USDT 计价，走 gift 域名。
    'premium_key': '',
    'premium_prices': {         # 各套餐卖多少 ***USDT***（上游成本 12.5/16.5/29.5）
        '3': 15.5,
        '6': 20.5,
        '12': 36.5,
    },
    'premium_callback': '',     # 开通结果回调地址（可空）

    # ===== 笔数（智能笔数）=====
    # ★ 只有 ApiTrx 的 web 域名有（TRX 计价），gift 那个账户做不了，
    #   所以笔数业务的客户付的是 TRX。
    'auto_enabled': False,      # 总开关
    # 各档卖多少 TRX。★ key 就是笔数，想上几档就写几档
    #（上游只要 >= 2 就行，没有上限；成本要去 @XXTrxBot 查）
    # ★ 默认留空 —— 档位是完全自定义的，不能被默认值掺进来
    'auto_prices': {},
    'auto_type': 1,             # 1=智能笔数(65k算1笔,131k算2笔)，用户指定
}


def _fill_missing(default, cur):
    """嵌套配置的合并：只补 default 里有、cur 里缺的键。

    ★ 不能拿 merge_default 递归 —— 那会把整个 DEFAULT_CONFIG
      （enabled / price / provider ...）都塞进 premium_prices 里，
      面板上就会看到一坨垃圾字段。
    """
    out = dict(cur)
    for k, v in default.items():
        if k not in out:
            out[k] = v
    return out


def merge_default(cfg):
    """把缺的配置项补上（面板加过新字段后，老配置也能用）"""
    out = {}
    for k, v in DEFAULT_CONFIG.items():
        cur = (cfg or {}).get(k)
        if isinstance(v, dict):
            out[k] = _fill_missing(v, cur) if isinstance(cur, dict) else dict(v)
        elif isinstance(v, list):
            out[k] = cur if isinstance(cur, list) else list(v)
        elif cur is None:
            out[k] = v
        else:
            out[k] = cur
    for k, v in (cfg or {}).items():
        out.setdefault(k, v)
    return out


class ShopStore:
    """所有读写都通过它，别在别处直接碰 data"""

    def __init__(self, runner):
        self.r = runner
        self.bot = runner.bot
        self.data = runner.data

    # ================= 配置 =================
    @property
    def cfg(self):
        return merge_default(self.bot.setdefault('shop', {}))

    def set_cfg(self, patch):
        cur = dict(self.bot.get('shop') or {})
        cur.update(patch or {})
        self.bot['shop'] = cur
        self.r.mgr.save()

    def save(self):
        self.r.save_data()

    def ready(self):
        """能不能对外服务（只差收款地址这一项）"""
        return bool((self.cfg.get('trx_own') or '').strip())

    # ================= 收款流水 =================
    @property
    def payments(self):
        return self.data.setdefault('payments', [])

    def by_txid(self, txid):
        for p in self.payments:
            if p.get('txid') == txid:
                return p
        return None

    def add_payment(self, txid, frm, amount, units, energy_ref, status,
                    note='', paid_at=0):
        p = {
            'txid': txid,
            'from': frm,
            'amount': round(float(amount), 6),
            'units': int(units),
            'energy': int(energy_ref),
            'status': status,
            'note': note,
            'paid_at': int(paid_at or 0),        # 链上时间（毫秒）
            'at': int(time.time()),              # 处理时间（秒）
        }
        self.payments.append(p)
        if len(self.payments) > MAX_PAYMENTS:
            del self.payments[:len(self.payments) - MAX_PAYMENTS]
        self.save()
        return p

    def recent(self, limit=60, status=None):
        out = self.payments if not status else [
            p for p in self.payments if p.get('status') == status]
        return list(reversed(out))[:limit]

    def need_attention(self):
        return [p for p in self.payments if p.get('status') in NEEDS_ATTENTION]

    def mark(self, payment, status, note=None):
        payment['status'] = status
        if note:
            payment['note'] = note
        self.save()
        return payment

    # ================= 地址档案 =================
    # 按「转账来源地址」记账：地址 A 一共充了多少、发了多少能量
    @property
    def addrs(self):
        return self.data.setdefault('addrs', {})

    def addr(self, frm):
        a = self.addrs.setdefault(frm, {})
        a.setdefault('trx', 0.0)        # 累计付了多少 TRX
        a.setdefault('energy', 0)       # 累计发了多少能量
        a.setdefault('count', 0)        # 付了几笔
        a.setdefault('first', datetime.now().strftime('%Y-%m-%d %H:%M'))
        a.setdefault('last', '')
        a.setdefault('quota', 0)        # 笔数套餐余量（预留）
        return a

    def credit(self, frm, trx, energy):
        a = self.addr(frm)
        a['trx'] = round(float(a['trx']) + float(trx), 6)
        a['energy'] = int(a['energy']) + int(energy)
        a['count'] = int(a['count']) + 1
        a['last'] = datetime.now().strftime('%Y-%m-%d %H:%M')
        self.save()
        return a

    # ================= 统计 =================
    def stats(self, days=1):
        """最近 N 天的汇总"""
        since = int(time.time()) - int(days * 86400)
        rows = [p for p in self.payments if int(p.get('at') or 0) >= since]
        done = [p for p in rows if p.get('status') == P_DONE]
        return {
            'count': len(rows),
            'done': len(done),
            'trx': round(sum(float(p.get('amount') or 0) for p in done), 6),
            'energy': sum(int(p.get('energy') or 0) for p in done),
            'failed': len([p for p in rows if p.get('status') == P_FAILED]),
        }

    # ================= 会员订单 =================
    @property
    def premiums(self):
        return self.data.setdefault('premiums', [])

    def premium_price(self, month):
        """某档套餐卖多少 USDT（上游按 USDT 计价，报价时再折成 TRX 收）"""
        try:
            return float((self.cfg.get('premium_prices') or {}).get(str(month)) or 0)
        except (TypeError, ValueError):
            return 0.0

    def _next_poid(self):
        n = int(self.data.get('pseq') or 1000) + 1
        self.data['pseq'] = n
        return 'P%d' % n

    def alloc_premium_amount(self, base, span=0.1):
        """会员订单金额 = 标价 + 一点点零头（最多 0.1）。

        ★ 用户要求：溢价不能超过 0.1（标价 15 就报 15.00~15.10 这种），
          别搞出 15.793031 那种把人看懵的数。
        ★ 这点零头只是为了区分「同时挂着的同价订单」；
          真正认单主要靠客户登记的自己地址（premium_match_amount）。
        """
        base = round(float(base), 6)
        used = {round(float(x.get('amount') or 0), 6)
                for x in self.premiums if x.get('status') == PR_CREATED}
        top = max(0, int(round(float(span) * 1e6)))
        for _ in range(60):
            amt = round(base + random.randint(0, top) / 1e6, 6)
            if amt not in used:
                return amt
        return base

    def auto_price(self, count):
        """某档笔数卖多少 TRX（上游只有 web 域名，所以是 TRX 计价）"""
        try:
            return float((self.cfg.get('auto_prices') or {}).get(str(count)) or 0)
        except (TypeError, ValueError):
            return 0.0

    def auto_tiers(self):
        """上架了哪几档：直接读 auto_prices 的 key，按笔数从小到大排。

        ★ 档位不写死在代码里 —— 上游只要 >= 2 笔都行，想上几档就配几档。
        """
        out = []
        for k, v in (self.cfg.get('auto_prices') or {}).items():
            try:
                c = int(k)
                p = float(v or 0)
            except (TypeError, ValueError):
                continue
            if c >= 2 and p > 0:
                out.append((c, p))
        return sorted(out)

    def add_premium(self, tg_id, username, month, amount, usdt=0.0,
                    coin='USDT', pay_addr='', kind='premium'):
        o = {
            'oid': self._next_poid(),
            'kind': kind or 'premium',   # premium=开会员 / auto=买笔数
            'tg_id': int(tg_id or 0),
            'username': (username or '').lstrip('@').strip(),
            'month': int(month or 0),
            'amount': round(float(amount), 6),      # 客户要付的金额
            'coin': (coin or 'USDT').upper(),       # 付什么币：USDT / TRX
            'usdt': round(float(usdt or 0), 4),     # 标价（USDT），方便算利润
            # 客户登记的自己地址 —— 认单主要靠它（金额可能几单同价）
            'pay_addr': (pay_addr or '').strip(),
            'status': PR_CREATED,
            'pay_txid': '',
            'provider_oid': '',
            'created': int(time.time()),
            'paid_at': 0,
            'done_at': 0,
            'note': '',
        }
        self.premiums.append(o)
        if len(self.premiums) > MAX_PREMIUMS:
            del self.premiums[:len(self.premiums) - MAX_PREMIUMS]
        self.save()
        return o

    def premium_by_oid(self, oid):
        for o in self.premiums:
            if o.get('oid') == oid:
                return o
        return None

    def premium_pending(self):
        """还挂着等付款的会员订单"""
        return [o for o in self.premiums if o.get('status') == PR_CREATED]

    def premium_recent(self, limit=50):
        return list(reversed(self.premiums))[:limit]

    def premium_need_attention(self):
        return [o for o in self.premiums
                if o.get('status') in PR_NEEDS_ATTENTION]

    def premium_match_amount(self, amount, coin='USDT', frm=''):
        """按「币种 + 金额」找待付款的会员订单，地址能对上就优先认它。

        金额不再加尾数，所以可能有几单同价同时挂着 ——
        这时候靠客户登记的自己地址来分辨是谁付的：
          · 地址对得上 → 铁定是这单
          · 只有一单同价 → 直接认（不用纠结）
          · 几单同价又认不出 → 返回 None，挂起来等人工
        """
        try:
            amt = round(float(amount), 6)
        except (TypeError, ValueError):
            return None
        frm = (frm or '').strip()
        cand = [o for o in self.premium_pending()
                if (o.get('coin') or 'USDT').upper()
                == (coin or 'USDT').upper()
                and abs(float(o.get('amount') or 0) - amt) < 0.000001]
        if not cand:
            return None
        for o in cand:
            if frm and (o.get('pay_addr') or '').strip() == frm:
                return o
        return cand[0] if len(cand) == 1 else None

    def premium_near_amount(self, amount, coin='USDT'):
        """金额「贴近」某个待付款会员订单吗？

        ★ 这是防误发能量的闸门。客户从交易所提币会被扣手续费，
          到账比订单少一点点，精确匹配就落空 —— 一旦掉进能量逻辑，
          系统会按倍数白送一大把能量，亏死。
          所以只要金额贴近某个待付款会员订单，就停住等人工确认，
          宁可让人看一眼，也不能自动发错。
        """
        try:
            amt = float(amount)
        except (TypeError, ValueError):
            return None
        for o in self.premium_pending():
            if (o.get('coin') or 'USDT').upper() != (coin or 'USDT').upper():
                continue
            want = float(o.get('amount') or 0)
            if want <= 0:
                continue
            tol = min(want * 0.015, 1.0)     # 1.5% 且最多 1 TRX
            if abs(amt - want) <= tol:
                return o
        return None

    def premium_expire_stale(self):
        """超时没付款的会员订单作废，别一直占着金额"""
        now = int(time.time())
        n = 0
        for o in self.premium_pending():
            if now - int(o.get('created') or 0) > PREMIUM_EXPIRE_MIN * 60:
                o['status'] = PR_EXPIRED
                o['note'] = '超过 %d 分钟没付款，已作废' % PREMIUM_EXPIRE_MIN
                n += 1
        if n:
            self.save()
        return n

    # ================= 倍数换算 =================
    def calc(self, trx_amount):
        """把收到的 TRX 折算成 (单位数, 能量)。

        返回 (units, energy, note)：
            units=0 表示不够一个单位，不该发
            note 非空表示有需要留意的地方（比如超过最高倍数被截断）
        """
        cfg = self.cfg
        try:
            price = float(cfg.get('price') or 0)
            per = int(cfg.get('energy') or 0)
            mx = int(cfg.get('max') or 0)
            mn = int(cfg.get('min_units') or 1)
        except (TypeError, ValueError):
            return 0, 0, '配置有误'
        if price <= 0 or per <= 0:
            return 0, 0, '单价/能量没配好'

        raw_units = int(float(trx_amount) / price)
        if raw_units < max(mn, 1):
            return 0, 0, '不足 %s TRX' % price
        units = raw_units
        note = ''
        if mx > 0 and raw_units > mx:
            units = mx
            note = ('实收 %s TRX 折合 %d 个单位，超过最高 %d 倍，'
                    '只按 %d 倍发；多出的部分需要人工处理'
                    % (trx_amount, raw_units, mx, mx))
        return units, units * per, note


# ================= 面板用的小壳子 =================
class StoreShim:
    """机器人没在跑时，面板也要能看流水、改配置。"""

    def __init__(self, mgr, bot):
        self.mgr = mgr
        self.bot = bot
        self.path = os.path.join(core.DATA_DIR, '%s.json' % bot['id'])
        data = core.load_json(self.path, {})
        self.data = data if isinstance(data, dict) else {}

    def save_data(self):
        os.makedirs(core.DATA_DIR, exist_ok=True)
        try:
            core.save_json(self.path, self.data)
        except Exception as e:
            core.log('面板保存商城数据失败：%s' % e)


def store_for(mgr, bid):
    b = mgr.find(bid)
    if not b:
        return None
    r = mgr.runners.get(bid)
    st = getattr(r, 'store', None) if r is not None else None
    if st is not None:
        return st
    return ShopStore(StoreShim(mgr, b))


# ================= 展示辅助 =================
def pay_label(p):
    return PAY_LABEL.get(p.get('status'), p.get('status') or '?')


def at_str(p):
    try:
        return datetime.fromtimestamp(int(p.get('at') or 0)).strftime('%m-%d %H:%M')
    except Exception:
        return '?'


def paid_str(p):
    try:
        return datetime.fromtimestamp(
            int(p.get('paid_at') or 0) / 1000).strftime('%m-%d %H:%M:%S')
    except Exception:
        return '?'
