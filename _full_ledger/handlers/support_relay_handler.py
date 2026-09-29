from __future__ import annotations

from telegram import Update
from telegram.ext import ApplicationHandlerStop, ContextTypes

from services import runtime
from services.support_relay import relay_incoming_private_message, relay_owner_reply


async def handle_support_relay(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await relay_owner_reply(update, context, runtime.OWNER_CHAT_ID):
        raise ApplicationHandlerStop
    if await relay_incoming_private_message(update, context, runtime.OWNER_CHAT_ID):
        raise ApplicationHandlerStop
