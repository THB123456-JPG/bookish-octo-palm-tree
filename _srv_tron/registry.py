from __future__ import annotations

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from config.settings import load_settings
from services.support_relay import relay_incoming_private_message, relay_owner_reply


def _owner() -> int:
    return int(load_settings().owner_chat_id)


def _private(update: Update) -> bool:
    return bool(update.effective_chat and update.effective_chat.type == "private")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    mode = load_settings().bot_mode
    labels = {"support": "客服转接", "trc20": "TRC20 查询与监听"}
    if update.message:
        await update.message.reply_text(f"{labels[mode]}机器人已就绪。发送 /help 查看用法。")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    mode = load_settings().bot_mode
    content = {
        "support": "客户直接发送消息；管理员回复转发消息即可回复客户。",
        "trc20": "/query 地址 查询余额\n/watch 地址 添加监听\n/unwatch 地址 取消监听\n/watches 查看监听",
    }[mode]
    await update.message.reply_text(content)


async def support(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _private(update):
        return
    path = load_settings().ledger_db_path.with_name("support-relay.json")
    if await relay_owner_reply(update, context, str(_owner()), path):
        return
    await relay_incoming_private_message(update, context, str(_owner()), path)


def register_handlers(app: Application) -> None:
    mode = load_settings().bot_mode
    if mode == "shop":
        from features.shop.ui import register
        register(app)
        return
    if mode == "trc20":
        from features.tron_monitor import register
        register(app)
        return
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    if mode == "support":
        app.add_handler(MessageHandler(filters.ALL, support))
