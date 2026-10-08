from __future__ import annotations

import hashlib
import json
import re
import time
from collections import deque

import httpx


ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def address_valid(value: str) -> bool:
    if not re.fullmatch(r"T[1-9A-HJ-NP-Za-km-z]{33}", value):
        return False
    number = 0
    for char in value:
        number = number * 58 + ALPHABET.index(char)
    raw = number.to_bytes(25, "big")
    return raw[0] == 65 and hashlib.sha256(hashlib.sha256(raw[:-4]).digest()).digest()[:4] == raw[-4:]


def base58(hex_address: str) -> str:
    raw = bytes.fromhex(hex_address)
    if len(raw) != 21 or raw[0] != 65:
        raise ValueError("Invalid TRON address")
    raw += hashlib.sha256(hashlib.sha256(raw).digest()).digest()[:4]
    number, output = int.from_bytes(raw, "big"), ""
    while number:
        number, remainder = divmod(number, 58)
        output = ALPHABET[remainder] + output
    return output


class ProviderError(Exception):
    pass


class ApiTrx:
    
    def __init__(self, store):
        self.store = store

    def ready(self, service: str) -> bool:
        key = "premium_key" if service == "premium" else "energy_key"
        return service in ("energy", "smart", "real", "premium") and bool(self.store.get(key))

    async def call(self, path: str, params=None, *, premium=False, post=False):
        key = self.store.get("premium_key" if premium else "energy_key", "")
        if not key:
            raise ProviderError("上游尚未配置。")
        base = "https://gift.apitrx.com" if premium else "https://web.apitrx.com"
        data = {**(params or {}), "apikey": key}
        try:
            async with httpx.AsyncClient(timeout=25, follow_redirects=False, trust_env=False) as client:
                response = await (client.post(base + path, json=data) if post else client.get(base + path, params=data))
                response.raise_for_status()
                body = response.json()
                if not isinstance(body, dict) or body.get("code") != 200:
                    raise ProviderError("上游未确认成功，请到上游后台核对；不会自动重试。")
                return body["data"]
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            
            raise ProviderError("上游响应异常或超时，结果待核对；不会自动重试。") from None

    async def deliver(self, order: dict) -> tuple[str, str]:
        details, service = json.loads(order["details"]), order["service"]
        if service == "energy":
            data = await self.call("/getenergy", {"add": order["destination"], "value": details["quantity"],
                                                 "hour": details["hours"], "traceId": order["id"]})
            oid, status = data.get("txid"), "done"
        elif service in ("smart", "real"):
            data = await self.call("/auto", {"add": order["destination"], "count": details["quantity"],
                                            "autoType": 1 if service == "smart" else 0})
            oid, status = data.get("orderId"), "done"
        elif service == "premium":
            data = await self.call("/premium", {"username": order["destination"], "month": str(details["quantity"])}, premium=True, post=True)
            oid = data.get("orderId")
            status = {0: "processing", 1: "done", 2: "review"}.get(data.get("status"), "review")
        else:
            raise ProviderError("该兑换服务尚未接入自动打款上游。")
        if not oid:
            raise ProviderError("上游未返回交付单号，结果待核对。")
        return str(oid), status

    async def remain(self, address: str) -> str:
        rows = await self.call("/list", {"receiver": address, "current": 1, "pageSize": 20}, post=True)
        if not isinstance(rows, list):
            raise ProviderError("上游笔数查询格式异常。")
        if not rows:
            return "该地址在本商户上游账户下暂无笔数订单。"
        return "最近 20 个笔数订单：\n" + "\n".join(
            f"订单 {row.get('orderId', '-')} · 类型 {row.get('orderType', '-')} · 剩余 {row.get('remainingCount', '-')} 笔" for row in rows[:20])


class Kuaizu:
    
    def __init__(self, store):
        self.store = store
        self.remaining_queries = deque()

    def ready(self, service):
        return service in ("energy", "smart") and bool(self.store.get("kuaizu_key"))

    async def call(self, path, params=None):
        if path not in ("/rent", "/ai", "/balance", "/bs/remaid"):
            raise ProviderError("未接入此快租接口，未提交请求。")
        key = self.store.get("kuaizu_key", "")
        if not key:
            raise ProviderError("尚未配置快租密钥。")
        if path == "/bs/remaid":
            now = time.monotonic()
            while self.remaining_queries and now - self.remaining_queries[0] >= 60:
                self.remaining_queries.popleft()
            if len(self.remaining_queries) >= 20:
                raise ProviderError("快租笔数查询较频繁，请一分钟后重试。")
            self.remaining_queries.append(now)
        try:
            async with httpx.AsyncClient(timeout=25, follow_redirects=False, trust_env=False) as client:
                response = await client.post("https://api.kuaizu.io/api" + path,
                                             json={**(params or {}), "apiKey": key})
                response.raise_for_status()
                body = response.json()
                if not isinstance(body, dict) or type(body.get("code")) is not int or body["code"] != 1:
                    raise ProviderError("快租未确认成功，请检查密钥、IP 白名单和账户余额；不会自动重试付费请求。")
                if not isinstance(body.get("data"), dict):
                    raise ProviderError("快租回执格式异常，结果待核对；不会自动重试。")
                return body["data"]
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise ProviderError("快租响应异常或超时，结果待核对；不会自动重试付费请求。") from None

    async def deliver(self, order):
        details, service = json.loads(order["details"]), order["service"]
        if service == "energy":
            if details["hours"] not in (0.25, 1):
                raise ProviderError("快租仅支持 15 分钟或 1 小时；未提交订单。")
            data = await self.call("/rent", {"resType": "ENERGY", "payNums": details["quantity"],
                "rentTime": 15 if details["hours"] == 0.25 else 1, "receiveAddress": order["destination"]})
            if not data.get("orderId") or not re.fullmatch(r"[a-fA-F0-9]{64}", str(data.get("hash", ""))):
                raise ProviderError("快租未返回完整能量交付回执，请人工核对。")
            return str(data["orderId"]), "done"
        if service == "smart":
            data = await self.call("/ai", {"payNums": details["quantity"], "receiveAddress": order["destination"]})
            if (data.get("address") != order["destination"] or isinstance(data.get("remaid_bs"), bool)
                    or not str(data.get("remaid_bs", "")).isdigit()):
                raise ProviderError("快租笔数回执无法核对，请人工核对。")
            
            return "", "done"
        raise ProviderError("快租文档未提供此服务下单协议，未提交请求。")

    async def remain(self, address):
        data = await self.call("/bs/remaid", {"receiveAddress": address})
        if data.get("address") != address:
            raise ProviderError("快租笔数查询地址不符，请稍后重试。")
        for field in ("remaid_bs", "remaid_ai"):
            if isinstance(data.get(field), bool) or not str(data.get(field, "")).isdigit():
                raise ProviderError("快租笔数查询格式异常，不能视为零笔数。")
        
        state = lambda value: value if value in ("正常", "停止") else "请到快租后台核对"
        return (f"快租真实笔数：{data['remaid_bs']} · {state(data.get('bs_status'))}\n"
                f"快租智能笔数：{data['remaid_ai']} · {state(data.get('ai_status'))}\n"
                "这里只查询余额，不能据此确认某个订单是否已交付。")


class ShopProviders:
    def __init__(self, store):
        self.store = store
        self.apitrx, self.kuaizu = ApiTrx(store), Kuaizu(store)

    def selected(self, service):
        return self.store.get("provider:" + service, "apitrx")

    def client(self, name):
        if name not in ("apitrx", "kuaizu"):
            raise ProviderError("未知上游，未提交请求。")
        return self.apitrx if name == "apitrx" else self.kuaizu

    def ready(self, service):
        return self.client(self.selected(service)).ready(service)

    def valid_prices(self, service, prices):
        if service != "energy":
            return True
        hours = (0.25, 1) if self.selected(service) == "kuaizu" else (1, 24, 72, 168, 336, 720)
        return all(tier["hours"] in hours for tier in prices)

    async def call(self, *args, **kwargs):
        return await self.apitrx.call(*args, **kwargs)

    async def deliver(self, order):
        name = json.loads(order["details"]).get("provider", "apitrx")
        return await self.client(name).deliver(order)

    async def remain(self, address):
        results = []
        for name in sorted({self.selected(service) for service in ("smart", "real")}):
            try:
                results.append(await self.client(name).remain(address))
            except ProviderError as error:
                results.append(("快租" if name == "kuaizu" else "ApiTrx") + "：" + str(error))
        return "\n\n".join(results)
