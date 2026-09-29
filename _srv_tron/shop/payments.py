from __future__ import annotations

import asyncio
import logging
import time

import httpx
from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Inline

from features.shop.providers import ShopProviders, ProviderError, base58
from features.shop.store import STATES, amount
from features.shop.watch import alert_text
from features.trc20 import USDT


logger = logging.getLogger(__name__)


def normalize(row, coin, address):
    
    try:
        if coin == "USDT":
            if row.get("type") != "Transfer" or row.get("token_info", {}).get("address") != USDT:
                return None
            if int(row["token_info"]["decimals"]) != 6:
                return None
            sender, receiver = row["from"], row["to"]
            value, txid = int(row["value"]), row["transaction_id"]
        else:
            if not row.get("ret") or any(item.get("contractRet") != "SUCCESS" for item in row["ret"]):
                return None
            contracts = row["raw_data"]["contract"]
            if len(contracts) != 1 or contracts[0]["type"] != "TransferContract":
                return None
            data = contracts[0]["parameter"]["value"]
            sender, receiver = base58(data["owner_address"]), base58(data["to_address"])
            value, txid = int(data["amount"]), row["txID"]
        if address not in (sender, receiver) or sender == receiver or value <= 0:
            return None
        return {"id": coin + ":" + txid, "hash": txid, "coin": coin, "from": sender, "to": receiver,
                "amount": value, "timestamp": int(row["block_timestamp"])}
    except (ValueError, KeyError, TypeError, OverflowError):
        return None


async def transfers(address, coin, since, api_key=""):
    endpoint = f"https://api.trongrid.io/v1/accounts/{address}/transactions" + ("/trc20" if coin == "USDT" else "")
    until = int(time.time() * 1000)
    params = {"only_confirmed": "true", "limit": 200, "order_by": "block_timestamp,asc",
              "min_timestamp": since, "max_timestamp": until}
    if coin == "USDT":
        params["contract_address"] = USDT
    headers = {"TRON-PRO-API-KEY": api_key} if api_key else {}
    records = []
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        for _ in range(100):
            response = await client.get(endpoint, params=params, headers=headers)
            response.raise_for_status()
            body = response.json()
            if body.get("success") is not True or not isinstance(body.get("data"), list):
                raise ValueError("Chain query unavailable")
            for item in body["data"]:
                tx = normalize(item, coin, address)
                if tx and since <= tx["timestamp"] <= until:
                    records.append(tx)
            fingerprint = body.get("meta", {}).get("fingerprint")
            if not fingerprint:
                return records, max((tx["timestamp"] for tx in records), default=since)
            if fingerprint == params.get("fingerprint"):
                raise ValueError("Chain pagination did not advance")
            params["fingerprint"] = fingerprint
    raise ValueError("Chain backlog exceeds one scan; cursor preserved")


async def notify(bot, owner, order):
    text = f"订单 {order['id']} · {STATES[order['state']]}\n{order['note']}"
    for target in {owner, order["buyer"]}:
        if target:
            try:
                await bot.send_message(target, text)
            except Exception:
                logger.warning("Shop notification unavailable; order status remains available")


async def dispatch(store, provider, order):
    if not store.transition(order["id"], "paid", "sending", note="上游交付请求处理中"):
        return
    try:
        oid, status = await provider.deliver(order)
    except Exception:
        
        store.transition(order["id"], "sending", "review", note="交付结果待核对，请联系商户。系统不会自动重复下单。")
        return
    store.transition(order["id"], "sending", status, provider_id=oid,
                     note=("上游已确认交付" if oid else "上游确认笔数入账；该接口未提供独立订单号") if status == "done" else "请等待上游处理或联系商户核对")


async def tick(store, bot, owner, provider=None):
    provider = provider or ShopProviders(store)
    now = int(time.time() * 1000)
    with store.db() as db:
        db.execute("UPDATE orders SET state='expired' WHERE state='pending' AND expires<?", (now,))
        cursors = [dict(row) for row in db.execute("SELECT * FROM cursors")]
    for cursor in cursors:
        try:
            since = max(store.get("scan_start:" + cursor["address"], cursor["timestamp"]), cursor["timestamp"] - 600000)
            rows, until = await transfers(cursor["address"], cursor["coin"], since, store.get("chain_key", ""))
            for tx in rows:
                if tx["to"] != cursor["address"]:
                    continue
                with store.db() as db:
                    existed = db.execute("SELECT 1 FROM receipts WHERE id=?", (tx["id"],)).fetchone()
                oid = store.receive(tx)
                if oid:
                    await notify(bot, owner, store.order(oid))
                elif not existed:
                    try:
                        await bot.send_message(owner, f"收到未匹配订单的 {amount(tx['amount'])} {tx['coin']}，未自动交付，请核对。\n交易：{tx['hash']}")
                    except Exception:
                        logger.warning("Shop unmatched payment notification unavailable")
            with store.db() as db:
                db.execute("UPDATE cursors SET timestamp=MAX(timestamp,?) WHERE address=? AND coin=?", (until, cursor["address"], cursor["coin"]))
        except Exception:
            logger.warning("Shop confirmed-payment scan unavailable; cursor preserved")
    for order in store.orders(states=("paid",)):
        await dispatch(store, provider, order)
        await notify(bot, owner, store.order(order["id"]))
    for order in store.orders(states=("processing",)):
        if order["service"] != "premium":
            continue
        try:
            data = await provider.call("/premiumstatus", {"orderId": order["provider_id"]}, premium=True)
            if str(data.get("orderId")) != order["provider_id"]:
                continue
            status = {1: "done", 2: "review"}.get(data.get("status"))
            if status and store.transition(order["id"], "processing", status, provider_id=order["provider_id"], note="上游已确认开通" if status == "done" else "上游开通失败，请联系商户处理"):
                await notify(bot, owner, store.order(order["id"]))
        except (ProviderError, ValueError, TypeError, AttributeError):
            logger.warning("Shop premium status query unavailable; no resubmission")


async def scan_watches(store, bot):
    for watch in store.watches():
        try:
            scan_started = int(time.time() * 1000)
            
            pages = await asyncio.gather(*(transfers(watch["address"], coin,
                max(watch["added"], watch["since"] - 600000), store.get("chain_key", "")) for coin in ("USDT", "TRX")))
            rows = sorted((tx for records, _ in pages for tx in records), key=lambda tx: tx["timestamp"])
            for tx in rows:
                current = store.watched(watch["buyer"], watch["address"])
                if not current or current["revision"] != watch["revision"]:
                    break
                watch = current
                if tx["timestamp"] < watch["added"]:
                    continue
                direction = "in" if tx["to"] == watch["address"] else "out"
                with store.db() as db:
                    old = db.execute("SELECT notified FROM watch_events WHERE buyer=? AND address=? AND txid=?",
                                     (watch["buyer"], watch["address"], tx["id"])).fetchone()
                    db.execute("INSERT OR IGNORE INTO watch_events(buyer,address,txid,timestamp,coin,direction,amount) VALUES(?,?,?,?,?,?,?)",
                               (watch["buyer"], watch["address"], tx["id"], tx["timestamp"], tx["coin"], direction, tx["amount"]))
                if old and old[0]:
                    continue
                if (tx["coin"] in watch["coins"].split(",") and tx["amount"] >= watch["minimum"]
                        and watch["direction"] in ("both", direction)):
                    markup = Inline([[Button("链上交易详情", url="https://tronscan.org/#/transaction/" + tx["hash"]),
                                      Button("账单统计", callback_data="shop:w:stats:" + watch["address"])]])
                    await bot.send_message(watch["buyer"], alert_text(watch, tx), reply_markup=markup)
                with store.db() as db:
                    db.execute("UPDATE watch_events SET notified=1 WHERE buyer=? AND address=? AND txid=?",
                               (watch["buyer"], watch["address"], tx["id"]))
            with store.db() as db:
                db.execute("UPDATE watches SET since=MAX(since,?),synced=?,error='' WHERE buyer=? AND address=? AND revision=?",
                           (max((until for _, until in pages), default=watch["since"]), scan_started,
                            watch["buyer"], watch["address"], watch["revision"]))
                
                db.execute("DELETE FROM watch_events WHERE buyer=? AND address=? AND timestamp<? AND notified=1",
                           (watch["buyer"], watch["address"], min(scan_started - 172800000,
                            max(watch["since"], max(until for _, until in pages)) - 600000)))
        except Exception:
            with store.db() as db:
                db.execute("UPDATE watches SET error='同步暂不可用' WHERE buyer=? AND address=? AND revision=?",
                           (watch["buyer"], watch["address"], watch["revision"]))
            logger.warning("Shop address alert scan unavailable; cursor preserved")


async def payment_loop(ui, bot):
    while True:
        try:
            await tick(ui.store, bot, ui.owner)
        except Exception:
            logger.warning("Shop worker unavailable; will retry read-only scans")
        await asyncio.sleep(30)


async def watch_loop(ui, bot):
    while True:
        try:
            await scan_watches(ui.store, bot)
        except Exception:
            logger.warning("Shop address alert worker unavailable")
        await asyncio.sleep(30)


async def run(ui, bot):
    
    with ui.store.db() as db:
        db.execute("UPDATE orders SET state='review',note='服务重启前交付结果不明，请人工核对' WHERE state='sending'")
    
    await asyncio.gather(payment_loop(ui, bot), watch_loop(ui, bot))
