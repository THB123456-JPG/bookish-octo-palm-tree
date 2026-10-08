from __future__ import annotations

import re
import sys

from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters
from telegram.request import HTTPXRequest

from config.constants import BOT_VERSION, TEXT_ADD_GROUP, TEXT_LEDGER, TEXT_LEDGER_ADD_GROUP
from config.logging_config import configure_logging
from config.settings import load_settings
from handlers.admin_handler import (
    learn_cancel_command,
    learn_cards_command,
    learn_confirm_command,
    ocr_candidates_command,
    ocr_cache_today_command,
    ocr_debug_command,
    ocr_export_fonts_command,
    ocr_font_stats_command,
    ocr_health_command,
    ocr_import_fonts_command,
    ocr_learning_stats_command,
    ocr_review_command,
    ocr_version_command,
    remote_ocr_status_command,
    show_id,
    show_version,
    status_panel_command,
)
from handlers.broadcast_handler import (
    broadcast_cancel_command,
    broadcast_preview_command,
    handle_broadcast_callback,
    notify_all_command,
    notify_members_command,
    start_broadcast,
)
from handlers.card_ocr_handler import handle_photo
from handlers.ledger_handler import (
    handle_ledger_callback,
    handle_ledger_command,
    handle_ledger_menu,
    handle_new_chat_members,
    handle_priority_ledger_text,
)
from handlers.start_handler import handle_add_group_menu, start
from handlers.support_relay_handler import handle_support_relay
import services.runtime as runtime
from services.runtime import *  # noqa: F403,F401 - compatibility exports for existing tests and external scripts.
from services.runtime import start_background_tasks, stop_background_tasks

if __name__ != "__main__":
    sys.modules[__name__] = runtime


def build_application() -> Application:
    settings = load_settings()
    if not settings.bot_token:
        raise RuntimeError("Please set BOT_TOKEN in .env first")

    request_kwargs = {
        "connect_timeout": settings.telegram_timeout,
        "read_timeout": settings.telegram_timeout,
        "write_timeout": settings.telegram_timeout,
        "pool_timeout": settings.telegram_timeout,
    }
    if settings.proxy_url:
        request_kwargs["proxy_url"] = settings.proxy_url

    app = (
        Application.builder()
        .token(settings.bot_token)
        .request(HTTPXRequest(**request_kwargs))
        .get_updates_request(HTTPXRequest(**request_kwargs))
        .post_init(start_background_tasks)
        .post_shutdown(stop_background_tasks)
        .build()
    )
    register_handlers(app)
    return app


def register_handlers(app: Application) -> None:
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", show_id))
    app.add_handler(CommandHandler("version", show_version))
    app.add_handler(CommandHandler("ocr_debug", ocr_debug_command))
    app.add_handler(CommandHandler("ocr_candidates", ocr_candidates_command))
    app.add_handler(CommandHandler("ocr_font_stats", ocr_font_stats_command))
    app.add_handler(CommandHandler("ocr_review", ocr_review_command))
    app.add_handler(CommandHandler("ocr_export_fonts", ocr_export_fonts_command))
    app.add_handler(CommandHandler("ocr_import_fonts", ocr_import_fonts_command))
    app.add_handler(CommandHandler("ocr_version", ocr_version_command))
    app.add_handler(CommandHandler("ocr_cache_today", ocr_cache_today_command))
    app.add_handler(CommandHandler("ocr_health", ocr_health_command))
    app.add_handler(CommandHandler("remote_ocr_status", remote_ocr_status_command))
    app.add_handler(CommandHandler(["status", "ocr_status"], status_panel_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/状态(?:@\w+)?(?:\s|$)"), status_panel_command))
    app.add_handler(CommandHandler("learn_cards", learn_cards_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/?学习卡密(?:\s|$)") & filters.ChatType.PRIVATE, learn_cards_command))
    app.add_handler(CommandHandler("learn_confirm", learn_confirm_command))
    app.add_handler(CommandHandler("learn_cancel", learn_cancel_command))
    app.add_handler(CommandHandler("ocr_learning_stats", ocr_learning_stats_command))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, handle_support_relay), group=-2)
    app.add_handler(
        MessageHandler(
            filters.Regex(f"^({re.escape(TEXT_LEDGER_ADD_GROUP)}|记账拉机器人进群)$"),
            handle_ledger_add_group_menu,
        )
    )
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(TEXT_LEDGER)}$"), handle_ledger_menu))
    app.add_handler(MessageHandler(filters.Regex(f"^{re.escape(TEXT_ADD_GROUP)}$"), handle_add_group_menu))
    app.add_handler(CommandHandler("broadcast", start_broadcast))
    app.add_handler(MessageHandler(filters.Regex(r"^广播$") & filters.ChatType.PRIVATE, start_broadcast))
    app.add_handler(CommandHandler("broadcast_preview", broadcast_preview_command))
    app.add_handler(CommandHandler("broadcast_cancel", broadcast_cancel_command))
    app.add_handler(CommandHandler(["notify_all", "at_all"], notify_all_command))
    app.add_handler(CommandHandler("notify_members", notify_members_command))
    app.add_handler(MessageHandler(filters.Regex(r"^通知所有人(?:\s|$)") & filters.ChatType.GROUPS, notify_all_command))
    app.add_handler(CallbackQueryHandler(handle_broadcast_callback, pattern=r"^broadcast:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_priority_ledger_text), group=-1)
    app.add_handler(
        CommandHandler(
            ["help", "bill", "fullbill", "yesterday", "undo", "clear", "in", "income", "out", "payout"],
            handle_ledger_command,
        )
    )
    app.add_handler(CallbackQueryHandler(handle_ledger_callback, pattern=r"^ledger:"))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_new_chat_members))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_ledger_command))


def main() -> None:
    configure_logging()
    app = build_application()
    logger.info("Bot is starting. Version=%s.", BOT_VERSION)  # noqa: F405 - exported by services.runtime.
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
