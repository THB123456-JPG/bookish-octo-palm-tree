from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class BotSettings:
    bot_token: str
    proxy_url: str
    telegram_timeout: float
    ledger_db_path: Path


def load_settings(env_path: str | Path | None = None) -> BotSettings:
    if env_path:
        load_dotenv(env_path)
    else:
        load_dotenv()
    return BotSettings(
        bot_token=os.getenv("BOT_TOKEN", "").strip(),
        proxy_url=os.getenv("PROXY_URL", "").strip(),
        telegram_timeout=float(os.getenv("TELEGRAM_TIMEOUT", "60")),
        ledger_db_path=Path(os.getenv("LEDGER_DB_PATH", "outputs/ledger.sqlite3")).expanduser(),
    )
