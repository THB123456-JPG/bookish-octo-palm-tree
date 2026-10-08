from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from html import escape

import httpx
from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Inline

from features.shop.providers import address_valid, base58
from features.shop.store import amount, units
from features.trc20 import USDT


ZONE = timezone(timedelta(hours=8))


def when(timestamp):
    return datetime.fromtimestamp(timestamp / 1000, ZONE).strftime("%m-%d %H:%M:%S")


def button(label, action):
    return Button(label, callback_data="shop:w:" + action)


def navigation():
    return [button("📒 地址簿", "book"), Button("🏠 首页", callback_data="shop:go:home")]


async def chain_get(client, path, params=None):
    response = await client.get("https://api.trongrid.io/v1/accounts/" + path, params=params)
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict) or body.get("success") is not True or not isinstance(body.get("data"), list):
        raise ValueError("查询服务返回异常，不代表余额为零。")
    return body["data"]


def balances(rows, address):
    if not rows:
        return "未查询到已激活账户；请核对地址。"
    if len(rows) != 1 or not isinstance(rows[0], dict):
        raise ValueError("Invalid account response")
    row = rows[0]
    identity = row.get("address", "")
    if identity != address and (not isinstance(identity, str) or len(identity) != 42 or base58(identity) != address):
        raise ValueError("Account mismatch")
    values = {"TRX": row.get("balance", 0), "USDT": 0}
    tokens = row.get("trc20", [])
    if not isinstance(tokens, list) or any(not isinstance(item, dict) for item in tokens):
        raise ValueError("Invalid token balances")
    for token in tokens:
        if USDT in token:
            values["USDT"] = token[USDT]
    for value in values.values():
        if isinstance(value, bool) or not str(value).isdigit():
            raise ValueError("Invalid balance")
    return "\n".join(f"{coin}：{amount(int(value))}" for coin, value in values.items())


async def query(ui, update, context, address):
    from features.shop.payments import normalize

    if not address_valid(address):
        raise ValueError("TRON 地址校验失败，请重新填写。")
    headers = {"TRON-PRO-API-KEY": ui.store.get("chain_key")} if ui.store.get("chain_key") else {}
    params = {"only_confirmed": "true", "limit": 20, "order_by": "block_timestamp,desc", "only_transfers": "true"}
    async with httpx.AsyncClient(timeout=15, headers=headers, follow_redirects=False, trust_env=False) as client:
        results = await asyncio.gather(
            chain_get(client, address, {"only_confirmed": "true"}),
            chain_get(client, address + "/transactions/trc20", {**params, "contract_address": USDT}),
            chain_get(client, address + "/transactions", {"only_confirmed": "true", "limit": 1, "order_by": "block_timestamp,desc"}), return_exceptions=True)
    subscribed = ui.store.watched(update.effective_user.id, address)
    title = subscribed["note"] if subscribed and subscribed["note"] else "地址查询"
    lines = ["<blockquote><b>" + escape(title) + "</b>\n<code>" + address + "</code></blockquote>", "最近 20 笔 USDT 交易（已确认）："]
    account = {}
    try:
        if isinstance(results[0], Exception):
            raise ValueError
        balance_text = balances(results[0], address).replace("TRX：", "💎 TRX 余额：").replace("USDT：", "💰 USDT 余额：")
        account = results[0][0] if results[0] else {}
    except (ValueError, TypeError, OverflowError):
        balance_text = "余额暂不可用，不代表余额为零。"
    unavailable = isinstance(results[1], Exception)
    records = [] if unavailable else [tx for row in results[1] if isinstance(row, dict) and (tx := normalize(row, "USDT", address))]
    records.sort(key=lambda row: row["timestamp"], reverse=True)
    entries = []
    for tx in records[:20]:
        direction = "转入" if tx["to"] == address else "转出"
        entries.append(f"{when(tx['timestamp'])} | {direction} | {amount(tx['amount'])} USDT")
    if not records:
        entries.append("当前返回范围内暂无 USDT 记录。" if not unavailable else "USDT 交易记录查询未完成，请稍后重试。")
    lines.append("<pre>" + escape("\n".join(entries)) + "</pre>")
    lines.append(escape(balance_text))
    def date(value):
        try:
            if type(value) is not int or value <= 0:
                return "暂无数据"
            return datetime.fromtimestamp(value / 1000, ZONE).strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, OverflowError, OSError):
            return "暂无数据"
    operations = [tx["timestamp"] for tx in records]
    if isinstance(results[2], list):
        operations += [row["block_timestamp"] for row in results[2] if isinstance(row, dict) and type(row.get("block_timestamp")) is int]
    if type(account.get("latest_opration_time")) is int:
        operations.append(account["latest_opration_time"])
    lines += ["⏰ 创建时间：" + date(account.get("create_time")),
              "🕒 最近操作：" + date(max(operations, default=0)) + ("（部分接口不可用）" if unavailable or isinstance(results[2], Exception) else ""),
              "时间：UTC+8 · 仅 TRON 主网"]
    rows = [[button("管理此地址" if subscribed else "加入监听", ("detail:" if subscribed else "add:") + address)],
            [button("刷新查询", "query:" + address), Button("链上详情", url="https://tronscan.org/#/address/" + address)],
            navigation()]
    await ui.send(update, context, "\n".join(lines), Inline(rows))


async def book(ui, update, context):
    rows = ui.store.watches(update.effective_user.id)
    text = [f"📒 我的地址簿 · {len(rows)}/5 个", "仅查看自己的订阅；删除监听不会删除机器人或账本。"]
    buttons = []
    for index, row in enumerate(rows, 1):
        text.append(f"\n{index}. {row['note'] or '未备注'}\n{row['address']}")
        buttons.append([button(f"{index}. {row['note'] or row['address'][:8]}", "detail:" + row["address"])])
    buttons += [[button("添加地址", "new")], navigation()]
    await ui.send(update, context, escape("\n".join(text)), Inline(buttons))


async def detail(ui, update, context, row):
    state = "尚未完成首次同步" if not row["synced"] else "最近同步：" + when(row["synced"])
    if row["error"]:
        state += "\n本次同步失败，将重试；余额和统计不代表零。"
    text = (f"{row['note'] or '未备注'}\n{row['address']}\n\n通知币种：{row['coins']}\n"
            f"方向：{ {'both': '转入和转出', 'in': '仅转入', 'out': '仅转出'}[row['direction']]}\n"
            f"最低提醒金额：{amount(row['minimum'])}（按各币种原单位）\n"
            f"格式：{'详细' if row['detailed'] else '简洁'}\n{state}\n"
            "仅积累订阅后的已确认收支；轮询间隔约 30 秒，不承诺固定到账通知时效。")
    address = row["address"]
    rows = [[button("查询余额/记录", "query:" + address), button("账单统计", "stats:" + address)],
            [button("设置备注", "note:" + address), button("通知设置", "prefs:" + address)],
            [button("停止监听", "delete:" + address)], navigation()]
    await ui.send(update, context, escape(text), Inline(rows))


async def statistics(ui, update, context, row):
    now = int(time.time() * 1000)
    today = int(datetime.now(ZONE).replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    lines = ["收支统计 · UTC+8", row["note"] or row["address"], "仅统计订阅后已同步的转账，不是钱包完整历史。"]
    if not row["synced"]:
        lines.append("尚未完成首次同步，暂无可用统计。")
    else:
        lines.append("同步至：" + when(row["synced"]))
        if row["error"] or now - row["synced"] > 120000:
            lines.append("数据同步延迟或失败，以下可能不完整。")
        for label, start in (("近 24 小时", now - 86400000), ("今日", today)):
            lines.append(f"\n{label}（{'部分覆盖' if row['added'] > start else '订阅覆盖此区间'}）")
            with ui.store.db() as db:
                totals = {(r[0], r[1]): r[2] for r in db.execute(
                    "SELECT coin,direction,SUM(amount) FROM watch_events WHERE buyer=? AND address=? "
                    "AND timestamp>=? AND timestamp<=? GROUP BY coin,direction",
                    (row["buyer"], row["address"], max(start, row["added"]), row["synced"]))}
            for coin in ("USDT", "TRX"):
                lines.append(f"{coin} 转入 {amount(totals.get((coin, 'in'), 0))} / 转出 {amount(totals.get((coin, 'out'), 0))}")
    await ui.send(update, context, escape("\n".join(lines)), Inline([[button("返回地址", "detail:" + row["address"])], navigation()]))


async def handle(ui, update, context, action):
    buyer = update.effective_user.id
    if action.startswith("confirm:"):
        draft = context.user_data.get("shop", {})
        if (draft.get("flow") != "watch_delete" or action[8:] != draft.get("nonce")
                or time.time() - draft.get("at", 0) > 600):
            return await ui.send(update, context, "确认已过期，请从地址簿重新操作。")
        row = ui.store.watched(buyer, draft["address"])
        if row and row["revision"] == draft["revision"]:
            ui.store.watch(buyer, row["address"], False)
            await ui.send(update, context, "已停止该地址监听，并清除本地订阅统计；机器人和账本不受影响。")
        context.user_data.pop("shop", None)
        return await book(ui, update, context)
    context.user_data.pop("shop", None)
    if action == "book":
        return await book(ui, update, context)
    if action == "new":
        ui.wait(context, flow="watch")
        return await ui.send(update, context, "发送 TRON 地址开始监听；每人最多 5 个。\n只接受已确认交易，不自动补发订阅前历史。\n发送 /cancel 或点击地址簿取消。", Inline([navigation()]))
    verb, _, address = action.partition(":")
    if not address_valid(address):
        raise ValueError("地址按钮无效，请重新打开地址簿。")
    if verb == "query":
        return await query(ui, update, context, address)
    if verb == "add":
        ui.store.watch(buyer, address, True)
        await ui.send(update, context, "已添加监听，等待首次同步。查询和收款订单互不影响。")
    row = ui.store.watched(buyer, address)
    if not row:
        raise ValueError("此地址不在你的地址簿中，可能已被删除。")
    if verb in ("detail", "add"):
        return await detail(ui, update, context, row)
    if verb == "stats":
        return await statistics(ui, update, context, row)
    if verb in ("note", "minimum"):
        ui.wait(context, flow="watch_" + verb, address=address, revision=row["revision"])
        text = "发送 1～24 字备注；发送 清空 移除备注。" if verb == "note" else "发送最低提醒金额，最多 6 位小数；0 表示不过滤。此设置不影响统计及商城收款。"
        return await ui.send(update, context, text + "\n发送 /cancel 取消。", Inline([[button("返回地址", "detail:" + address)]]))
    if verb == "delete":
        ui.wait(context, flow="watch_delete", address=address, revision=row["revision"])
        nonce = context.user_data["shop"]["nonce"]
        return await ui.send(update, context, escape(f"确认停止监听？\n{row['note']}\n{address}\n仅清除该订阅及其本地统计；可重新添加，但不会恢复已清除统计。"),
                             Inline([[button("确认停止", "confirm:" + nonce), button("取消", "detail:" + address)]]))
    if verb in ("coins", "direction", "format"):
        field = "detailed" if verb == "format" else verb
        options = {"coins": ["USDT,TRX", "USDT", "TRX"], "direction": ["both", "in", "out"], "detailed": [0, 1]}[field]
        value = options[(options.index(row[field]) + 1) % len(options)]
        with ui.store.db() as db:
            db.execute(f"UPDATE watches SET {field}=? WHERE buyer=? AND address=?", (value, buyer, address))
        row[field] = value
    if verb in ("prefs", "coins", "direction", "format"):
        rows = [[button("币种：" + row["coins"], "coins:" + address)],
                [button("方向：" + {"both": "全部", "in": "转入", "out": "转出"}[row["direction"]], "direction:" + address)],
                [button("格式：" + ("详细" if row["detailed"] else "简洁"), "format:" + address)],
                [button("最低金额：" + amount(row["minimum"]), "minimum:" + address)],
                [button("返回地址", "detail:" + address)]]
        return await ui.send(update, context, "点击币种/方向/格式切换，仅影响你的此地址通知。\n始终记录两种币的收支用于统计，不改变商城付款确认。", Inline(rows))


async def input_value(ui, update, context, draft, text):
    buyer = update.effective_user.id
    if draft["flow"] == "watch":
        if not address_valid(text):
            raise ValueError("TRON 地址校验失败，请重新填写或取消。")
        return await handle(ui, update, context, "add:" + text)
    row = ui.store.watched(buyer, draft["address"])
    if not row or row["revision"] != draft["revision"]:
        raise ValueError("订阅已变化，请重新打开地址簿。")
    field = "note" if draft["flow"] == "watch_note" else "minimum"
    if field == "note":
        value = "" if text == "清空" else text
        if (not value and text != "清空") or len(value) > 24 or "\n" in value:
            raise ValueError("备注须为 1～24 字，不能换行；发送 清空 可移除。")
    else:
        value = 0 if text == "0" else units(text)
    with ui.store.db() as db:
        db.execute(f"UPDATE watches SET {field}=? WHERE buyer=? AND address=? AND revision=?",
                   (value, buyer, row["address"], row["revision"]))
    context.user_data.pop("shop", None)
    await detail(ui, update, context, ui.store.watched(buyer, row["address"]))


def alert_text(watch, tx):
    direction = "转入" if tx["to"] == watch["address"] else "转出"
    text = f"{watch['note'] or '地址监听'}\n{tx['coin']} 已确认{direction} {amount(tx['amount'])}\n监听地址：{watch['address']}"
    if watch["detailed"]:
        text += f"\n付款方：{tx['from']}\n收款方：{tx['to']}\n时间：{when(tx['timestamp'])} UTC+8\n交易：{tx['hash']}"
    return text
