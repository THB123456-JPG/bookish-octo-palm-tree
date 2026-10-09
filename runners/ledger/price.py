from __future__ import annotations

import logging
import threading
import time
from decimal import Decimal

import httpx

from .calculator import format_calc_result


logger = logging.getLogger("telegram-group-toolkit")
OKX_C2C_USDT_CNY_URL = (
    "https://www.okx.com/v3/c2c/tradingOrders/books"
    "?quoteCurrency=cny&baseCurrency=usdt&side=sell&paymentMethod=all&userType=all&showTrade=false"
)
OKX_EXCHANGE_RATE_URL = "https://www.okx.com/api/v5/market/exchange-rate"
OKX_HTTP_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

# ★ 抓价是**联网**，而调用它的地方是「收消息的主线程」——
#   卡住一秒，群里所有人的消息就晚一秒。所以：
#     · 超时短（8 秒）：宁可报失败，也不能把记账拖住
#     · 结果缓存 30 秒：连发几次「bj」不该打三次网络
FETCH_TIMEOUT = 8.0
CACHE_TTL = 30.0

_cache = {"at": 0.0, "value": None}
_cache_lock = threading.Lock()


def parse_okx_c2c_usdt_cny_prices(payload: dict, limit: int = 5) -> list[Decimal]:
    sell_orders = payload.get("data", {}).get("sell", [])
    if not sell_orders:
        raise ValueError("empty OKX C2C sell order book")
    prices: list[Decimal] = []
    for order in sell_orders:
        price = order.get("price")
        if price is None:
            continue
        prices.append(Decimal(str(price)))
        if len(prices) >= limit:
            break
    if not prices:
        raise ValueError("missing OKX C2C prices")
    return prices


def parse_okx_exchange_rate_price(payload: dict) -> Decimal:
    data = payload.get("data", [])
    if not data:
        raise ValueError("empty OKX exchange-rate data")
    price = data[0].get("usdCny")
    if price is None:
        raise ValueError("missing OKX usdCny")
    return Decimal(str(price))


def format_okx_prices(prices: list[Decimal], source: str) -> str:
    lines = ["欧意USDT/CNY 最新5档"]
    lines.extend(f"{index}. {format_calc_result(price)}" for index, price in enumerate(prices, start=1))
    lines.append(f"来源：{source}")
    return "\n".join(lines)


def is_price_command(text: str) -> bool:
    normalized = text.strip()
    lowered = normalized.lower()
    return normalized == "币价" or lowered in {"bj", "z0"}


def is_realtime_rate_command(text: str) -> bool:
    return text.strip() == "设置实时汇率"


def fetch_prices(timeout: float = FETCH_TIMEOUT) -> tuple[list[Decimal], str]:
    """同步抓一次欧意价。返回 (价格列表, 来源)。

    ★★ 为什么是**同步**的（原来那版是 async 的，一直是死代码）：
       记账这边的代码从头到尾是同步的，`await` 根本没法往里塞 ——
       上一版把 async 的函数原样搬过来，结果**没有任何地方能调它**，
       于是「币价」「设置实时汇率」两个说明书上写着的功能全是废的。

    ★ 先试 C2C 卖单（那才是**真实成交价**，跟换 U 的价对得上）；
      挂了再退回官方 USD/CNY 汇率（那个只是参考价，会标出来）。
    ★ 失败**不缓存** —— 下次再问会重新试，不会把一次网络抖动记 30 秒。
    """
    now = time.time()
    with _cache_lock:
        got = _cache["value"]
        if got is not None and now - _cache["at"] < CACHE_TTL:
            return got
    with httpx.Client(timeout=timeout, headers=OKX_HTTP_HEADERS) as client:
        try:
            r = client.get(OKX_C2C_USDT_CNY_URL)
            r.raise_for_status()
            got = (parse_okx_c2c_usdt_cny_prices(r.json(), limit=5),
                   "OKX C2C卖单")
        except Exception as e:
            logger.warning("欧意 C2C 抓取失败，退回官方汇率：%s", e)
            r = client.get(OKX_EXCHANGE_RATE_URL)
            r.raise_for_status()
            got = ([parse_okx_exchange_rate_price(r.json())],
                   "OKX官方USD/CNY汇率")
    with _cache_lock:
        _cache["at"] = time.time()
        _cache["value"] = got
    return got
