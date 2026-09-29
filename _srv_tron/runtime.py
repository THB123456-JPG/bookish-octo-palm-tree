from __future__ import annotations

import asyncio

from config.settings import load_settings
from services import state


async def start(app) -> None:
    if state.EXPECTED_BOT_USERNAME:
        user = await app.bot.get_me()
        if (user.username or "").lower() != state.EXPECTED_BOT_USERNAME.lower():
            raise RuntimeError("Bot username mismatch; startup stopped")
    archive = app.bot_data.get("message_archive")
    if archive is not None:
        archive.start()
    if load_settings().bot_mode == "trc20":
        from features.tron_monitor import run
        app.bot_data["trc20_watch_task"] = asyncio.create_task(run(app.bot_data["monitor"], app.bot))
    if load_settings().bot_mode == "shop":
        from features.shop.payments import run
        app.bot_data["shop_task"] = asyncio.create_task(run(app.bot_data["shop"], app.bot))


async def stop(app) -> None:
    shop_task = app.bot_data.pop("shop_task", None)
    if isinstance(shop_task, asyncio.Task):
        shop_task.cancel()
        try:
            await shop_task
        except asyncio.CancelledError:
            pass
    task = app.bot_data.pop("trc20_watch_task", None)
    if isinstance(task, asyncio.Task):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    archive = app.bot_data.get("message_archive")
    if archive is not None:
        await asyncio.to_thread(archive.close)
