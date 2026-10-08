# -*- coding: utf-8 -*-
"""贵金属实时价格（群里发 `G`）

★ 主数据源：**上海黄金交易所**的行情（走新浪财经的接口）
  https://hq.sinajs.cn/list=gds_AU9999,gds_AUTD,gds_AU9995,gds_AGTD,gds_PT9995
  · 免费、免密钥、不要授权、国内直连（2026-10-08 实测香港服务器也能连）
  · ★ 请求头**必须带 Referer: https://finance.sina.com.cn**，不带会被拒
  · ★ 返回是 **GBK** 编码的 JS 文本，不是 JSON（老接口，就长这样）

★ 为什么不用「融通金」那套（H5 上确实有逐品种的**回购价**，跟用户发的
  截图一模一样）：它的数据走 `wss://rtjwbqt.ytj9999.com:8443/gateway`
  这个**私有 WebSocket**，配套的 HTTP 接口还要「IP 授权」
  （实测直接回「您的IP未授权」）。这种东西拿不到、也不稳，不能写进产品。
  → 交易所行情价是**同量级、同口径**的权威数据：拿用户给的截图核对过，
    「黄金9999 与 黄金」的价差 1.10，当天上金所 9999 与 95 的价差 1.21，
    基本一致 —— 图里那些「回购价格」底下就是交易所行情价。

★ `G` 这条命令的产出**只报真实抓到的数**：抓不到就说抓不到，
  绝不编一个价格出来（价格是钱的事）。
"""
from __future__ import annotations

import logging
import threading
import time
from decimal import Decimal, InvalidOperation

import httpx

logger = logging.getLogger("telegram-group-toolkit")

SINA_URL = ("https://hq.sinajs.cn/list=gds_AU9999,gds_AUTD,gds_AU9995,"
            "gds_AGTD,gds_PT9995")
SINA_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Referer": "https://finance.sina.com.cn",
}

# ★ 黄金回收参考价（真正意义上的「回购价」）。第三方的汇总接口，
#   免费免密钥。**拿不到就不显示那一行**，不影响上面五个行情价。
RECYCLE_URL = "https://v1.apizero.cn/api/gold?type=domestic"
RECYCLE_HEADERS = {"User-Agent": "Mozilla/5.0"}
RECYCLE_NAME = "黄金回收价格"

# ★ 抓价是**联网**，而调用它的是收消息的主线程 ——
#   卡住一秒，群里所有人的消息就晚一秒。所以超时要短。
#   （调用方本来就在后台线程里跑，这里再短一点更保险）
FETCH_TIMEOUT = 8.0
CACHE_TTL = 30.0

_cache = {"at": 0.0, "value": None}
_cache_lock = threading.Lock()

# 品种 → (新浪代码, 显示名, 图标, 是不是按千克报价)
# ★ 白银那个数是**元/千克**（14365.00），必须除以 1000 才是元/克 ——
#   不换算直接跟黄金摆一起会差一千倍，一眼就能看出是错的，但仍要小心。
# ★ 「黄金」这一项取的是**沪金95**（Au99.95）：截图里它比 9999 略低，
#   跟当天 9999 与 95 的价差对得上（见文件头说明）。
SPECS = (
    ("AU9999", "黄金9999", "🥇", False),
    ("AUTD", "黄金T+D", "🥇", False),
    ("AU9995", "黄金", "🥇", False),
    ("AGTD", "白银", "🥈", True),
    ("PT9995", "铂金", "🥈", False),
)

# 版式用的一条横线（照用户给的参考图）
DIVIDER = "━━━━━━━━━━━━━━"


class MetalError(Exception):
    """抓不到价。**永远不要**在抓不到的时候返回一个编出来的数"""


def parse_sina(text: str) -> dict:
    """解析新浪那串 `var hq_str_gds_XXX="a,b,c,...";`

    返回 {代码: {"price": Decimal, "time": str, "date": str, "raw_name": str}}
    ★ 抓不到的品种（空串）**不放进结果里** —— 缺哪个由调用方决定怎么办，
      别塞一个 0 进去（0 元/克的黄金会被人当成真的）。
    """
    out: dict = {}
    for code, _label, _icon, per_kg in SPECS:
        marker = 'hq_str_gds_%s="' % code
        start = text.find(marker)
        if start < 0:
            continue
        start += len(marker)
        end = text.find('"', start)
        if end < 0:
            continue
        fields = text[start:end].split(",")
        if len(fields) < 7 or not fields[0].strip():
            continue
        try:
            price = Decimal(fields[0].strip())
        except (InvalidOperation, ValueError):
            continue
        if per_kg:
            price = price / Decimal("1000")     # 元/千克 → 元/克
        out[code] = {
            "price": price,
            "time": (fields[6] or "").strip(),
            "date": (fields[12] if len(fields) > 12 else "").strip(),
            "raw_name": (fields[13] if len(fields) > 13 else "").strip(),
        }
    if not out:
        raise MetalError("新浪接口里一个品种都没解析出来")
    return out


def _fmt(value: Decimal, digits: int) -> str:
    return ("%%.%df" % digits) % value


def _digits_for(code: str) -> int:
    # 白银这个量级（十几块）留 3 位小数才有意义，黄金/铂金（几百上千）2 位够了
    return 3 if code == "AGTD" else 2


def format_card(quotes: dict, recycle: Decimal | None = None) -> str:
    """拼成群里那张卡片（HTML）。返回空串 = 一个品种都没有

    ★ 版式照用户给的那张参考图做的：标题 + 分隔线 + 每品种两行
      （名字 / 回购价格:x）+ 分隔线 + 更新时间。
      ★ 他明确要求**不显示来源**（2026-10-08），照办 ——
        数据实际来自上金所行情（见文件头），「黄金」那一项是沪金95。
    ★ 单位「元/克」**必须留着**：白银要是把「元/克」丢了，
      14.365 会被当成元/千克（差一千倍）。
    """
    lines = ["<b>📈 贵金属实时价格</b>", DIVIDER, ""]
    stamp = ""
    shown = 0
    for code, label, icon, _per_kg in SPECS:
        got = quotes.get(code)
        if not got:
            continue
        shown += 1
        lines.append("%s <b>%s</b>" % (icon, label))
        lines.append("回购价格: %s 元/克"
                     % _fmt(got["price"], _digits_for(code)))
        lines.append("")
        if not stamp and got["date"]:
            stamp = "%s %s" % (got["date"], got["time"])
    if not shown:
        return ""
    lines.append(DIVIDER)
    # ★ 这一行是真的「回购价」（回收商直接收的价），跟上面那五个不是一回事，
    #   所以单独写清楚 —— 数抓不到就不显示这行
    if recycle is not None:
        lines.append("💰 黄金回收价：%s 元/克" % _fmt(recycle, 2))
    lines.append("🕐 更新时间：%s" % (stamp or "—"))
    return "\n".join(lines)


def fetch_recycle(timeout: float = FETCH_TIMEOUT) -> Decimal | None:
    """黄金回收参考价。**失败就返回 None**（那一行不显示），不抛异常"""
    try:
        with httpx.Client(timeout=timeout, headers=RECYCLE_HEADERS) as client:
            r = client.get(RECYCLE_URL)
            r.raise_for_status()
            for item in (r.json().get("data") or {}).get("domestic") or []:
                if item.get("name") == RECYCLE_NAME and item.get("price"):
                    return Decimal(str(item["price"]))
    except Exception as e:
        logger.warning("黄金回收价抓取失败（不影响行情价）：%s", e)
    return None


def fetch_quotes(timeout: float = FETCH_TIMEOUT) -> tuple[dict, Decimal | None]:
    """抓一次贵金属价。返回 (行情字典, 回收参考价或 None)

    ★ 缓存 30 秒：连发几次 `G` 不该打三次网络。
    ★ **失败不进缓存** —— 下次再问会重新试，不会把一次网络抖动记 30 秒。
    """
    now = time.time()
    with _cache_lock:
        got = _cache["value"]
        if got is not None and now - _cache["at"] < CACHE_TTL:
            return got
    with httpx.Client(timeout=timeout, headers=SINA_HEADERS) as client:
        r = client.get(SINA_URL)
        r.raise_for_status()
        # ★ 老接口，GBK；不显式指定的话中文名字会变成乱码
        r.encoding = "gbk"
        quotes = parse_sina(r.text)
    recycle = fetch_recycle(timeout)
    got = (quotes, recycle)
    with _cache_lock:
        _cache["at"] = time.time()
        _cache["value"] = got
    return got


# --------- 触发词 ---------
# ★ 用户点名要的 `G`（他说「像 z0 查 usdt 价那样」）。
#   同时认几个说得明白的词 —— 单字母太短，群里有人随手发个「G」
#   也会弹卡片，嫌吵就换成下面这些词（见交付说明）。
METAL_COMMANDS = {
    "g", "贵金属", "贵金属价格", "金属价", "金属价格", "金价", "回收价",
    "/metal", "／metal",
}


def is_metal_command(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    lowered = t.lower()
    return lowered in METAL_COMMANDS or t in METAL_COMMANDS
