from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from html import escape
from types import SimpleNamespace

import httpx
from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Inline, ReplyKeyboardMarkup
from telegram.error import TelegramError
from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler, filters

from config.settings import load_settings
from features.shop import watch
from features.shop.payments import scan_watches
from features.shop.providers import address_valid
from features.shop.store import ShopStore
from features.tron_queries import Prices, blacklist, node, transaction


ADMIN = "⚙️ 设置"
MENU = {"💰 地址查询": "query", "➕ 添加地址": "new", "📒 地址簿": "book", "🔎 交易查询": "tx",
        "🧊 冻结查询": "blacklist", "📊 币价查询": "price", "📖 使用说明": "help"}
TEST_ADDRESS = "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb"


class MonitorUI:
    def __init__(self, store, owner):
        self.store, self.owner = store, owner
        self.prices = Prices()
        with self.store.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS freeze_watches(buyer INTEGER,address TEXT,revision TEXT,state INTEGER,checked INTEGER DEFAULT 0,error TEXT DEFAULT '',PRIMARY KEY(buyer,address))")

    async def entry(self, handler, update, context):
        chat = update.effective_chat
        if not chat or chat.type not in ("private", "group", "supergroup") or not update.effective_user:
            return
        message = update.message
        data = update.callback_query.data if update.callback_query else ""
        text = (message.text or "").strip() if message else ""
        scoped = SimpleNamespace(bot=context.bot, args=getattr(context, "args", []),
            user_data=context.user_data.setdefault("monitor:" + str(chat.id), {}))
        if chat.type != "private":
            command = text.split()[0].split("@")[0] if text else ""
            public = (data.startswith(("shop:w:query:", "monitor:blacklist:")) or data == "shop:go:home"
                or command in ("/start", "/help", "/cancel", "/query", "/tx", "/price", "/blacklist")
                or address_valid(text) or bool(re.fullmatch(r"[a-fA-F0-9]{64}", text))
                or text.upper() in ("TRX", "BTC", "ETH", "USDT"))
            if not public:
                if not data and not command.startswith("/") and text not in MENU and not scoped.user_data.get("shop"):
                    return
                try:
                    if update.effective_user.is_bot or (message and message.sender_chat):
                        raise ValueError
                    bot_member = await context.bot.get_chat_member(chat.id, context.bot.id)
                    member = await context.bot.get_chat_member(chat.id, update.effective_user.id)
                    if bot_member.status not in ("administrator", "creator") or member.status not in ("administrator", "creator"):
                        raise ValueError
                except Exception:
                    if update.callback_query:
                        await update.callback_query.answer("仅群管理员可操作；请先将机器人设为管理员。", show_alert=True)
                    else:
                        await self.send(update, context, "仅群管理员可管理群监听；请先将机器人设为管理员。匿名管理员请改用本人身份操作。")
                    return
            update = SimpleNamespace(effective_chat=chat, effective_user=SimpleNamespace(id=chat.id),
                                     message=message, callback_query=update.callback_query)
        await handler(update, scoped)

    def owner_is(self, update):
        return bool(update.effective_user and update.effective_user.id == self.owner
                    and update.effective_chat and update.effective_chat.type == "private")

    async def send(self, update, context, text, markup=None):
        
        for old, new in (("查询和收款订单互不影响。", "只通知订阅后的已确认转账。"),
                         ("不改变商城付款确认。", "不改变收支统计。"),
                         ("此设置不影响统计及商城收款。", "此设置不影响收支统计。")):
            text = text.replace(old, new)
        if update.effective_chat.type != "private":
            text = text.replace("每人最多", "每群最多").replace("我的地址簿", "本群地址簿").replace("仅查看自己的订阅", "群管理员管理本群订阅")
        if isinstance(markup, Inline):
            rows = [list(row) for row in markup.inline_keyboard]
            query_address = next((button.callback_data.removeprefix("shop:w:query:") for row in rows for button in row
                                  if button.callback_data and button.callback_data.startswith("shop:w:query:")), None)
            if query_address and text.startswith("<blockquote>"):
                rows.insert(-1, [Button("查询 USDT 冻结状态", callback_data="monitor:blacklist:" + query_address)])
            address = next((button.callback_data.removeprefix("shop:w:delete:") for row in rows for button in row
                            if button.callback_data and button.callback_data.startswith("shop:w:delete:")), None)
            if address:
                rows.insert(-1, [Button("USDT 冻结预警设置", callback_data="monitor:freeze:" + address)])
                markup = Inline(rows)
        await context.bot.send_message(update.effective_chat.id, text, parse_mode="HTML",
                                       reply_markup=markup, disable_web_page_preview=True)

    def wait(self, context, **values):
        context.user_data["shop"] = {"at": time.time(), "nonce": secrets.token_hex(4), **values}

    async def ready(self, update, context):
        if self.store.get("chain_key"):
            return True
        await self.send(update, context, "查询服务尚未配置，请联系机器人主人。" +
                        ("\n发送 /设置 查看 TronGrid API Key 配置步骤。" if self.owner_is(update) else ""))
        return False

    async def home(self, update, context):
        if self.owner_is(update):
            self.store.set("setup_active", False)
        context.user_data.pop("shop", None)
        if update.effective_chat.type != "private":
            return await self.send(update, context, "TRON 群查询与监听\n所有成员可 /query 地址、/tx 哈希、/price TRX。群监听仅群管理员可管理，提醒发到本群。\n机器人需为群管理员；API Key 由主人在私聊 /设置 填写。",
                Inline([[watch.button("添加群监听", "new"), watch.button("群地址簿", "book")]]))
        labels = list(MENU)
        rows = [labels[index:index+2] for index in range(0, len(labels), 2)]
        if self.owner_is(update):
            rows.append([ADMIN])
        await self.send(update, context, "TRON 查询与监听\n直接发送地址查询 USDT/TRX 余额及最近转账；查询不会自动订阅。\n添加监听后可设置备注、币种、方向、最低金额和通知格式。",
                        ReplyKeyboardMarkup(rows, resize_keyboard=True))

    async def manage(self, update, context, step=None):
        if not self.owner_is(update):
            return
        if step is None:
            step = self.store.get("setup_step", 0)
        if not isinstance(step, int) or not 0 <= step < 4:
            return
        self.store.set("setup_active", step < 3)
        context.user_data.pop("shop", None)
        self.store.set("setup_step", step)
        configured = "已填写（不代表检测通过）" if self.store.get("chain_key") else "未填写"
        steps = [
            ("申请主网 API Key", "登录 https://www.trongrid.io，在控制台创建主网 API Key。不是钱包私钥，不需要助记词。\n"
             "权限需包含账户、TRX 交易、TRC20 交易，以及 walletsolidity 下 gettransactionbyid、gettransactioninfobyid、triggerconstantcontract。"
             "合约只读查询需允许官方 USDT 合约。\n"
             "现在直接粘贴你的 API Key，不要加命令、网址或引号。保存后会进入检测。请求额度以 TronGrid 控制台为准。密钥不回显；若原消息未删除请手动删除。Telegram 普通私聊非端到端加密。",
             []),
            ("保存 API Key", "直接粘贴 TronGrid API Key 即可，不需要输入命令，不要带网址或引号。\n"
             "清除用 /tronkey clear，会暂停查询监听但保留订阅。\n"
             "密钥不回显；设置消息会尝试删除，审计中脱敏。Telegram 普通私聊不是端到端加密；若原消息仍在请手动删除。"
             "不要发送钱包私钥或助记词。", []),
            ("只读检测接口", "点击下方检测，检查主网账户、TRX/USDT 记录、交易回执和合约冻结查询权限；不会转账或添加订阅，会消耗 API 查询额度。\n"
             "检测失败先核对密钥、权限、白名单和额度，修改后重试。检测通过只表示当时接口可用，不代表通知已验收。",
             [[Button("只读检测 API", callback_data="monitor:test")]]),
            ("自行验证查询和监听", "点击返回首页，发送一个公开 TRON 地址，核对余额与最近 20 笔 USDT 转账。\n"
             "需要提醒时再点“加入监听”，到地址簿设置备注、币种、方向、最低金额和通知格式；每人最多 5 个地址。\n"
             "群监听：把机器人设为群管理员，在群里 /start；仅群管理员能改群订阅，每群最多 5 个地址。\n"
             "冻结预警需在地址详情单独开启，只检查官方 USDT 黑名单；币价来自 Binance 公开市场，不是兑换成交价。\n"
             "扫描约 30 秒一轮，失败会退避；请自行验证订阅后的真实通知，不承诺秒级。完成引导不会自动添加地址。",
             []),
        ]
        title, content, actions = steps[step]
        nav = []
        if step:
            nav.append(Button("← 上一步", callback_data=f"monitor:setup:{step - 1}"))
        if step in (0, 1) and self.store.get("chain_key"):
            nav.append(Button("使用已保存的密钥", callback_data="monitor:setup:2"))
        elif step == 3:
            nav.append(Button("完成引导／返回首页", callback_data="shop:go:home"))
        rows = actions + ([nav] if nav else []) + [[Button("暂停／返回首页", callback_data="shop:go:home")]]
        text = escape(f"⚙️ 设置 · 第 {step + 1}/4 步：{title}\nTronGrid API：{configured}\n\n{content}\n\n"
                      "仅主人私聊可设置，只影响本机器人。发送 /设置 返回当前步骤；/cancel 返回首页。")
        text = text.replace("&lt;tronkey&gt;", "<code>/tronkey 你的APIKey</code>")
        await self.send(update, context, text, Inline(rows))

    async def key(self, update, context):
        if not self.owner_is(update):
            return
        try:
            await update.message.delete()
        except TelegramError:
            pass
        if len(context.args) != 1 or (context.args[0] != "clear" and not re.fullmatch(r"[A-Za-z0-9_-]{8,256}", context.args[0])):
            return await self.send(update, context, "用法：/tronkey APIKey；仅填写 TronGrid API Key，不要粘贴网址、钱包私钥或助记词。")
        self.store.set("chain_key", "" if context.args[0] == "clear" else context.args[0])
        await self.send(update, context, "API Key 已清除，查询监听暂停，订阅保留。" if context.args[0] == "clear" else "API Key 已保存，不回显。请点击管理中的只读检测；若原消息仍在，请手动删除。")
        await self.manage(update, context, 1 if context.args[0] == "clear" else 2)

    async def key_text(self, update, context):
        context.args = (update.message.text or "").split()[1:]
        await self.key(update, context)

    async def test_api(self, update, context):
        if not self.owner_is(update) or not await self.ready(update, context):
            return
        await self.send(update, context, "正在只读检测主网查询权限，不会转账或订阅地址。")
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False,
                    headers={"TRON-PRO-API-KEY": self.store.get("chain_key")}) as client:
                for suffix, params in (("", {"only_confirmed": "true"}),
                        ("/transactions", {"only_confirmed": "true", "limit": 1}),
                        ("/transactions/trc20", {"only_confirmed": "true", "limit": 1, "contract_address": watch.USDT})):
                    await watch.chain_get(client, TEST_ADDRESS + suffix, params)
            await blacklist(self.store.get("chain_key"), TEST_ADDRESS)
            for method in ("gettransactionbyid", "gettransactioninfobyid"):
                await node(self.store.get("chain_key"), method, {"value": "0" * 64})
            await self.send(update, context, "只读检测通过：账户、TRX/USDT 记录、交易回执及合约冻结查询接口可用。尚未验证实时通知；额度及后续可用性以供应商为准。")
            if self.store.get("setup_active"):
                await self.manage(update, context, 3)
        except Exception:
            await self.send(update, context, "检测未通过。请核对主网 API Key、接口权限及剩余额度，稍后重试；未修改订阅。")

    async def action(self, update, context, action):
        context.user_data.pop("shop", None)
        if action == "help":
            return await self.send(update, context, "直接发 TRON 地址查询；/query 地址 也可查询。\n/watch 地址 添加监听，/watches 查看地址簿，/unwatch 地址 确认后删除。\n/tx 交易哈希 查询已确认交易；/price BTC 查询行情。\n/blacklist 地址 查询 USDT 冻结状态，/freeze 地址 打开该订阅的冻结预警设置。\n私聊每人/群聊每群最多 5 个地址，群管理仅限管理员。今日统计采用 UTC+8，只覆盖订阅后已同步交易。\n通知支持 USDT/TRX、转入/转出、最低金额与详细/简洁格式。删除只清理当前订阅及统计。\n发送 /cancel 返回首页。")
        if action == "book":
            return await watch.book(self, update, context)
        if action in ("tx", "blacklist", "price"):
            if action != "price" and not await self.ready(update, context):
                return
            self.wait(context, flow="extra", action=action)
            return await self.send(update, context, {"tx": "发送 64 位 TRON 交易哈希。", "blacklist": "发送要查询 USDT 冻结状态的 TRON 地址。", "price": "发送币种代码，如 TRX、BTC、ETH；报价单位为 USDT。"}[action] + "\n/cancel 返回首页。")
        if not await self.ready(update, context):
            return
        if action == "query":
            self.wait(context, flow="query")
            return await self.send(update, context, "请发送 TRON 主网地址；发送 /cancel 取消。")
        if action == "new":
            return await watch.handle(self, update, context, "new")

    async def command(self, update, context):
        command = update.message.text.split()[0].split("@")[0].lstrip("/").lower()
        if command in ("tx", "price", "blacklist", "freeze"):
            if not context.args:
                return await self.send(update, context, {"tx": "用法：/tx 64位交易哈希", "price": "用法：/price TRX（或 BTC、ETH 等币种）", "blacklist": "用法：/blacklist TRON地址", "freeze": "用法：/freeze 已订阅的TRON地址"}[command])
            return await self.extra(update, context, command, context.args[0])
        if command == "watches":
            return await self.action(update, context, "book")
        if not context.args:
            return await self.action(update, context, "query" if command == "query" else "new" if command == "watch" else "book")
        action = {"query": "query", "watch": "add", "unwatch": "delete"}[command]
        await self.watch_action(update, context, action + ":" + context.args[0])

    async def watch_action(self, update, context, action):
        if action.startswith(("query:", "add:")) or action == "new":
            if not await self.ready(update, context):
                return
        try:
            await watch.handle(self, update, context, action)
        except ValueError as error:
            await self.send(update, context, escape(str(error)))
        except Exception:
            await self.send(update, context, "查询或监听操作暂不可用，请稍后重试；不会转账。")

    async def callback(self, update, context):
        await update.callback_query.answer()
        data = update.callback_query.data
        if data.startswith("monitor:setup:"):
            value = data.removeprefix("monitor:setup:")
            if value in ("0", "1", "2", "3"):
                return await self.manage(update, context, int(value))
            return
        if data.startswith("monitor:blacklist:"):
            return await self.extra(update, context, "blacklist", data.split(":", 2)[2])
        if data.startswith("monitor:f:"):
            return await self.extra(update, context, "freeze_toggle", data[10:])
        if data.startswith("monitor:freeze:"):
            return await self.extra(update, context, "freeze", data.split(":", 2)[2])
        if data == "monitor:test":
            return await self.test_api(update, context)
        if data == "shop:go:home":
            return await self.home(update, context)
        if data.startswith("shop:w:"):
            return await self.watch_action(update, context, data[7:])

    async def message(self, update, context):
        text = (update.message.text or "").strip()
        if text in (ADMIN, "⚙️ 机器人管理"):
            return await self.manage(update, context)
        if self.owner_is(update) and self.store.get("setup_active"):
            if self.store.get("setup_step", 0) in (0, 1):
                context.args = [text]
                return await self.key(update, context)
            return await self.send(update, context, "请点击只读检测；发送 /设置 显示当前问题，/cancel 暂停。")
        if text in MENU:
            return await self.action(update, context, MENU[text])
        draft = context.user_data.get("shop", {})
        if draft and time.time() - draft.get("at", 0) > 600:
            context.user_data.pop("shop", None)
            return await self.send(update, context, "输入已过期，请重新选择功能。")
        flow = draft.get("flow")
        if flow == "extra":
            return await self.extra(update, context, draft["action"], text)
        if flow == "watch" and not await self.ready(update, context):
            return
        if flow in ("watch", "watch_note", "watch_minimum"):
            try:
                return await watch.input_value(self, update, context, draft, text)
            except ValueError as error:
                return await self.send(update, context, escape(str(error)))
        if flow == "query" or address_valid(text):
            return await self.watch_action(update, context, "query:" + text)
        if re.fullmatch(r"[a-fA-F0-9]{64}", text):
            return await self.extra(update, context, "tx", text)
        if text.upper() in ("TRX", "BTC", "ETH", "USDT"):
            return await self.extra(update, context, "price", text)
        await self.send(update, context, "请选择菜单或发送有效 TRON 地址；/cancel 返回首页。")

    async def extra(self, update, context, action, value):
        if action not in ("price", "freeze", "freeze_toggle") and not await self.ready(update, context):
            return
        try:
            if action == "price":
                return await self.send(update, context, escape(await self.prices.get(value)))
            if action == "tx":
                text = await transaction(self.store.get("chain_key"), value)
                return await self.send(update, context, escape(text), Inline([[Button("链上详情", url="https://tronscan.org/#/transaction/" + value.lower())]]))
            if action == "blacklist":
                frozen = await blacklist(self.store.get("chain_key"), value)
                return await self.send(update, context, escape(value + "\nUSDT 合约黑名单：" + ("已列入（USDT 转账受限）" if frozen else "当前未列入") + "\n仅为当前已确认链上状态，不代表地址安全、合规或其他资产状态。"))
            change = None
            if action == "freeze_toggle":
                parts = value.split(":")
                if len(parts) != 3 or parts[2] not in ("0", "1"):
                    raise ValueError("预警按钮无效，请重新打开设置。")
                value, revision, change = parts
            row = self.store.watched(update.effective_user.id, value)
            if not row:
                raise ValueError("请先将地址加入当前地址簿；不能修改别人的订阅。")
            if change is not None and row["revision"] != revision:
                raise ValueError("订阅已变化，请重新打开预警设置。")
            with self.store.db() as db:
                enabled = db.execute("SELECT checked,error FROM freeze_watches WHERE buyer=? AND address=? AND revision=?", (row["buyer"], value, row["revision"])).fetchone()
                if action == "freeze_toggle":
                    if change == "0":
                        db.execute("DELETE FROM freeze_watches WHERE buyer=? AND address=?", (row["buyer"], value))
                        enabled = None
                    elif enabled is None:
                        db.execute("INSERT OR REPLACE INTO freeze_watches(buyer,address,revision) VALUES(?,?,?)", (row["buyer"], value, row["revision"]))
                        enabled = (0, "")
            state = "未启用" if enabled is None else "等待首次检查" if not enabled[0] else "上次检查：" + watch.when(enabled[0])
            if enabled is not None and enabled[1]:
                state += "；检查失败，旧状态不代表当前状态"
            await self.send(update, context, escape(f"USDT 冻结预警\n{value}\n{state}\n轮询官方合约黑名单；首次发现已列入或状态变化时通知。不是全网冻结预警频道，不承诺秒级。"),
                Inline([[Button("开启预警" if enabled is None else "关闭预警", callback_data="monitor:f:" + value + ":" + row["revision"] + ":" + ("1" if enabled is None else "0"))], [watch.button("返回地址", "detail:" + value)]]))
        except ValueError as error:
            await self.send(update, context, escape(str(error)))
        except Exception:
            await self.send(update, context, "查询暂不可用，请核对 API 权限或稍后重试；不代表零余额、未冻结或交易失败。")


async def scan_freeze(ui, bot):
    with ui.store.db() as db:
        db.execute("DELETE FROM freeze_watches WHERE NOT EXISTS(SELECT 1 FROM watches w WHERE w.buyer=freeze_watches.buyer AND w.address=freeze_watches.address AND w.revision=freeze_watches.revision)")
        rows = [dict(row) for row in db.execute("SELECT * FROM freeze_watches")]
    for row in rows:
        try:
            state = int(await blacklist(ui.store.get("chain_key"), row["address"]))
            current = ui.store.watched(row["buyer"], row["address"])
            if not current or current["revision"] != row["revision"]:
                continue
            with ui.store.db() as db:
                if not db.execute("SELECT 1 FROM freeze_watches WHERE buyer=? AND address=? AND revision=?", (row["buyer"], row["address"], row["revision"])).fetchone():
                    continue
            if (row["state"] is None and state) or (row["state"] is not None and state != row["state"]):
                await bot.send_message(row["buyer"], f"USDT 黑名单状态提醒\n{current['note']}\n{row['address']}\n{'已列入黑名单，USDT 转账受限' if state else '已从黑名单移除'}\n仅反映官方合约状态，不代表地址整体安全。")
            with ui.store.db() as db:
                db.execute("UPDATE freeze_watches SET state=?,checked=?,error='' WHERE buyer=? AND address=? AND revision=?",
                    (state, int(time.time() * 1000), row["buyer"], row["address"], row["revision"]))
        except Exception:
            with ui.store.db() as db:
                db.execute("UPDATE freeze_watches SET error='检查暂不可用' WHERE buyer=? AND address=? AND revision=?", (row["buyer"], row["address"], row["revision"]))


async def run(ui, bot):
    delay = 30
    while True:
        if ui.store.get("chain_key"):
            try:
                await scan_watches(ui.store, bot)
                await scan_freeze(ui, bot)
                rows = ui.store.watches()
                with ui.store.db() as db:
                    frozen = db.execute("SELECT error FROM freeze_watches").fetchall()
                failed = (rows and all(row["error"] for row in rows)) or (frozen and all(row[0] for row in frozen))
                delay = min(delay * 2, 300) if failed else 30
            except Exception:
                logging.getLogger(__name__).warning("TRON watch unavailable; retry with backoff")
                delay = min(delay * 2, 300)
        await asyncio.sleep(delay)


def register(app):
    settings = load_settings()
    ui = MonitorUI(ShopStore(settings.ledger_db_path.with_name("monitor.sqlite3")), int(settings.owner_chat_id))
    app.bot_data["monitor"] = ui
    for name, callback in (("start", ui.home), ("cancel", ui.home), ("manage", ui.manage), ("tronkey", ui.key),
                           ("query", ui.command), ("watch", ui.command), ("unwatch", ui.command), ("watches", ui.command),
                           ("tx", ui.command), ("price", ui.command), ("blacklist", ui.command), ("freeze", ui.command)):
        app.add_handler(CommandHandler(name, lambda u, c, fn=callback: ui.entry(fn, u, c),
            filters=filters.ChatType.PRIVATE if name in ("manage", "tronkey") else None))
    app.add_handler(CommandHandler("help", lambda u, c: ui.entry(lambda a, b: ui.action(a, b, "help"), u, c)))
    app.add_handler(CallbackQueryHandler(lambda u, c: ui.entry(ui.callback, u, c), pattern=r"^(monitor:(setup:|test$|freeze:|f:|blacklist:)|shop:w:|shop:go:home$)"))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.Regex(r"^\s*/设置\s*$"), lambda u, c: ui.entry(ui.manage, u, c)))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.Regex(r"^\s*/tronkey(?:\s|$)"), lambda u, c: ui.entry(ui.key_text, u, c)))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u, c: ui.entry(ui.message, u, c)))
