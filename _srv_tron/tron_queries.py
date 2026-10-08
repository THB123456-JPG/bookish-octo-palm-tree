
from __future__ import annotations

import re
import time
from decimal import Decimal

import httpx

from features.shop.providers import ALPHABET, address_valid, base58
from features.shop.store import amount
from features.shop.watch import when
from features.trc20 import USDT


TRANSFER = "ddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def address_hex(address):
    if not address_valid(address):
        raise ValueError("TRON 地址校验失败。")
    value = 0
    for char in address:
        value = value * 58 + ALPHABET.index(char)
    return value.to_bytes(25, "big")[:21].hex()


async def node(key, method, params):
    if method not in ("gettransactionbyid", "gettransactioninfobyid", "triggerconstantcontract") or not key:
        raise ValueError("主网查询尚未配置。")
    async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
        response = await client.post("https://api.trongrid.io/walletsolidity/" + method,
            headers={"TRON-PRO-API-KEY": key}, json=params)
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict) or any(field in data for field in ("Error", "error")):
            raise ValueError("主网接口暂不可用，不能视为查询结果。")
        return data


async def blacklist(key, address):
    parameter = address_hex(address)[2:].rjust(64, "0")
    data = await node(key, "triggerconstantcontract", {"owner_address": address,
        "contract_address": USDT, "function_selector": "isBlackListed(address)",
        "parameter": parameter, "visible": True})
    values = data.get("constant_result")
    if (data.get("result", {}).get("result") is not True or not isinstance(values, list)
            or len(values) != 1 or not isinstance(values[0], str)
            or not re.fullmatch(r"0{63}[01]", values[0])):
        raise ValueError("USDT 冻结状态查询失败，不代表未冻结。")
    if any(item.get("ret", "SUCCESS") != "SUCCESS" for item in data.get("transaction", {}).get("ret", [])):
        raise ValueError("合约执行未成功，不能判断冻结状态。")
    return values[0].endswith("1")


async def transaction(key, txid):
    if not re.fullmatch(r"[0-9a-fA-F]{64}", txid):
        raise ValueError("请输入 64 位交易哈希。")
    txid = txid.lower()
    body = await node(key, "gettransactionbyid", {"value": txid})
    if not body:
        return "主网暂未查到已确认交易，可能未确认、哈希错误或网络不符；不能据此认定失败。"
    info = await node(key, "gettransactioninfobyid", {"value": txid})
    if body.get("txID") != txid or info.get("id") != txid:
        raise ValueError("交易回执未完整匹配，请稍后重试。")
    results = body.get("ret", [])
    success = bool(results) and all(item.get("contractRet") == "SUCCESS" for item in results)
    failed = any(item.get("contractRet") not in (None, "SUCCESS") for item in results)
    receipt_state = info.get("result") or info.get("receipt", {}).get("result")
    if receipt_state and receipt_state != "SUCCESS":
        success, failed = False, True
    lines = ["TRON 主网已确认交易", txid, "执行：" + ("成功" if success else "失败" if failed else "结果待核对")]
    if isinstance(info.get("blockTimeStamp"), int):
        lines.append("时间：" + when(info["blockTimeStamp"]) + " UTC+8")
    if type(info.get("fee", 0)) is int and info.get("fee", 0) >= 0:
        lines.append("链上费用：" + amount(info.get("fee", 0)) + " TRX")
    if success:
        for contract in body.get("raw_data", {}).get("contract", []):
            if contract.get("type") == "TransferContract":
                fields = contract["parameter"]["value"]
                lines.append(f"TRX {amount(int(fields['amount']))}\n付款：{base58(fields['owner_address'])}\n收款：{base58(fields['to_address'])}")
        for log in info.get("log", []):
            topics = log.get("topics", [])
            if log.get("address", "").lower() != address_hex(USDT)[2:] or len(topics) != 3 or topics[0].lower() != TRANSFER:
                continue
            if not all(re.fullmatch(r"0{24}[0-9a-fA-F]{40}", topic) for topic in topics[1:]) or not re.fullmatch(r"[0-9a-fA-F]{64}", log.get("data", "")):
                raise ValueError("USDT 转账日志格式异常，请查看链上原始回执。")
            lines.append(f"USDT {amount(int(log['data'], 16))}\n付款：{base58('41' + topics[1][-40:])}\n收款：{base58('41' + topics[2][-40:])}")
    lines.append("仅解析 TRX 原生转账与官方 USDT 转账日志；其他合约操作请查看链上详情。")
    text = "\n".join(lines)
    return text if len(text) <= 3500 else text[:3300] + "\n记录较多，未完整展示；请查看链上详情。"


class Prices:
    def __init__(self):
        self.cache = {}

    async def get(self, symbol):
        symbol = symbol.upper()
        if not re.fullmatch(r"[A-Z0-9]{2,12}", symbol):
            raise ValueError("请输入币种代码，例如 BTC、ETH、TRX。")
        if symbol == "USDT":
            return "本功能以 USDT 为报价单位，不提供 USDT/人民币场外报价，也不把 USDT 当作恒定美元价格。"
        cached = self.cache.get(symbol)
        if cached and time.time() - cached[0] < 60:
            return cached[1]
        async with httpx.AsyncClient(timeout=12, follow_redirects=False, trust_env=False) as client:
            response = await client.get("https://data-api.binance.vision/api/v3/ticker/24hr", params={"symbol": symbol + "USDT"})
            response.raise_for_status()
            data = response.json()
        if data.get("symbol") != symbol + "USDT" or not isinstance(data.get("closeTime"), int) or not -60 <= time.time() - data["closeTime"] / 1000 <= 300:
            raise ValueError("行情数据过期或不匹配，请稍后查询。")
        price = Decimal(str(data.get("lastPrice")))
        if not price.is_finite() or price <= 0:
            raise ValueError("行情数据异常。")
        text = f"{symbol}/USDT：{price:f}\n来源：Binance 公开现货行情\n行情时间：{when(data['closeTime'])} UTC+8\n缓存最多 60 秒；仅供查询，不是成交承诺。"
        if len(self.cache) >= 128:
            self.cache.pop(next(iter(self.cache)))
        self.cache[symbol] = (time.time(), text)
        return text
