from __future__ import annotations

import asyncio
import ast
import html
import json
import logging
import os
import re
import subprocess
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timezone, timedelta
from decimal import Decimal, DivisionByZero, InvalidOperation
from io import BytesIO
from pathlib import Path

import httpx
import pytesseract
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, ApplicationHandlerStop, CallbackQueryHandler, ChatMemberHandler, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.request import HTTPXRequest

from services.ledger import ledger_commands
from services.ledger.ledger_commands import Actor as LedgerActor
from services.ledger.ledger_commands import handle_text as handle_ledger_command_text
from services.ocr.candidate_audit import append_candidate_audit, build_candidate_audit
from services.ocr.correction_engine import apply_corrections
from services.ocr.admin_commands import (
    export_font_templates,
    format_font_stats as format_ocr_font_stats_plus,
    format_ocr_review,
    format_ocr_version,
    import_font_templates,
)
from services.ocr.debug_commands import ocr_candidates as format_ocr_candidates_debug
from services.ocr.debug_commands import ocr_debug as format_ocr_debug
from services.ocr.font_repository import FontRepository
from services.ocr.daily_learning import extract_ground_truth_cards
from services.ocr.learning_commands import build_learning_preview, execute_learning, format_learning_stats
from services.ocr.pubg_char_correction import apply_pubg_char_corrections
from services.ocr.pubg_candidate_merge import incomplete_pubg_prefix_keys, merge_text_and_worker_pubg_cards
from services.ocr.today_cache import append_today_ocr_cache, today_ocr_cache_summary
from services.ocr.validator import validate_candidate
from services.ocr.duplicate_detector import canonical_card
from storage.repositories.ledger_storage import LedgerStore


TEXT_LEDGER = "记账"
TEXT_ADD_GROUP = "拉机器人进群"
TEXT_LEDGER_ADD_GROUP = "✅记账拉机器人进群"


load_dotenv()

BOT_VERSION = "strict-v120-owner-broadcast-no-trx"
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_CHAT_ID = os.getenv("OWNER_CHAT_ID", "").strip()
AUDIT_BOT_TOKEN = os.getenv("AUDIT_BOT_TOKEN", "").strip()
AUDIT_CHAT_ID = os.getenv("AUDIT_CHAT_ID", "").strip()
SINGLE_WAIT_SECONDS = float(os.getenv("SINGLE_WAIT_SECONDS", os.getenv("BATCH_WAIT_SECONDS", "0.6")))
MULTI_BATCH_WAIT_SECONDS = max(
    float(os.getenv("MULTI_BATCH_WAIT_SECONDS", os.getenv("BATCH_WAIT_SECONDS", "3.0"))),
    2.0,
)
OCR_PROVIDER = os.getenv("OCR_PROVIDER", "ocrspace").strip().lower()
REMOTE_OCR_ENABLED = os.getenv("REMOTE_OCR_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
REMOTE_OCR_URL = os.getenv("REMOTE_OCR_URL", "http://100.81.208.104:8000").strip().rstrip("/")
REMOTE_OCR_TIMEOUT = float(os.getenv("REMOTE_OCR_TIMEOUT", "1.5"))
REMOTE_OCR_HEALTH_CACHE_SECONDS = float(os.getenv("REMOTE_OCR_HEALTH_CACHE_SECONDS", "10"))
REMOTE_OCR_OFFLINE_SECONDS = max(5, int(float(os.getenv("REMOTE_OCR_OFFLINE_SECONDS", "120"))))
REMOTE_OCR_PROBE_SECONDS = max(5, int(float(os.getenv("REMOTE_OCR_PROBE_SECONDS", "30"))))
OCR_SPACE_API_KEY = os.getenv("OCR_SPACE_API_KEY", "").strip()
OCR_SPACE_API_KEYS_RAW = os.getenv("OCR_SPACE_API_KEYS", "").strip()
OCR_SPACE_MAX_SIDE = int(os.getenv("OCR_SPACE_MAX_SIDE", "3000"))
OCR_SPACE_MIN_SIDE = int(os.getenv("OCR_SPACE_MIN_SIDE", "2600"))
OCR_SPACE_MAX_UPLOAD_BYTES = max(300_000, int(os.getenv("OCR_SPACE_MAX_UPLOAD_BYTES", "950000")))
OCR_SPACE_TIMEOUT = float(os.getenv("OCR_SPACE_TIMEOUT", "18"))
OCR_SPACE_ENGINES = [engine.strip() for engine in os.getenv("OCR_SPACE_ENGINES", "2,1").split(",") if engine.strip()]
OCR_CONCURRENCY = int(os.getenv("OCR_CONCURRENCY", "1"))
OCR_SPACE_429_COOLDOWN_SECONDS = max(30, int(os.getenv("OCR_SPACE_429_COOLDOWN_SECONDS", "180")))
LOCAL_FALLBACK = os.getenv("LOCAL_FALLBACK", "1").strip() == "1"
LOCAL_COMPLEMENT = os.getenv("LOCAL_COMPLEMENT", "0").strip() == "1"
VERIFY_WITH_LOCAL = os.getenv("VERIFY_WITH_LOCAL", "0").strip() == "1"
OCR_MAX_SIDE = int(os.getenv("OCR_MAX_SIDE", "3000"))
OCR_MIN_SIDE = int(os.getenv("OCR_MIN_SIDE", "2600"))
LOCAL_OCR_MIN_CARD_VOTES = max(1, int(os.getenv("LOCAL_OCR_MIN_CARD_VOTES", "2")))
MAX_PSN_PER_IMAGE = int(os.getenv("MAX_PSN_PER_IMAGE", "2"))
TESSERACT_CMD = os.getenv("TESSERACT_CMD", "").strip()
PROXY_URL = os.getenv("PROXY_URL", "").strip()
TELEGRAM_TIMEOUT = float(os.getenv("TELEGRAM_TIMEOUT", "60"))
DEBUG_OCR = os.getenv("DEBUG_OCR", "").strip() == "1"
CLEANUP_ENABLED = os.getenv("CLEANUP_ENABLED", "1").strip() == "1"
CLEANUP_AFTER_SECONDS = max(60, int(float(os.getenv("CLEANUP_AFTER_HOURS", "24")) * 3600))
CLEANUP_CHECK_SECONDS = max(60, int(os.getenv("CLEANUP_CHECK_SECONDS", "300")))
CLEANUP_OUTPUTS_DIR = Path(os.getenv("CLEANUP_OUTPUTS_DIR", "outputs")).expanduser()
LEDGER_DB_PATH = Path(os.getenv("LEDGER_DB_PATH", "outputs/ledger.sqlite3")).expanduser()
OCR_CANDIDATES_PATH = Path(os.getenv("OCR_CANDIDATES_PATH", "outputs/ocr_candidates.json")).expanduser()
TODAY_OCR_CACHE_PATH = Path(os.getenv("TODAY_OCR_CACHE_PATH", "outputs/today_ocr_cache.json")).expanduser()
PHOTO_BATCH_MAX_IMAGES = max(1, int(os.getenv("PHOTO_BATCH_MAX_IMAGES", "50")))
PHOTO_RATE_WINDOW_SECONDS = max(10, int(os.getenv("PHOTO_RATE_WINDOW_SECONDS", "60")))
PHOTO_RATE_LIMIT_PER_CHAT = max(1, int(os.getenv("PHOTO_RATE_LIMIT_PER_CHAT", "80")))
PHOTO_RATE_LIMIT_PER_USER = max(1, int(os.getenv("PHOTO_RATE_LIMIT_PER_USER", "50")))
OKX_C2C_USDT_CNY_URL = (
    "https://www.okx.com/v3/c2c/tradingOrders/books"
    "?quoteCurrency=cny&baseCurrency=usdt&side=sell&paymentMethod=all&userType=all&showTrade=false"
)
OKX_EXCHANGE_RATE_URL = "https://www.okx.com/api/v5/market/exchange-rate"
OKX_HTTP_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
TELEGRAM_TEXT_LIMIT = 4096
TELEGRAM_SAFE_TEXT_LIMIT = 3600
PROCESS_STARTED_AT = time.time()

SUCCESS_PREFIX = "\u672c\u6b21\u8bc6\u522b\u6210\u529f"
COUNT_SUFFIX = "\u4e2a"
UNCERTAIN_PREFIX = "\u7591\u4f3c\u51b2\u7a81"
UNCERTAIN_SUFFIX = "\u4e2a\u672a\u8f93\u51fa"
MANUAL_REVIEW_TEXT = "\u8bc6\u522b\u6a21\u7cca\uff0c\u8bf7\u4eba\u5de5\u6838\u5b9e"
MANUAL_REVIEW_SUMMARY = "\u9700\u4eba\u5de5\u6838\u5b9e"
PUBG_LABEL = "PUBG\u5361\u5bc6"
PSN_LABEL = "PSN\u5361\u5bc6"
FUZZY_SUFFIX = "\uff08\u8bc6\u522b\u6a21\u7cca\uff09"

if not TESSERACT_CMD:
    win_tesseract = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
    if win_tesseract.exists():
        TESSERACT_CMD = str(win_tesseract)

if TESSERACT_CMD:
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logger = logging.getLogger("telegram-card-platform")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


@dataclass(frozen=True)
class OcrResult:
    cards: tuple[str, ...]
    psn_cards: tuple[str, ...] = tuple()
    psn_uncertain: tuple[str, ...] = tuple()
    psn_ordered: tuple[str, ...] = tuple()
    card_locations: tuple[tuple[str, int, int], ...] = tuple()
    psn_locations: tuple[tuple[str, int, int], ...] = tuple()
    sequence_index: int = 0
    pubg_expected_count: int | None = None
    psn_expected_count: int | None = None
    raw_text: str = ""
    uncertain_count: int = 0
    source_caption: str = ""
    ocr_fixed_count: int = 0
    ocr_missing_count: int = 0
    ocr_false_negative: int = 0
    ocr_character_confusion: int = 0
    corrections_applied: tuple[dict[str, str], ...] = tuple()
    has_unresolved_pubg_fragment: bool = False


@dataclass(frozen=True)
class OrderedCardOccurrence:
    card: str
    image_index: int
    y: int
    x: int
    duplicate_key: str
    display: str = ""


@dataclass(frozen=True)
class CardHistoryDuplicate:
    card_type: str
    card: str
    first_seen_at: str
    first_source_user: str


chat_buffers: dict[int, list[Update]] = defaultdict(list)
chat_tasks: dict[int, asyncio.Task] = {}
chat_flush_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
photo_sequence_lock = asyncio.Lock()
photo_sequence_by_update: dict[int, int] = {}
global_photo_sequence = 0
photo_rate_chat: dict[int, list[float]] = defaultdict(list)
photo_rate_user: dict[tuple[int, int], list[float]] = defaultdict(list)
photo_rate_warned_at: dict[tuple[str, int], float] = {}
ocrspace_cooldown_until = 0.0
ocrspace_key_cooldowns: dict[str, float] = {}
remote_ocr_status = {
    "last_ok": False,
    "last_error": "",
    "last_latency_ms": 0,
    "last_card_count": 0,
    "last_checked_at": "",
    "remote_health": False,
    "last_success_at": "",
    "last_failed_at": "",
    "today_date": "",
    "today_remote_calls": 0,
    "today_remote_success": 0,
    "today_remote_failed": 0,
    "today_fallback_count": 0,
    "today_remote_latency_total_ms": 0,
    "today_enhanced_used": 0,
    "today_cache_hits": 0,
}
remote_ocr_health_cache: dict[str, object] = {"checked_at": 0.0, "result": None}
remote_ocr_offline_until = 0.0
remote_http_client: httpx.Client | None = None
remote_http_client_timeout: float | None = None
ocr_semaphore = asyncio.Semaphore(max(1, OCR_CONCURRENCY))
ledger_store = LedgerStore(LEDGER_DB_PATH)
font_repository = FontRepository()
pending_learning_texts: dict[int, str] = {}
welcome_sent_at: dict[int, float] = {}
LEDGER_TZ = timezone(timedelta(hours=8))
LEDGER_CALLBACK_TEXT = {
    "ledger:yesterday": "昨日账单",
    "ledger:today": "今日账单",
    "ledger:full": "完整账单",
    "ledger:help": "帮助",
}


FULLWIDTH_MAP = str.maketrans(
    {
        "\uff33": "S",
        "\uff2f": "O",
        "\uff31": "Q",
        "\uff24": "D",
        "\uff29": "I",
        "\uff2c": "L",
        "\uff34": "T",
        "\uff3a": "Z",
        "\uff22": "B",
        "\uff27": "G",
        "\uff10": "0",
        "\uff11": "1",
        "\uff12": "2",
        "\uff13": "3",
        "\uff14": "4",
        "\uff15": "5",
        "\uff16": "6",
        "\uff17": "7",
        "\uff18": "8",
        "\uff19": "9",
        "\u2014": "-",
        "\u2013": "-",
        "\uff0d": "-",
        "\u30fc": "-",
        "\uff3f": "_",
        "\uff1a": ":",
        "\uff1b": ";",
    }
)


def normalize_text(text: str) -> str:
    text = text.upper().translate(FULLWIDTH_MAP)
    text = re.sub(r"(?<![A-Z0-9])9([0ODQU][7TIL/?][0-9ODQUILTZEA$SGB]{3})", r"S\1", text)
    return re.sub(r"((?:密码|卡号|CDK|CDKEY)\s*\d*[:：]?)\s*([SP5$][0ODQU][7TIL/?])", r"\1 \2", text)


def is_within_cleanup_root(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def remove_path(path: Path) -> None:
    if path.is_symlink():
        path.unlink(missing_ok=True)
        return
    if path.is_dir():
        for child in path.iterdir():
            remove_path(child)
        path.rmdir()
    else:
        path.unlink(missing_ok=True)


def parse_ocrspace_api_keys(raw_keys: str, fallback_key: str = "") -> list[str]:
    keys: list[str] = []
    for value in [*raw_keys.replace(";", ",").split(","), fallback_key]:
        key = value.strip()
        if key and key not in keys:
            keys.append(key)
    return keys


OCR_SPACE_API_KEYS = parse_ocrspace_api_keys(OCR_SPACE_API_KEYS_RAW, OCR_SPACE_API_KEY)


def ocr_confusion_count(source_cards: list[str], selected_cards: list[str]) -> int:
    count = 0
    for selected in selected_cards:
        selected_compact = selected.replace("-", "")
        for source in source_cards:
            source_compact = source.replace("-", "")
            if source == selected or len(source_compact) != len(selected_compact):
                continue
            if hamming_distance(source_compact, selected_compact) == 1:
                count += 1
                break
    return count


def enhanced_ocrspace_pubg_cards(raw_text: str, legacy_cards: list[str]) -> tuple[list[str], dict[str, int]]:
    stats = {
        "ocr_fixed_count": 0,
        "ocr_missing_count": 0,
        "ocr_false_negative": 0,
        "ocr_character_confusion": 0,
    }
    if not raw_text.strip():
        return [], stats

    try:
        correction_result = apply_corrections(raw_text, card_type="PUBG")
        audit = build_candidate_audit(raw_text, card_type="PUBG")
        append_candidate_audit(raw_text, card_type="PUBG", output_path=OCR_CANDIDATES_PATH)
        font_repository.learn_sample(raw_text, card_type="PUBG")
    except Exception:
        logger.exception("OCR candidate audit failed")
        return [], stats

    scored_items = []
    for item in audit.candidate_list:
        value = str(item.get("value", ""))
        if not validate_candidate(value, card_type="PUBG"):
            continue
        score = item.get("score")
        scored_items.append((float(score or 0.0), value))
    if correction_result.best_candidate:
        value = correction_result.best_candidate.candidate.corrected_text
        if validate_candidate(value, card_type="PUBG"):
            scored_items.append((float(correction_result.best_candidate.score), value))

    score_by_value = {value: score for score, value in scored_items}
    selected: list[str] = []
    for item in audit.candidate_list:
        value = str(item.get("value", ""))
        if value not in score_by_value:
            continue
        same_index = next((index for index, existing in enumerate(selected) if likely_same_card(value, existing)), None)
        if same_index is None:
            selected.append(value)
            continue
        existing = selected[same_index]
        if score_by_value[value] > score_by_value.get(existing, 0.0):
            selected[same_index] = value
    if correction_result.best_candidate:
        value = correction_result.best_candidate.candidate.corrected_text
        if validate_candidate(value, card_type="PUBG") and value not in selected:
            selected.append(value)

    legacy_set = set(legacy_cards)
    selected_set = set(selected)
    stats["ocr_fixed_count"] = len([card for card in selected if card not in legacy_set])
    stats["ocr_missing_count"] = max(0, len(selected_set - legacy_set))
    stats["ocr_false_negative"] = stats["ocr_missing_count"]
    stats["ocr_character_confusion"] = ocr_confusion_count(legacy_cards, selected)
    return selected, stats


def merge_ocr_stats(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
    return {
        "ocr_fixed_count": left.get("ocr_fixed_count", 0) + right.get("ocr_fixed_count", 0),
        "ocr_missing_count": left.get("ocr_missing_count", 0) + right.get("ocr_missing_count", 0),
        "ocr_false_negative": left.get("ocr_false_negative", 0) + right.get("ocr_false_negative", 0),
        "ocr_character_confusion": left.get("ocr_character_confusion", 0) + right.get("ocr_character_confusion", 0),
    }


BUILTIN_PUBG_CARD_CORRECTIONS = {
    "S07304-9M8Q-Y7UW-78220": "S07304-9M8Q-Y7UW-78Z2U",
    "S07304-8MP5-4TY9-VDVR6": "S07304-8MP5-4TYS-VDVR6",
    "S07304-4U60-U5L1-GLXUV": "S07304-4U6Q-U5LL-GLXUV",
}

PUBG_PREFIXES = {
    "S07304",
    "S07234",
    "S07303",
    "S07240",
    "S07292",
    "S07298",
    "S07213",
    "S07291",
    "S07205",
    "S07239",
    "S07228",
    "S07286",
}
PUBG_PREFIX_RE = re.compile(r"S07[A-Z0-9]{3}")
PUBG_PREFIX_TAIL_RE = re.compile(r"7[A-Z0-9]{3}")


def cleanup_server_files(now: float | None = None) -> int:
    if not CLEANUP_ENABLED:
        return 0
    cutoff = (now if now is not None else time.time()) - CLEANUP_AFTER_SECONDS
    removed = 0

    temp_root = Path(tempfile.gettempdir())
    for path in temp_root.glob("s07_card_*"):
        try:
            if path.stat().st_mtime <= cutoff and is_within_cleanup_root(path, temp_root):
                remove_path(path)
                removed += 1
        except FileNotFoundError:
            continue
        except OSError:
            logger.warning("Failed to clean temp path: %s", path)

    output_root = CLEANUP_OUTPUTS_DIR
    if not output_root.is_absolute():
        output_root = Path.cwd() / output_root
    if output_root.exists() and output_root.is_dir():
        for path in output_root.iterdir():
            try:
                if path.stat().st_mtime <= cutoff and is_within_cleanup_root(path, output_root):
                    remove_path(path)
                    removed += 1
            except FileNotFoundError:
                continue
            except OSError:
                logger.warning("Failed to clean output path: %s", path)
    if removed:
        logger.info("Cleaned %s old server file record(s).", removed)
    return removed


async def server_file_cleanup_loop() -> None:
    while True:
        await asyncio.sleep(CLEANUP_CHECK_SECONDS)
        await asyncio.to_thread(cleanup_server_files)


async def start_background_tasks(app: Application) -> None:
    if CLEANUP_ENABLED:
        await asyncio.to_thread(cleanup_server_files)
        app.bot_data["server_file_cleanup_task"] = asyncio.create_task(server_file_cleanup_loop())
    if REMOTE_OCR_ENABLED and REMOTE_OCR_URL:
        app.bot_data["remote_ocr_probe_task"] = asyncio.create_task(remote_ocr_probe_loop())


async def stop_background_tasks(app: Application) -> None:
    task = app.bot_data.get("server_file_cleanup_task")
    if isinstance(task, asyncio.Task):
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    remote_task = app.bot_data.get("remote_ocr_probe_task")
    if isinstance(remote_task, asyncio.Task):
        remote_task.cancel()
        try:
            await remote_task
        except asyncio.CancelledError:
            pass


def repair_digit(char: str) -> str:
    # Only used inside S07xxx, where xxx must be digits.
    return {
        "O": "0",
        "D": "0",
        "Q": "0",
        "U": "0",
        "I": "1",
        "L": "1",
        "T": "1",
        "Z": "2",
        "E": "3",
        "A": "4",
        "S": "5",
        "$": "5",
        "G": "6",
        "B": "8",
    }.get(char, char)


def repair_first_group(group: str) -> str:
    chars = list(group)
    if len(chars) >= 1 and chars[0] in {"5", "$", "P", "9"}:
        chars[0] = "S"
    if len(chars) >= 2 and chars[1] in {"O", "D", "Q", "U"}:
        chars[1] = "0"
    if len(chars) >= 3 and chars[2] in {"T", "I", "L", "/", "?"}:
        chars[2] = "7"
    for index in range(3, min(6, len(chars))):
        chars[index] = repair_digit(chars[index])
    return "".join(chars)


def valid_card(card: str) -> bool:
    return bool(
        re.fullmatch(r"S07[A-Z0-9]{3}-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{5}", card)
    )


def apply_builtin_pubg_correction(card: str) -> str:
    corrected = BUILTIN_PUBG_CARD_CORRECTIONS.get(card, card)
    if corrected != card:
        logger.info("Applied built-in PUBG OCR correction: %s -> %s", card, corrected)
    return corrected


def valid_psn_card(card: str) -> bool:
    return bool(re.fullmatch(r"[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}", card))


def psn_is_pubg_substring(psn: str, pubg_cards: list[str] | tuple[str, ...]) -> bool:
    key = psn_key(psn) or normalize_text(psn).strip()
    if not key:
        return False
    compact_key = key.replace("-", "")
    for pubg in pubg_cards:
        normalized_pubg = normalize_text(pubg).strip()
        if not valid_card(normalized_pubg):
            continue
        if key in normalized_pubg or compact_key in normalized_pubg.replace("-", ""):
            return True
    return False


def filter_psn_pubg_substrings(psn_lines: list[str], pubg_cards: list[str] | tuple[str, ...]) -> list[str]:
    if not pubg_cards:
        return psn_lines
    filtered: list[str] = []
    for line in psn_lines:
        if psn_is_pubg_substring(line, pubg_cards):
            continue
        filtered.append(line)
    return filtered


def add_card_candidate(cards: list[str], seen: set[str], first: str, second: str, third: str, fourth: str) -> None:
    card = apply_builtin_pubg_correction(f"{repair_first_group(first)}-{second}-{third}-{fourth}")
    if valid_card(card) and card not in seen:
        seen.add(card)
        cards.append(card)


@dataclass(frozen=True)
class OcrTextLine:
    text: str
    y: float
    x: float
    index: int


def text_without_s07_lines(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if "S07" not in normalize_text(line))


def is_pubg_image_text(text: str) -> bool:
    normalized = normalize_text(text)
    compact = re.sub(r"[^A-Z0-9$]", "", normalized)
    if PUBG_PREFIX_RE.search(normalized):
        return True
    if re.search(r"(?<![A-Z0-9])7[A-Z0-9]{3}[\s\-_|:锛氾紱;,.锛屻€倈]+[A-Z0-9]{4}[\s\-_|:锛氾紱;,.锛屻€倈]+[A-Z0-9]{4}", normalized):
        return True
    if "S07" in normalized:
        return True
    pubg_traces = ["S07", "507304", "907304", "SO7304", "$07304"]
    for prefix in PUBG_PREFIXES:
        suffix = prefix[1:]
        pubg_traces.extend((prefix, f"5{suffix}", f"9{suffix}", f"${suffix}", f"SO{suffix[1:]}"))
    if any(trace in compact for trace in pubg_traces):
        return True
    return bool(re.search(r"S0[A-Z0-9]304", compact))


def line_has_pubg_prefix(text: str) -> bool:
    return bool(PUBG_PREFIX_RE.search(normalize_text(text)))


def clean_pubg_fragment(text: str, *, from_prefix: bool) -> str:
    normalized = normalize_text(text)
    if from_prefix:
        match = PUBG_PREFIX_RE.search(normalized)
        if not match:
            return ""
        normalized = normalized[match.start() :]
    return re.sub(r"[^A-Z0-9-]", "", normalized)


def join_pubg_fragments(left: str, right: str) -> str:
    if not left:
        return right
    if not right:
        return left
    if left.endswith("-") or right.startswith("-"):
        return left + right
    return left + right


def rebuild_pubg_card_from_fragments(text: str) -> str:
    compact = re.sub(r"[^A-Z0-9]", "", normalize_text(text))
    match = PUBG_PREFIX_RE.search(compact)
    if not match:
        return ""
    compact = compact[match.start() :]
    if len(compact) < 19:
        return ""
    card = f"{compact[:6]}-{compact[6:10]}-{compact[10:14]}-{compact[14:19]}"
    card = apply_builtin_pubg_correction(card)
    return card if valid_card(card) else ""


def extract_cards(text: str) -> list[str]:
    text = normalize_text(text)
    sep = r"[\s\-_|:：；;,.，。|]+"
    shaped_pattern = (
        r"(?<![A-Z])"
        r"([SP5$][0ODQU][7TIL/?][0-9ODQUILTZEA$SGB]{3})"
        + sep
        + r"([A-Z0-9]{4})"
        + sep
        + r"([A-Z0-9]{4})"
        + sep
        + r"([A-Z0-9]{5})"
        + r"(?![A-Z0-9])"
    )

    cards: list[str] = []
    seen: set[str] = set()
    for first, second, third, fourth in re.findall(shaped_pattern, text):
        add_card_candidate(cards, seen, first, second, third, fourth)

    missing_s0_pattern = (
        r"(?<![A-Z0-9])"
        r"(7[A-Z0-9]{3})"
        + sep
        + r"([A-Z0-9]{4})"
        + sep
        + r"([A-Z0-9]{4})"
        + sep
        + r"([A-Z0-9]{5})"
        + r"(?![A-Z0-9])"
    )
    for tail, second, third, fourth in re.findall(missing_s0_pattern, text):
        add_card_candidate(cards, seen, f"S0{tail}", second, third, fourth)

    noisy_shaped_pattern = (
        r"(?<![A-Z])"
        r"([SP5$][0ODQU][7TIL/?][0-9ODQUILTZEA$SGB]{3})"
        + sep
        + r"([A-Z0-9]{4})"
        + sep
        + r"([A-Z0-9]{4})"
        + sep
        + r"([A-Z0-9]{5})"
    )
    for match in re.finditer(noisy_shaped_pattern, text):
        matched_text = match.group(0)
        if "|" not in matched_text:
            continue
        add_card_candidate(cards, seen, *match.groups())

    compact_pattern = (
        r"(?<![A-Z])"
        r"([SP5$][0ODQU][7TIL/?][0-9ODQUILTZEA$SGB]{3})"
        r"([A-Z0-9]{4})"
        r"([A-Z0-9]{4})"
        r"([A-Z0-9]{5})"
        r"(?![A-Z0-9])"
    )
    for first, second, third, fourth in re.findall(compact_pattern, text):
        add_card_candidate(cards, seen, first, second, third, fourth)

    loose_pattern = (
        r"(?<![A-Z])"
        r"([SP5$][0ODQU][7TIL/?][0-9ODQUILTZEA$SGB]{3})"
        r"((?:[\s\-_|:：；;,.，。|]*[A-Z0-9]){13})"
    )
    for first, rest in re.findall(loose_pattern, text):
        if "\n" not in rest and "|" not in rest:
            continue
        compact = re.sub(r"[^A-Z0-9]", "", rest)
        if len(compact) not in {12, 13}:
            continue
        add_card_candidate(cards, seen, first, compact[:4], compact[4:8], compact[8:])

    return cards


def ocr_item_text(item) -> str:
    if isinstance(item, dict):
        return str(item.get("text", "")).strip()
    return str(item).strip()


def ocr_item_xy(item) -> tuple[float, float]:
    if not isinstance(item, dict):
        return 0.0, 0.0
    box = (
        item.get("rec_box")
        or item.get("rec_boxes")
        or item.get("box")
        or item.get("bbox")
        or item.get("rect")
    )
    poly = item.get("rec_poly") or item.get("rec_polys") or item.get("poly") or item.get("points")
    if isinstance(box, (list, tuple)) and len(box) >= 4:
        try:
            return float(box[1]), float(box[0])
        except (TypeError, ValueError):
            pass
    if isinstance(poly, (list, tuple)) and poly:
        points = poly
        if len(points) == 1 and isinstance(points[0], (list, tuple)):
            points = points[0]
        xs: list[float] = []
        ys: list[float] = []
        for point in points:
            if isinstance(point, (list, tuple)) and len(point) >= 2:
                try:
                    xs.append(float(point[0]))
                    ys.append(float(point[1]))
                except (TypeError, ValueError):
                    continue
        if xs and ys:
            return min(ys), min(xs)
    return 0.0, 0.0


def ordered_ocr_text_lines(items: list | tuple) -> list[OcrTextLine]:
    lines: list[OcrTextLine] = []
    for index, item in enumerate(items or []):
        text = ocr_item_text(item)
        if not text:
            continue
        y, x = ocr_item_xy(item)
        lines.append(OcrTextLine(text=text, y=y, x=x, index=index))
    return sorted(lines, key=lambda line: (line.y, line.x, line.index))


def extract_cards_from_ordered_lines(lines: list[OcrTextLine]) -> tuple[list[str], bool]:
    cards: list[str] = []
    seen: set[str] = set()
    unresolved = False
    for index, line in enumerate(lines):
        line_cards = extract_cards(line.text)
        for card in line_cards:
            if card not in seen:
                seen.add(card)
                cards.append(card)
        if not is_pubg_image_text(line.text):
            continue
        if line_cards:
            continue
        current = clean_pubg_fragment(line.text, from_prefix=True)
        if not current:
            continue
        for end in range(index + 1, min(index + 4, len(lines))):
            next_line = lines[end]
            if line_has_pubg_prefix(next_line.text):
                unresolved = True
                logger.info(
                    "PUBG LINE WRAP UNRESOLVED: %s reason=next_pubg_prefix",
                    " + ".join(part.text for part in lines[index:end]),
                )
                break
            next_fragment = clean_pubg_fragment(next_line.text, from_prefix=False)
            current = join_pubg_fragments(current, next_fragment)
            card = rebuild_pubg_card_from_fragments(current)
            if not card:
                continue
            if card not in seen:
                seen.add(card)
                cards.append(card)
                logger.info(
                    "PUBG LINE WRAP MERGED: %s => %s",
                    " + ".join(part.text for part in lines[index : end + 1]),
                    card,
                )
            break
    return cards, unresolved
def extract_source_anchored_pubg_cards(raw_text: str) -> tuple[list[str], bool]:
    """Keep PUBG candidates tied to one OCR line or an adjacent line wrap."""
    return extract_cards_from_ordered_lines(ordered_ocr_text_lines(raw_text.splitlines()))


def pubg_card_prefix_key(card: str) -> tuple[str, str, str] | None:
    parts = card.split("-")
    if len(parts) != 4 or not valid_card(card):
        return None
    return parts[0], parts[1], parts[2]


def merge_text_rebuilt_and_worker_cards(
    text_cards: list[str],
    worker_cards: list[str],
    text_lines: list[str] | None = None,
) -> list[str]:
    blocked_prefix_keys = incomplete_pubg_prefix_keys(text_lines or [])
    merged = merge_text_and_worker_pubg_cards(text_cards, worker_cards, blocked_prefix_keys=blocked_prefix_keys)
    for dropped in merged.dropped:
        logger.info("PUBG WORKER CARD DROPPED: %s reason=%s", dropped.card, dropped.reason)
    return list(merged.cards)


def repair_psn_group(group: str, index: int) -> tuple[str, bool]:
    return group, False


def scan_psn_candidates(text: str, force: bool = False) -> list[tuple[str, bool]]:
    text = normalize_text(text)
    pubg_cards = extract_cards(text)
    text = text_without_s07_lines(text)
    pattern = (
        r"(?<![A-Z0-9-])"
        r"([A-Z0-9]{4})[\s_]*-[\s_]*([A-Z0-9]{4})[\s_]*-[\s_]*([A-Z0-9]{4})"
        r"(?![\s_]*-[\s_]*[A-Z0-9])"
        r"(?![A-Z0-9-])"
    )
    results: list[tuple[str, bool]] = []
    seen: set[str] = set()
    for match in re.finditer(pattern, text):
        if match.start() > 0 and text[match.start() - 1] == "-":
            continue
        candidate = "-".join(match.groups())
        if psn_is_pubg_substring(candidate, pubg_cards):
            continue
        if not candidate.startswith("S07") and candidate not in seen:
            seen.add(candidate)
            results.append((candidate, False))
    return results


def scan_labeled_psn_candidates(text: str) -> list[tuple[str, bool]]:
    text = normalize_text(text)
    pubg_cards = extract_cards(text)
    text = text_without_s07_lines(text)
    label_pattern = re.compile(r"(\u5361\s*\u53f7|\u5bc6\s*\u7801)")
    code_pattern = re.compile(
        r"([A-Z0-9]{4})[\s_]*-[\s_]*([A-Z0-9]{4})[\s_]*-[\s_]*([A-Z0-9]{4})"
        r"(?![\s_]*-[\s_]*[A-Z0-9])"
    )
    results: list[tuple[str, bool]] = []
    seen: set[str] = set()
    labels = list(label_pattern.finditer(text))
    for index, label_match in enumerate(labels):
        next_label_start = labels[index + 1].start() if index + 1 < len(labels) else len(text)
        after_label = text[label_match.end() : next_label_start]
        lines = after_label.splitlines()
        same_line = lines[0] if lines else after_label
        matches = list(code_pattern.finditer(same_line))
        if not matches:
            next_line = "\n".join(lines[1:2])
            matches = list(code_pattern.finditer(next_line))
        for match in matches[:1]:
            candidate = "-".join(match.groups())
            if psn_is_pubg_substring(candidate, pubg_cards):
                continue
            if candidate.startswith("S07") or candidate in seen:
                continue
            seen.add(candidate)
            results.append((candidate, False))
    return results


def extract_psn_ordered(text: str, force: bool = False) -> list[str]:
    if is_pubg_image_text(text):
        return []
    pubg_cards = extract_cards(text)
    labeled = scan_labeled_psn_candidates(text)
    if labeled:
        return filter_psn_pubg_substrings(psn_matches_to_lines(labeled), pubg_cards)
    return filter_psn_pubg_substrings(
        [
            f"{card}{FUZZY_SUFFIX}" if fuzzy and not card.endswith(FUZZY_SUFFIX) else card
            for card, fuzzy in scan_psn_candidates(text, force=force)
        ],
        pubg_cards,
    )


def extract_psn_cards(text: str, force: bool = False) -> list[str]:
    if is_pubg_image_text(text):
        return []
    pubg_cards = extract_cards(text)
    return filter_psn_pubg_substrings(
        [card for card, fuzzy in scan_psn_candidates(text, force=force) if not fuzzy and valid_psn_card(card)],
        pubg_cards,
    )


def psn_matches_to_lines(matches: list[tuple[str, bool]]) -> list[str]:
    return [
        f"{card}{FUZZY_SUFFIX}" if fuzzy and not card.endswith(FUZZY_SUFFIX) else card
        for card, fuzzy in matches
    ]


def prefer_labeled_psn_ordered(raw_chunks: list[str], fallback_ordered: list[str]) -> list[str]:
    raw_text = "\n".join(raw_chunks)
    if is_pubg_image_text(raw_text):
        return []
    pubg_cards = extract_cards(raw_text)
    labeled = scan_labeled_psn_candidates(raw_text)
    if labeled:
        return exact_unique_text(filter_psn_pubg_substrings(psn_matches_to_lines(labeled), pubg_cards))
    return exact_unique_text(filter_psn_pubg_substrings(fallback_ordered, pubg_cards))


def extract_uncertain_psn_cards(text: str, known_cards: list[str] | None = None, force: bool = False) -> list[str]:
    return []


def psn_ordered_for_image(raw_text: str, cards: list[str], psn_hint: bool = False) -> list[str]:
    if cards or is_pubg_image_text(raw_text):
        return []
    return exact_unique_text(extract_psn_ordered(raw_text, force=psn_hint))


def parse_psn_expected_count(caption: str) -> int | None:
    return None


def parse_pubg_expected_count(caption: str) -> int | None:
    return None


def limit_psn_ordered(cards: list[str], expected_count: int | None) -> list[str]:
    cards = unique_psn_lines(cards)
    if expected_count is None and MAX_PSN_PER_IMAGE > 0:
        expected_count = MAX_PSN_PER_IMAGE
    if expected_count is None or len(cards) <= expected_count:
        return cards
    exact = [card for card in cards if not card.endswith(FUZZY_SUFFIX)]
    if len(exact) >= expected_count:
        result: list[str] = []
        for card in cards:
            if not card.endswith(FUZZY_SUFFIX):
                result.append(card)
                if len(result) == expected_count:
                    return result
    return cards[:expected_count]


def exact_unique(cards: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for card in cards:
        if valid_card(card) and card not in seen:
            seen.add(card)
            result.append(card)
    return result


def exact_unique_psn(cards: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for card in cards:
        if valid_psn_card(card) and card not in seen:
            seen.add(card)
            result.append(card)
    return result


def exact_unique_text(cards: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for card in cards:
        if card and card not in seen:
            seen.add(card)
            result.append(card)
    return result


def psn_key(line: str) -> str | None:
    fuzzy = line.endswith(FUZZY_SUFFIX)
    card = line[: -len(FUZZY_SUFFIX)] if fuzzy else line
    card = normalize_text(card).strip()
    return card if valid_psn_card(card) else None


def unique_psn_lines(cards: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for line in cards:
        key = psn_key(line)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(f"{key}{FUZZY_SUFFIX}" if line.endswith(FUZZY_SUFFIX) else key)
    return result


def hamming_distance(left: str, right: str) -> int:
    return sum(a != b for a, b in zip(left, right))


def edit_distance_at_most(left: str, right: str, limit: int) -> bool:
    if abs(len(left) - len(right)) > limit:
        return False
    previous = list(range(len(right) + 1))
    for left_index, left_char in enumerate(left, start=1):
        current = [left_index]
        row_min = current[0]
        for right_index, right_char in enumerate(right, start=1):
            cost = 0 if left_char == right_char else 1
            value = min(
                previous[right_index] + 1,
                current[right_index - 1] + 1,
                previous[right_index - 1] + cost,
            )
            current.append(value)
            row_min = min(row_min, value)
        if row_min > limit:
            return False
        previous = current
    return previous[-1] <= limit


def almost_same(left: str, right: str) -> bool:
    left_compact = left.replace("-", "")
    right_compact = right.replace("-", "")
    return len(left_compact) == len(right_compact) and hamming_distance(left_compact, right_compact) <= 1


def likely_same_card(left: str, right: str) -> bool:
    left_compact = left.replace("-", "")
    right_compact = right.replace("-", "")
    if len(left_compact) == len(right_compact):
        return hamming_distance(left_compact, right_compact) <= 3
    if abs(len(left_compact) - len(right_compact)) == 1:
        return edit_distance_at_most(left_compact, right_compact, 2)
    return False


def likely_learned_variant(left: str, right: str) -> bool:
    left_compact = left.replace("-", "")
    right_compact = right.replace("-", "")
    if len(left_compact) != len(right_compact):
        return False
    return edit_distance_at_most(left_compact, right_compact, 6)


def merge_card_variants(cards: list[str]) -> tuple[list[str], int]:
    merged: list[str] = []
    suppressed = 0
    for card in exact_unique(cards):
        if any(likely_same_card(card, existing) for existing in merged):
            suppressed += 1
            continue
        merged.append(card)
    return merged, suppressed


def merge_without_guessing(primary: list[str], secondary: list[str]) -> tuple[list[str], int]:
    merged = exact_unique(primary)
    uncertain = 0
    for card in exact_unique(secondary):
        if card in merged:
            continue
        if any(likely_same_card(card, existing) for existing in merged):
            uncertain += 1
            continue
        merged.append(card)
    return merged, uncertain


def settle_image_cards(cards: list[str]) -> tuple[list[str], int]:
    return merge_card_variants(cards)


def settle_and_correct_pubg_cards(cards: list[str]) -> tuple[list[str], int, tuple[dict[str, str], ...]]:
    settled, uncertain = settle_image_cards(cards)
    correction = apply_pubg_char_corrections(settled, font_repository=font_repository)
    return list(correction.cards), uncertain, tuple(item.as_dict() for item in correction.corrections)


def filter_local_ocr_cards(cards: list[str], min_votes: int = LOCAL_OCR_MIN_CARD_VOTES) -> list[str]:
    if min_votes <= 1:
        return exact_unique(cards)
    counts = Counter(card for card in cards if valid_card(card))
    return [card for card in exact_unique(cards) if counts[card] >= min_votes]


def resize_for_ocr(image: Image.Image, max_side_limit: int, min_side_target: int) -> Image.Image:
    max_side = max(image.size)
    if max_side > max_side_limit:
        scale = max_side_limit / max_side
    elif max_side < min_side_target:
        scale = min_side_target / max_side
    else:
        return image
    return image.resize((int(image.width * scale), int(image.height * scale)), Image.Resampling.LANCZOS)


def prepare_ocrspace_image(image_path: Path) -> Path:
    with Image.open(image_path) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")

    image = resize_for_ocr(image, OCR_SPACE_MAX_SIDE, OCR_SPACE_MIN_SIDE)

    output_path = image_path.with_suffix(".ocrspace.png")
    image.save(output_path, format="PNG", optimize=True)
    if output_path.stat().st_size <= OCR_SPACE_MAX_UPLOAD_BYTES:
        return output_path

    output_path.unlink(missing_ok=True)
    jpeg_path = image_path.with_suffix(".ocrspace.jpg")
    working = image
    for max_side_target in (2600, 2200, 1800, 1500, 1200, 1000, 850):
        max_side = max(working.size)
        if max_side > max_side_target:
            scale = max_side_target / max_side
            working = working.resize(
                (max(1, int(working.width * scale)), max(1, int(working.height * scale))),
                Image.Resampling.LANCZOS,
            )
        for quality in (88, 78, 68, 58, 48):
            working.save(jpeg_path, format="JPEG", quality=quality, optimize=True)
            if jpeg_path.stat().st_size <= OCR_SPACE_MAX_UPLOAD_BYTES:
                return jpeg_path

    return jpeg_path


def run_ocrspace(
    image_path: Path,
    psn_hint: bool = False,
    psn_expected_count: int | None = None,
    pubg_expected_count: int | None = None,
) -> OcrResult:
    global ocrspace_cooldown_until
    if time.time() < ocrspace_cooldown_until:
        logger.warning("OCR.space is cooling down after rate limit.")
        return OcrResult(cards=tuple(), pubg_expected_count=pubg_expected_count, psn_expected_count=psn_expected_count)
    if not OCR_SPACE_API_KEYS:
        return OcrResult(cards=tuple(), pubg_expected_count=pubg_expected_count, psn_expected_count=psn_expected_count)

    upload_path: Path | None = None
    raw_chunks: list[str] = []
    all_cards: list[str] = []
    all_psn_ordered: list[str] = []
    ocr_stats = {
        "ocr_fixed_count": 0,
        "ocr_missing_count": 0,
        "ocr_false_negative": 0,
        "ocr_character_confusion": 0,
    }
    uncertain_total = 0
    try:
        upload_path = prepare_ocrspace_image(image_path)
        with httpx.Client(timeout=OCR_SPACE_TIMEOUT) as client:
            for engine in OCR_SPACE_ENGINES:
                response = None
                now = time.time()
                available_keys = [key for key in OCR_SPACE_API_KEYS if ocrspace_key_cooldowns.get(key, 0) <= now]
                if not available_keys:
                    ocrspace_cooldown_until = max(
                        ocrspace_cooldown_until,
                        min(ocrspace_key_cooldowns.values(), default=now + OCR_SPACE_429_COOLDOWN_SECONDS),
                    )
                    logger.warning("All OCR.space keys are cooling down.")
                    break
                for key_index, api_key in enumerate(available_keys, start=1):
                    with upload_path.open("rb") as image_file:
                        response = client.post(
                            "https://api.ocr.space/parse/image",
                            data={
                                "apikey": api_key,
                                "language": "eng",
                                "OCREngine": engine,
                                "scale": "true",
                                "detectOrientation": "true",
                                "isTable": "false",
                            },
                            files={
                                "file": (
                                    upload_path.name,
                                    image_file,
                                    "image/png" if upload_path.suffix.lower() == ".png" else "image/jpeg",
                                )
                            },
                        )
                    if response.status_code != 429:
                        break
                    ocrspace_key_cooldowns[api_key] = time.time() + OCR_SPACE_429_COOLDOWN_SECONDS
                    logger.warning(
                        "OCR.space key #%s rate limited; trying next key.",
                        key_index,
                    )
                    response = None
                if response is None:
                    continue
                response.raise_for_status()
                payload = response.json()
                if payload.get("IsErroredOnProcessing"):
                    logger.warning("OCR.space engine %s error: %s", engine, payload.get("ErrorMessage"))
                    continue

                chunks = [parsed.get("ParsedText", "") for parsed in payload.get("ParsedResults", []) or []]
                raw_text = "\n".join(chunk for chunk in chunks if chunk)
                if raw_text:
                    raw_chunks.append(raw_text)

                if is_pubg_image_text(raw_text):
                    legacy_cards, unresolved = extract_source_anchored_pubg_cards(raw_text)
                    enhanced_cards, enhanced_stats = [], {
                        "ocr_fixed_count": 0,
                        "ocr_missing_count": 0,
                        "ocr_false_negative": 0,
                        "ocr_character_confusion": 0,
                    }
                    uncertain_total += int(unresolved)
                else:
                    legacy_cards = extract_cards(raw_text)
                    enhanced_cards, enhanced_stats = enhanced_ocrspace_pubg_cards(raw_text, legacy_cards)
                ocr_stats = merge_ocr_stats(ocr_stats, enhanced_stats)
                cards, uncertain, card_corrections = settle_and_correct_pubg_cards(enhanced_cards + legacy_cards)
                psn_ordered = psn_ordered_for_image(raw_text, cards, psn_hint=psn_hint)
                all_cards.extend(cards)
                all_psn_ordered.extend(psn_ordered)
                uncertain_total += uncertain
        merged_cards, conflict_count = merge_card_variants(all_cards)
        psn_ordered = limit_psn_ordered(prefer_labeled_psn_ordered(raw_chunks, all_psn_ordered), psn_expected_count)
        psn_cards = exact_unique_psn([card for card in psn_ordered if not card.endswith(FUZZY_SUFFIX)])
        psn_uncertain = exact_unique_text([card for card in psn_ordered if card.endswith(FUZZY_SUFFIX)])
        uncertain_total += conflict_count
        corrected_merged_cards, correction_uncertain, card_corrections = settle_and_correct_pubg_cards(merged_cards)
        uncertain_total += correction_uncertain
        if corrected_merged_cards or psn_cards or psn_uncertain:
            return OcrResult(
                        cards=tuple(corrected_merged_cards),
                        psn_cards=tuple(psn_cards),
                        psn_uncertain=tuple(psn_uncertain),
                        psn_ordered=tuple(psn_ordered),
                        pubg_expected_count=pubg_expected_count,
                        psn_expected_count=psn_expected_count,
                        raw_text="\n".join(raw_chunks),
                        uncertain_count=uncertain_total,
                        ocr_fixed_count=ocr_stats["ocr_fixed_count"],
                        ocr_missing_count=ocr_stats["ocr_missing_count"],
                        ocr_false_negative=ocr_stats["ocr_false_negative"],
                        ocr_character_confusion=ocr_stats["ocr_character_confusion"],
                        corrections_applied=card_corrections,
                    )
    except Exception:
        logger.exception("OCR.space request failed")
        return OcrResult(cards=tuple())
    finally:
        if upload_path:
            upload_path.unlink(missing_ok=True)

    return OcrResult(
        cards=tuple(),
        psn_cards=tuple(),
        pubg_expected_count=pubg_expected_count,
        psn_expected_count=psn_expected_count,
        raw_text="\n".join(raw_chunks),
        uncertain_count=uncertain_total,
        ocr_fixed_count=ocr_stats["ocr_fixed_count"],
        ocr_missing_count=ocr_stats["ocr_missing_count"],
        ocr_false_negative=ocr_stats["ocr_false_negative"],
        ocr_character_confusion=ocr_stats["ocr_character_confusion"],
    )


def rotations_for(image: Image.Image) -> list[Image.Image]:
    return [image, image.rotate(90, expand=True), image.rotate(270, expand=True)]


def crop_regions(image: Image.Image) -> list[Image.Image]:
    width, height = image.size
    boxes = [
        (0, 0, width, height),
        (int(width * 0.03), int(height * 0.18), int(width * 0.97), int(height * 0.90)),
        (0, int(height * 0.25), int(width * 0.86), int(height * 0.78)),
        (0, int(height * 0.35), int(width * 0.82), int(height * 0.65)),
        (int(width * 0.02), int(height * 0.30), int(width * 0.72), int(height * 0.72)),
    ]
    return [image.crop(box) for box in boxes]


def enhance(region: Image.Image) -> Image.Image:
    gray = ImageOps.grayscale(region)
    contrast = ImageEnhance.Contrast(gray).enhance(1.9)
    return contrast.filter(ImageFilter.UnsharpMask(radius=1.2, percent=150, threshold=3))


def enhance_variants(region: Image.Image) -> list[Image.Image]:
    enhanced = enhance(region)
    threshold = enhanced.point(lambda pixel: 255 if pixel > 178 else 0)
    return [enhanced, threshold]


def iter_local_ocr_images(image_path: Path):
    with Image.open(image_path) as opened:
        source = ImageOps.exif_transpose(opened).convert("RGB")

    source = resize_for_ocr(source, OCR_MAX_SIDE, OCR_MIN_SIDE)

    for rotated in rotations_for(source):
        for region in crop_regions(rotated):
            yield from enhance_variants(region)


def run_local_ocr(
    image_path: Path,
    psn_hint: bool = False,
    psn_expected_count: int | None = None,
    pubg_expected_count: int | None = None,
) -> OcrResult:
    configs = [
        "--oem 3 --psm 6 --dpi 300 "
        "-c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-$",
        "--oem 3 --psm 7 --dpi 300 "
        "-c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-$",
    ]
    raw_chunks: list[str] = []
    cards: list[str] = []
    psn_cards: list[str] = []
    psn_uncertain: list[str] = []
    psn_ordered: list[str] = []

    for image in iter_local_ocr_images(image_path):
        for config in configs:
            try:
                text = pytesseract.image_to_string(image, lang="eng", config=config)
            except Exception:
                logger.exception("Local OCR failed")
                continue
            if text.strip():
                raw_chunks.append(text)
                if DEBUG_OCR:
                    logger.info("Local OCR text: %s", text.strip().replace("\n", " | ")[:500])
            text_cards = extract_cards(text)
            cards.extend(text_cards)
            ordered = psn_ordered_for_image(text, text_cards, psn_hint=psn_hint)
            psn_ordered.extend(ordered)
            psn_cards.extend(card for card in ordered if not card.endswith(FUZZY_SUFFIX))
            psn_uncertain.extend(card for card in ordered if card.endswith(FUZZY_SUFFIX))

    settled_cards, uncertain, card_corrections = settle_and_correct_pubg_cards(filter_local_ocr_cards(cards))
    return OcrResult(
        cards=tuple(settled_cards),
        psn_cards=tuple(exact_unique_psn(psn_cards)),
        psn_uncertain=tuple(exact_unique_text(psn_uncertain)),
        psn_ordered=tuple(limit_psn_ordered(prefer_labeled_psn_ordered(raw_chunks, psn_ordered), psn_expected_count)),
        pubg_expected_count=pubg_expected_count,
        psn_expected_count=psn_expected_count,
        raw_text="\n".join(raw_chunks),
        uncertain_count=uncertain,
        corrections_applied=card_corrections,
    )


def remote_ocr_now() -> datetime:
    return datetime.now(LEDGER_TZ)


def ensure_remote_ocr_today(now: datetime | None = None) -> None:
    now = now or remote_ocr_now()
    today = now.date().isoformat()
    if remote_ocr_status.get("today_date") == today:
        return
    remote_ocr_status.update(
        {
            "today_date": today,
            "today_remote_calls": 0,
            "today_remote_success": 0,
            "today_remote_failed": 0,
            "today_fallback_count": 0,
            "today_remote_latency_total_ms": 0,
            "today_enhanced_used": 0,
            "today_cache_hits": 0,
        }
    )


def record_remote_ocr_start() -> None:
    ensure_remote_ocr_today()
    remote_ocr_status["today_remote_calls"] += 1


def record_remote_ocr_fallback(reason: str) -> None:
    ensure_remote_ocr_today()
    remote_ocr_status["today_fallback_count"] += 1
    logger.info("OCRSPACE FALLBACK reason=%s", reason)


def remote_ocr_is_circuit_open(now: float | None = None) -> bool:
    current = time.time() if now is None else now
    return current < remote_ocr_offline_until


def remote_ocr_circuit_reason(now: float | None = None) -> str:
    current = time.time() if now is None else now
    remaining = max(0, int(remote_ocr_offline_until - current))
    if remaining <= 0:
        return "ok"
    return f"remote offline, retry in {remaining}s"


def mark_remote_ocr_offline(reason: str) -> None:
    global remote_ocr_offline_until
    remote_ocr_offline_until = max(remote_ocr_offline_until, time.time() + REMOTE_OCR_OFFLINE_SECONDS)
    remote_ocr_health_cache.update({"checked_at": time.time(), "result": (False, {}, reason)})
    logger.info("REMOTE OCR OFFLINE reason=%s retry_after=%ss", reason, REMOTE_OCR_OFFLINE_SECONDS)


def mark_remote_ocr_online() -> None:
    global remote_ocr_offline_until
    if remote_ocr_offline_until > 0:
        logger.info("REMOTE OCR ONLINE")
    remote_ocr_offline_until = 0.0


async def remote_ocr_probe_loop() -> None:
    while True:
        await asyncio.sleep(REMOTE_OCR_PROBE_SECONDS)
        if not REMOTE_OCR_ENABLED or not REMOTE_OCR_URL:
            continue
        if not remote_ocr_is_circuit_open():
            continue
        await asyncio.to_thread(remote_ocr_available, True)


def remote_ocr_fallback_reason() -> str:
    if not REMOTE_OCR_ENABLED:
        return "remote disabled"
    if not REMOTE_OCR_URL:
        return "remote url empty"
    if remote_ocr_is_circuit_open():
        return remote_ocr_circuit_reason()
    return remote_ocr_status.get("last_error") or "remote unavailable"


def avg_remote_latency_ms() -> int:
    ensure_remote_ocr_today()
    success_count = int(remote_ocr_status.get("today_remote_success", 0))
    if success_count <= 0:
        return 0
    return int(int(remote_ocr_status.get("today_remote_latency_total_ms", 0)) / success_count)


def percent_rate(part: int, total: int) -> str:
    if total <= 0:
        return "0.0%"
    return f"{(part / total) * 100:.1f}%"


def current_ocr_provider() -> str:
    if remote_ocr_status.get("last_ok"):
        return "RTX5070"
    if int(remote_ocr_status.get("today_fallback_count", 0)) > 0:
        return "OCR.space"
    return "unknown"


def safe_remote_url() -> str:
    url = REMOTE_OCR_URL.split("?", 1)[0].replace("http://", "").replace("https://", "")
    return url.rstrip("/")


def format_time_value(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return "无"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return text
    return parsed.astimezone(LEDGER_TZ).strftime("%H:%M:%S")


def process_memory_mb() -> str:
    try:
        if os.name == "posix":
            statm = Path("/proc/self/statm")
            if statm.exists():
                pages = int(statm.read_text(encoding="utf-8").split()[1])
                return f"{pages * os.sysconf('SC_PAGE_SIZE') / 1024 / 1024:.1f} MB"
    except Exception:
        pass
    return "unknown"


def process_uptime_text() -> str:
    seconds = max(0, int(time.time() - PROCESS_STARTED_AT))
    hours, remainder = divmod(seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    if hours:
        return f"{hours}小时{minutes}分钟"
    return f"{minutes}分钟"


def git_output(args: list[str]) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=Path("."),
            capture_output=True,
            text=True,
            timeout=1.5,
            check=False,
        )
    except Exception:
        return "unknown"
    value = (result.stdout or "").strip()
    return value or "unknown"


def service_active_state() -> str:
    if os.name != "posix":
        return "unknown"
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "telegram-card-platform"],
            capture_output=True,
            text=True,
            timeout=1.5,
            check=False,
        )
    except Exception:
        return "unknown"
    return (result.stdout or "").strip() or "unknown"


def remote_worker_health() -> tuple[bool, dict[str, object], str]:
    if not REMOTE_OCR_ENABLED or not REMOTE_OCR_URL:
        return False, {}, "disabled"
    now = time.time()
    cached = remote_ocr_health_cache.get("result")
    if (
        cached is not None
        and REMOTE_OCR_HEALTH_CACHE_SECONDS > 0
        and now - float(remote_ocr_health_cache.get("checked_at", 0.0)) <= REMOTE_OCR_HEALTH_CACHE_SECONDS
    ):
        return cached  # type: ignore[return-value]
    try:
        client = get_remote_http_client(1.5)
        response = client.get(f"{REMOTE_OCR_URL}/health")
        if response.status_code != 200:
            result = (False, {}, f"status={response.status_code}")
            remote_ocr_health_cache.update({"checked_at": now, "result": result})
            return result
        payload = response.json()
    except Exception as exc:
        result = (False, {}, exc.__class__.__name__)
        remote_ocr_health_cache.update({"checked_at": now, "result": result})
        return result
    if not isinstance(payload, dict):
        result = (False, {}, "invalid_json")
        remote_ocr_health_cache.update({"checked_at": now, "result": result})
        return result
    result = (True, payload, "ok")
    remote_ocr_health_cache.update({"checked_at": now, "result": result})
    return result


def get_remote_http_client(timeout: float | None = None) -> httpx.Client:
    global remote_http_client, remote_http_client_timeout
    target_timeout = float(timeout if timeout is not None else REMOTE_OCR_TIMEOUT)
    if remote_http_client is None or remote_http_client_timeout != target_timeout:
        if remote_http_client is not None:
            try:
                remote_http_client.close()
            except Exception:
                pass
        remote_http_client = httpx.Client(timeout=target_timeout)
        remote_http_client_timeout = target_timeout
    return remote_http_client


def close_remote_http_client() -> None:
    global remote_http_client, remote_http_client_timeout
    if remote_http_client is not None:
        try:
            remote_http_client.close()
        except Exception:
            pass
    remote_http_client = None
    remote_http_client_timeout = None


def today_cache_counts() -> dict[str, int]:
    summary = today_ocr_cache_summary(TODAY_OCR_CACHE_PATH)
    cards = list(summary.first_cards)
    raw_candidates: list[str] = []
    try:
        data = json.loads(TODAY_OCR_CACHE_PATH.read_text(encoding="utf-8")) if summary.exists else {}
        if isinstance(data, dict):
            cards = [str(card) for card in data.get("ocr_cards", []) if isinstance(card, str)]
            raw_candidates = [str(card) for card in data.get("raw_candidates", []) if isinstance(card, str)]
    except Exception:
        raw_candidates = []
    pubg_count = sum(1 for card in cards if valid_card(card))
    psn_count = sum(1 for card in cards if re.fullmatch(r"[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}", card))
    seen: set[str] = set()
    duplicate_count = 0
    for card in raw_candidates:
        key = canonical_card(card)
        if not key:
            continue
        if key in seen:
            duplicate_count += 1
        seen.add(key)
    return {
        "images": summary.images,
        "cards": len(cards),
        "pubg": pubg_count,
        "psn": psn_count,
        "duplicates": duplicate_count,
    }


def build_status_panel() -> str:
    ensure_remote_ocr_today()
    worker_ok, worker_payload, worker_error = remote_worker_health()
    remote_ocr_status["remote_health"] = worker_ok
    remote_calls = int(remote_ocr_status["today_remote_calls"])
    cache_counts = today_cache_counts()
    service_state = service_active_state()
    branch = git_output(["branch", "--show-current"])
    commit = git_output(["rev-parse", "--short", "HEAD"])
    worker_status = str(worker_payload.get("status", "ok" if worker_ok else "offline"))
    worker_gpu = str(worker_payload.get("gpu", "unknown"))
    worker_engine = str(worker_payload.get("engine", "unknown"))
    extra_fields = []
    for key in ("pipeline_loaded", "opencv", "cached", "stats"):
        if key in worker_payload:
            extra_fields.append(f"{key}: {worker_payload[key]}")
    worker_extra = "\n".join(extra_fields)
    current_provider = "RTX5070" if worker_ok else "OCR.space"
    lines = [
        "━━━━━━━━━━━━━━",
        "📊 机器人状态",
        "━━━━━━━━━━━━━━",
        "",
        "🤖 阿里云机器人",
        f"状态：{'运行中' if service_state == 'active' else service_state}",
        "版本：v2.2-status-panel",
        f"服务：telegram-card-platform {service_state}{'/running' if service_state == 'active' else ''}",
        f"分支：{branch}",
        f"Commit：{commit}",
        f"内存：{process_memory_mb()}",
        f"运行时间：{process_uptime_text()}",
        f"ledger.sqlite3：{'存在' if LEDGER_DB_PATH.exists() else '缺失'}",
        "",
        "🖥 本地 RTX5070 OCR",
        f"启用：{'是' if REMOTE_OCR_ENABLED else '否'}",
        f"状态：{'在线' if worker_ok else '离线'}",
        f"status：{worker_status if worker_ok else worker_error}",
        f"GPU：{worker_gpu}",
        f"引擎：{worker_engine}",
        f"地址：{safe_remote_url()}",
        f"平均延迟：{avg_remote_latency_ms()} ms",
        f"最近成功：{format_time_value(remote_ocr_status.get('last_success_at'))}",
        f"最近失败：{format_time_value(remote_ocr_status.get('last_failed_at'))}",
        f"最近错误：{remote_ocr_status.get('last_error') or '无'}",
    ]
    if worker_extra:
        lines.append(worker_extra)
    lines.extend(
        [
            "",
            "🔁 OCR 路由",
            f"当前主引擎：{current_provider}",
            "备用引擎：OCR.space",
            f"OCR.space fallback：{'可用' if OCR_SPACE_API_KEYS else '未配置'}",
            f"今日 Remote 调用：{remote_calls}",
            f"成功：{remote_ocr_status['today_remote_success']}",
            f"失败：{remote_ocr_status['today_remote_failed']}",
            f"Fallback：{remote_ocr_status['today_fallback_count']}",
            f"缓存命中率：{percent_rate(int(remote_ocr_status['today_cache_hits']), remote_calls)}",
            f"OpenCV增强率：{percent_rate(int(remote_ocr_status['today_enhanced_used']), remote_calls)}",
            "",
            "📦 今日识别",
            f"图片：{cache_counts['images']} 张",
            f"卡密：{cache_counts['cards']} 个",
            f"PUBG卡密：{cache_counts['pubg']} 个",
            f"PSN卡密：{cache_counts['psn']} 个",
            f"重复：{cache_counts['duplicates']} 个",
            "",
            "━━━━━━━━━━━━━━",
        ]
    )
    return "\n".join(lines)


def record_remote_ocr_status(
    ok: bool,
    latency_ms: int,
    card_count: int = 0,
    text_count: int = 0,
    error: str = "",
    health_check: bool = False,
    enhanced_used: bool = False,
    cache_hit: bool = False,
) -> None:
    now = remote_ocr_now()
    ensure_remote_ocr_today(now)
    if health_check:
        remote_ocr_status["remote_health"] = ok
        remote_ocr_status["last_checked_at"] = now.isoformat(timespec="seconds")
        if ok:
            logger.info("REMOTE OCR HEALTH OK")
        else:
            logger.info("REMOTE OCR HEALTH FAILED reason=%s", error)
        return

    if ok:
        remote_ocr_status["today_remote_success"] += 1
        remote_ocr_status["today_remote_latency_total_ms"] += latency_ms
        if enhanced_used:
            remote_ocr_status["today_enhanced_used"] += 1
        if cache_hit:
            remote_ocr_status["today_cache_hits"] += 1
        remote_ocr_status["last_success_at"] = now.isoformat(timespec="seconds")
        logger.info(
            "REMOTE OCR SUCCESS latency_ms=%s cards=%s texts=%s enhanced_used=%s",
            latency_ms,
            card_count,
            text_count,
            str(enhanced_used).lower(),
        )
    else:
        remote_ocr_status["today_remote_failed"] += 1
        remote_ocr_status["last_failed_at"] = now.isoformat(timespec="seconds")
        logger.info("REMOTE OCR FAILED reason=%s", error)
    remote_ocr_status.update(
        {
            "last_ok": ok,
            "last_error": error[:200],
            "last_latency_ms": latency_ms,
            "last_card_count": card_count,
            "last_checked_at": now.isoformat(timespec="seconds"),
        }
    )


def remote_ocr_available(force_probe: bool = False) -> tuple[bool, str]:
    if not REMOTE_OCR_ENABLED or not REMOTE_OCR_URL:
        return False, "disabled"
    if remote_ocr_is_circuit_open() and not force_probe:
        return False, remote_ocr_circuit_reason()
    cached = remote_ocr_health_cache.get("result")
    now = time.time()
    if (
        not force_probe
        and
        cached is not None
        and REMOTE_OCR_HEALTH_CACHE_SECONDS > 0
        and now - float(remote_ocr_health_cache.get("checked_at", 0.0)) <= REMOTE_OCR_HEALTH_CACHE_SECONDS
    ):
        ok, _payload, reason = cached  # type: ignore[misc]
        return bool(ok), str(reason)
    start = time.time()
    try:
        client = get_remote_http_client(REMOTE_OCR_TIMEOUT)
        response = client.get(f"{REMOTE_OCR_URL}/health")
        latency_ms = int((time.time() - start) * 1000)
        if response.status_code != 200:
            record_remote_ocr_status(False, latency_ms, error=f"health status {response.status_code}", health_check=True)
            mark_remote_ocr_offline(f"health status {response.status_code}")
            remote_ocr_health_cache.update({"checked_at": start, "result": (False, {}, f"status={response.status_code}")})
            return False, f"status={response.status_code}"
        record_remote_ocr_status(True, latency_ms, card_count=remote_ocr_status.get("last_card_count", 0), health_check=True)
        mark_remote_ocr_online()
        payload = response.json()
        remote_ocr_health_cache.update({"checked_at": start, "result": (True, payload if isinstance(payload, dict) else {}, "ok")})
        return True, "ok"
    except Exception as exc:
        latency_ms = int((time.time() - start) * 1000)
        record_remote_ocr_status(False, latency_ms, error=type(exc).__name__, health_check=True)
        mark_remote_ocr_offline(type(exc).__name__)
        remote_ocr_health_cache.update({"checked_at": start, "result": (False, {}, type(exc).__name__)})
        return False, type(exc).__name__


def run_remote_ocr(
    image_path: Path,
    psn_hint: bool = False,
    psn_expected_count: int | None = None,
    pubg_expected_count: int | None = None,
) -> OcrResult | None:
    if not REMOTE_OCR_ENABLED or not REMOTE_OCR_URL:
        return None
    if remote_ocr_is_circuit_open():
        logger.info("REMOTE OCR SKIP reason=%s", remote_ocr_circuit_reason())
        return None

    start = time.time()
    record_remote_ocr_start()
    logger.info("REMOTE OCR START url=%s", REMOTE_OCR_URL)
    try:
        client = get_remote_http_client(REMOTE_OCR_TIMEOUT)
        with image_path.open("rb") as image_file:
            response = client.post(
                f"{REMOTE_OCR_URL}/ocr",
                files={"file": (image_path.name, image_file, "image/jpeg")},
        )
        latency_ms = int((time.time() - start) * 1000)
        if response.status_code != 200:
            record_remote_ocr_status(False, latency_ms, error=f"status {response.status_code}")
            mark_remote_ocr_offline(f"status {response.status_code}")
            return None
        payload = response.json()
        if payload.get("ok") is not True:
            record_remote_ocr_status(False, latency_ms, error="ok=false")
            return None
        worker_cards = payload.get("cards")
        if not isinstance(worker_cards, list):
            worker_cards = []

        text_items = payload.get("texts", []) or []
        ordered_lines = ordered_ocr_text_lines(text_items)
        ordered_line_cards, has_unresolved_pubg_fragment = extract_cards_from_ordered_lines(ordered_lines)
        text_values: list[str] = []
        for item in text_items:
            if isinstance(item, dict):
                value = str(item.get("text", "")).strip()
            else:
                value = str(item).strip()
            if value:
                text_values.append(value)
        for item in worker_cards:
            if isinstance(item, dict):
                value = str(item.get("text", "")).strip()
            else:
                value = str(item).strip()
            if value:
                text_values.append(value)

        raw_text = "\n".join(text_values)
        text_raw = "\n".join(line.text for line in ordered_lines)
        worker_text = "\n".join(ocr_item_text(item) for item in worker_cards)
        if ordered_lines and is_pubg_image_text(text_raw):
            extracted_cards = merge_text_rebuilt_and_worker_cards(
                ordered_line_cards,
                extract_cards(worker_text),
                [line.text for line in ordered_lines],
            )
        else:
            extracted_cards = ordered_line_cards or extract_cards(raw_text)
        cards, uncertain, card_corrections = settle_and_correct_pubg_cards(extracted_cards)
        psn_ordered = limit_psn_ordered(psn_ordered_for_image(raw_text, cards, psn_hint=psn_hint), psn_expected_count)
        psn_cards = exact_unique_psn([card for card in psn_ordered if not card.endswith(FUZZY_SUFFIX)])
        psn_uncertain = exact_unique_text([card for card in psn_ordered if card.endswith(FUZZY_SUFFIX)])
        if not cards and not psn_cards and not psn_uncertain:
            record_remote_ocr_status(False, latency_ms, error="no valid cards")
            return None

        card_count = len(cards) + len(psn_cards) + len(psn_uncertain)
        mark_remote_ocr_online()
        record_remote_ocr_status(
            True,
            latency_ms,
            card_count=card_count,
            text_count=len(text_values),
            enhanced_used=bool(payload.get("enhanced_used")),
            cache_hit=bool(payload.get("cached")),
        )
        return OcrResult(
            cards=tuple(cards),
            psn_cards=tuple(psn_cards),
            psn_uncertain=tuple(psn_uncertain),
            psn_ordered=tuple(psn_ordered),
            pubg_expected_count=pubg_expected_count,
            psn_expected_count=psn_expected_count,
            raw_text=raw_text,
            uncertain_count=uncertain,
            corrections_applied=card_corrections,
            has_unresolved_pubg_fragment=has_unresolved_pubg_fragment,
        )
    except Exception as exc:
        latency_ms = int((time.time() - start) * 1000)
        record_remote_ocr_status(False, latency_ms, error=type(exc).__name__)
        mark_remote_ocr_offline(type(exc).__name__)
        return None


def run_ocr(
    image_path: Path,
    psn_hint: bool = False,
    psn_expected_count: int | None = None,
    pubg_expected_count: int | None = None,
) -> OcrResult:
    remote = run_remote_ocr(
        image_path,
        psn_hint=psn_hint,
        psn_expected_count=psn_expected_count,
        pubg_expected_count=pubg_expected_count,
    )
    if (
        remote is not None
        and remote.has_unresolved_pubg_fragment
        and OCR_PROVIDER == "ocrspace"
        and OCR_SPACE_API_KEYS
    ):
        record_remote_ocr_fallback("remote unresolved pubg fragment")
        fallback = run_ocrspace(
            image_path,
            psn_hint=psn_hint,
            psn_expected_count=psn_expected_count,
            pubg_expected_count=pubg_expected_count,
        )
        merged, conflict_count = merge_without_guessing(list(fallback.cards), list(remote.cards))
        settled_cards, correction_conflicts, card_corrections = settle_and_correct_pubg_cards(merged)
        merged_psn = exact_unique_psn(list(remote.psn_cards) + list(fallback.psn_cards))
        merged_psn_uncertain = exact_unique_text(list(remote.psn_uncertain) + list(fallback.psn_uncertain))
        merged_psn_ordered = limit_psn_ordered(list(remote.psn_ordered) + list(fallback.psn_ordered), psn_expected_count)
        merged_raw_text = remote.raw_text + "\n" + fallback.raw_text
        if settled_cards or is_pubg_image_text(merged_raw_text):
            merged_psn = []
            merged_psn_uncertain = []
            merged_psn_ordered = []
        if settled_cards or merged_psn or merged_psn_uncertain:
            return OcrResult(
                cards=tuple(settled_cards),
                psn_cards=tuple(merged_psn),
                psn_uncertain=tuple(merged_psn_uncertain),
                psn_ordered=tuple(merged_psn_ordered),
                pubg_expected_count=pubg_expected_count,
                psn_expected_count=psn_expected_count,
                raw_text=merged_raw_text,
                uncertain_count=remote.uncertain_count + fallback.uncertain_count + conflict_count + correction_conflicts,
                ocr_fixed_count=remote.ocr_fixed_count + fallback.ocr_fixed_count,
                ocr_missing_count=remote.ocr_missing_count + fallback.ocr_missing_count,
                ocr_false_negative=remote.ocr_false_negative + fallback.ocr_false_negative,
                ocr_character_confusion=remote.ocr_character_confusion + fallback.ocr_character_confusion,
                corrections_applied=tuple(
                    list(remote.corrections_applied)
                    + list(fallback.corrections_applied)
                    + list(card_corrections)
                ),
            )

    if remote is not None:
        return remote

    if OCR_PROVIDER == "ocrspace" and OCR_SPACE_API_KEYS:
        record_remote_ocr_fallback(remote_ocr_fallback_reason())
        remote = run_ocrspace(
            image_path,
            psn_hint=psn_hint,
            psn_expected_count=psn_expected_count,
            pubg_expected_count=pubg_expected_count,
        )
        if (remote.cards or remote.psn_cards or remote.psn_uncertain) and not VERIFY_WITH_LOCAL and not LOCAL_COMPLEMENT:
            return remote
        if not LOCAL_FALLBACK and not VERIFY_WITH_LOCAL:
            return remote

        local = run_local_ocr(
            image_path,
            psn_hint=psn_hint,
            psn_expected_count=psn_expected_count,
            pubg_expected_count=pubg_expected_count,
        )
        merged, uncertain = merge_without_guessing(list(remote.cards), list(local.cards))
        merged_psn = exact_unique_psn(list(remote.psn_cards) + list(local.psn_cards))
        merged_psn_uncertain = exact_unique_text(list(remote.psn_uncertain) + list(local.psn_uncertain))
        merged_psn_ordered = limit_psn_ordered(list(remote.psn_ordered) + list(local.psn_ordered), psn_expected_count)
        settled_cards, conflict_count, card_corrections = settle_and_correct_pubg_cards(merged)
        merged_raw_text = remote.raw_text + "\n" + local.raw_text
        if settled_cards or is_pubg_image_text(merged_raw_text):
            merged_psn = []
            merged_psn_uncertain = []
            merged_psn_ordered = []
        uncertain += remote.uncertain_count + local.uncertain_count + conflict_count
        if settled_cards or merged_psn or merged_psn_uncertain:
            return OcrResult(
                cards=tuple(settled_cards),
                psn_cards=tuple(merged_psn),
                psn_uncertain=tuple(merged_psn_uncertain),
                psn_ordered=tuple(merged_psn_ordered),
                pubg_expected_count=pubg_expected_count,
                psn_expected_count=psn_expected_count,
                raw_text=merged_raw_text,
                uncertain_count=uncertain,
                ocr_fixed_count=remote.ocr_fixed_count + local.ocr_fixed_count,
                ocr_missing_count=remote.ocr_missing_count + local.ocr_missing_count,
                ocr_false_negative=remote.ocr_false_negative + local.ocr_false_negative,
                ocr_character_confusion=remote.ocr_character_confusion + local.ocr_character_confusion,
                corrections_applied=tuple(list(remote.corrections_applied) + list(local.corrections_applied) + list(card_corrections)),
            )
        return OcrResult(
            cards=tuple(),
            psn_cards=tuple(),
            psn_uncertain=tuple(merged_psn_uncertain),
            psn_ordered=tuple(merged_psn_ordered),
            pubg_expected_count=pubg_expected_count,
            psn_expected_count=psn_expected_count,
            raw_text=merged_raw_text,
            uncertain_count=uncertain,
            ocr_fixed_count=remote.ocr_fixed_count + local.ocr_fixed_count,
            ocr_missing_count=remote.ocr_missing_count + local.ocr_missing_count,
            ocr_false_negative=remote.ocr_false_negative + local.ocr_false_negative,
            ocr_character_confusion=remote.ocr_character_confusion + local.ocr_character_confusion,
            corrections_applied=tuple(list(remote.corrections_applied) + list(local.corrections_applied)),
        )

    return run_local_ocr(
        image_path,
        psn_hint=psn_hint,
        psn_expected_count=psn_expected_count,
        pubg_expected_count=pubg_expected_count,
    )


async def download_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Path:
    if not update.message or not update.message.photo:
        raise ValueError("No photo received")
    photo = update.message.photo[-1]
    tg_file = await context.bot.get_file(photo.file_id)
    temp_dir = Path(tempfile.mkdtemp(prefix="s07_card_"))
    image_path = temp_dir / f"{photo.file_unique_id}.jpg"
    await tg_file.download_to_drive(custom_path=image_path)
    return image_path


async def download_message_photo(message, context: ContextTypes.DEFAULT_TYPE) -> Path:
    photo_sizes = getattr(message, "photo", None)
    if not photo_sizes:
        raise ValueError("No photo received")
    photo = photo_sizes[-1]
    tg_file = await context.bot.get_file(photo.file_id)
    temp_dir = Path(tempfile.mkdtemp(prefix="s07_learn_"))
    image_path = temp_dir / f"{photo.file_unique_id}.jpg"
    await tg_file.download_to_drive(custom_path=image_path)
    return image_path


async def recognize_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> OcrResult:
    image_path = await download_photo(update, context)
    caption = update.message.caption if update.message and update.message.caption else ""
    psn_hint = "PSN" in normalize_text(caption)
    psn_expected_count = parse_psn_expected_count(caption)
    pubg_expected_count = parse_pubg_expected_count(caption)
    try:
        async with ocr_semaphore:
            result = await asyncio.to_thread(run_ocr, image_path, psn_hint, psn_expected_count, pubg_expected_count)
            return replace(result, source_caption=caption.strip())
    finally:
        try:
            image_path.unlink(missing_ok=True)
            image_path.parent.rmdir()
        except OSError:
            pass


async def assign_photo_sequence(update: Update) -> int:
    global global_photo_sequence
    key = id(update)
    async with photo_sequence_lock:
        existing = photo_sequence_by_update.get(key)
        if existing is not None:
            return existing
        global_photo_sequence += 1
        photo_sequence_by_update[key] = global_photo_sequence
        return global_photo_sequence


def photo_sequence(update: Update) -> int:
    return photo_sequence_by_update.get(id(update), 0)


def photo_display_order(update: Update) -> tuple[int, int]:
    message = getattr(update, "message", None)
    message_id = getattr(message, "message_id", None)
    if isinstance(message_id, int):
        return message_id, photo_sequence(update)
    return 10**12, photo_sequence(update)


def result_location(index: int, result: OcrResult) -> str:
    return f"第{index}张"


def format_duplicate_lines(groups: list[tuple[int, list[int]]]) -> list[str]:
    lines: list[str] = []
    for first_index, duplicate_indexes in groups:
        unique_duplicates = sorted(dict.fromkeys(index for index in duplicate_indexes if index != first_index))
        if not unique_duplicates:
            continue
        duplicate_text = "".join(f"第{index}张" for index in unique_duplicates)
        lines.append(f"重复卡密：{duplicate_text}与第{first_index}张重复")
    return lines


def format_card_code(card: str) -> str:
    return f"<code>{html.escape(card)}</code>"


def format_card_codes(cards: list[str]) -> str:
    if len(cards) == 1:
        return format_card_code(cards[0])
    return f"<pre>{html.escape(chr(10).join(cards))}</pre>"


def format_underlined_card_code(card: str) -> str:
    return f"<u>{html.escape(card)}</u>"


def source_username_only(source_user: str) -> str:
    match = re.search(r"@[A-Za-z0-9_]+", source_user)
    if match:
        return match.group(0)
    parts = [part.strip() for part in source_user.split("|") if part.strip()]
    return parts[0] if parts else source_user.strip() or "Unknown"


def ordered_pubg_occurrences(results: list[OcrResult]) -> list[OrderedCardOccurrence]:
    occurrences: list[OrderedCardOccurrence] = []
    for image_index, result in enumerate(results, start=1):
        sequence_index = result.sequence_index or image_index
        if result.card_locations:
            for card, y, x in result.card_locations:
                key = canonical_card(card)
                if key and valid_card(card):
                    occurrences.append(OrderedCardOccurrence(card=card, image_index=sequence_index, y=int(y), x=int(x), duplicate_key=key))
            continue
        for y, card in enumerate(result.cards):
            key = canonical_card(card)
            if key and valid_card(card):
                occurrences.append(OrderedCardOccurrence(card=card, image_index=sequence_index, y=y, x=0, duplicate_key=key))
    return sorted(occurrences, key=lambda item: (item.image_index, item.y, item.x))


def ordered_psn_occurrences(results: list[OcrResult]) -> list[OrderedCardOccurrence]:
    occurrences: list[OrderedCardOccurrence] = []
    all_pubg_cards = [card for result in results for card in result.cards if valid_card(card)]
    for image_index, result in enumerate(results, start=1):
        sequence_index = result.sequence_index or image_index
        if result.psn_locations:
            for line, y, x in result.psn_locations:
                key = psn_key(line)
                if not key:
                    continue
                if psn_is_pubg_substring(key, all_pubg_cards):
                    continue
                display = f"{key}{FUZZY_SUFFIX}" if line.endswith(FUZZY_SUFFIX) else key
                occurrences.append(OrderedCardOccurrence(card=key, image_index=sequence_index, y=int(y), x=int(x), duplicate_key=key, display=display))
            continue
        if result.psn_ordered:
            ordered_psn = list(result.psn_ordered)
        else:
            ordered_psn = exact_unique_psn(list(result.psn_cards)) + exact_unique_text(list(result.psn_uncertain))
        ordered_psn = filter_psn_pubg_substrings(ordered_psn, all_pubg_cards)
        ordered_psn = limit_psn_ordered(ordered_psn, result.psn_expected_count)
        for y, line in enumerate(ordered_psn):
            key = psn_key(line)
            if not key:
                continue
            if psn_is_pubg_substring(key, all_pubg_cards):
                continue
            display = f"{key}{FUZZY_SUFFIX}" if line.endswith(FUZZY_SUFFIX) else key
            occurrences.append(OrderedCardOccurrence(card=key, image_index=sequence_index, y=y, x=0, duplicate_key=key, display=display))
    return sorted(occurrences, key=lambda item: (item.image_index, item.y, item.x))


def format_reply(results: list[OcrResult]) -> str:
    pubg_occurrences = ordered_pubg_occurrences(results)
    psn_occurrences = ordered_psn_occurrences(results)
    conflict_lines: list[str] = []
    expected_pubg_total = 0
    expected_psn_total = 0
    pubg_image_count = 0
    psn_image_count = 0
    uncertain_count = 0
    for index, result in enumerate(results, start=1):
        image_pubg = [item for item in pubg_occurrences if item.image_index == index]
        image_psn = [item for item in psn_occurrences if item.image_index == index]
        if image_pubg:
            pubg_image_count += 1
        if image_psn:
            psn_image_count += 1
        if result.pubg_expected_count:
            expected_pubg_total += result.pubg_expected_count
        if result.psn_expected_count:
            expected_psn_total += result.psn_expected_count
        if result.uncertain_count:
            conflict_lines.append(f"{result_location(index, result)}：{UNCERTAIN_PREFIX}{result.uncertain_count}{UNCERTAIN_SUFFIX}")
        uncertain_count += result.uncertain_count

    pubg_cards: list[str] = []
    seen_pubg: dict[str, int] = {}
    pubg_duplicate_groups: dict[int, list[int]] = {}
    for occurrence in pubg_occurrences:
        if not valid_card(occurrence.card):
            continue
        if occurrence.duplicate_key not in seen_pubg:
            seen_pubg[occurrence.duplicate_key] = occurrence.image_index
            pubg_cards.append(occurrence.card)
            continue
        pubg_duplicate_groups.setdefault(seen_pubg[occurrence.duplicate_key], []).append(occurrence.image_index)

    psn_lines: list[str] = []
    seen_psn: dict[str, int] = {}
    psn_duplicate_groups: dict[int, list[int]] = {}
    for occurrence in psn_occurrences:
        if occurrence.duplicate_key not in seen_psn:
            seen_psn[occurrence.duplicate_key] = occurrence.image_index
            psn_lines.append(occurrence.display)
            continue
        psn_duplicate_groups.setdefault(seen_psn[occurrence.duplicate_key], []).append(occurrence.image_index)
    psn_cards = exact_unique_psn([card for card in psn_lines if not card.endswith(FUZZY_SUFFIX)])
    psn_uncertain = exact_unique_text([card for card in psn_lines if card.endswith(FUZZY_SUFFIX)])
    pubg_duplicate_lines = format_duplicate_lines(list(pubg_duplicate_groups.items()))
    psn_duplicate_lines = format_duplicate_lines(list(psn_duplicate_groups.items()))

    sections: list[str] = []
    if pubg_cards:
        pubg_summary = (
            f"<b>{SUCCESS_PREFIX}{PUBG_LABEL}：{len(pubg_cards)}{COUNT_SUFFIX}（点击卡密复制）</b>\n"
            f"\u672c\u6b21\u8bc6\u522bPUBG\u56fe\u7247\uff1a{pubg_image_count}\u5f20"
        )
        if expected_pubg_total and len(pubg_cards) < expected_pubg_total:
            pubg_summary += f"\n{MANUAL_REVIEW_SUMMARY}{PUBG_LABEL}{expected_pubg_total - len(pubg_cards)}{COUNT_SUFFIX}"
        if pubg_duplicate_lines:
            pubg_summary += "\n" + "\n".join(pubg_duplicate_lines)
        sections.append(f"<b>【{PUBG_LABEL}】</b>\n\n{format_card_codes(pubg_cards)}\n\n{pubg_summary}")

    if psn_lines:
        psn_summary = (
            f"<b>{SUCCESS_PREFIX}{PSN_LABEL}：{len(psn_cards)}{COUNT_SUFFIX}（点击卡密复制）</b>\n"
            f"\u672c\u6b21\u8bc6\u522bPSN\u56fe\u7247\uff1a{psn_image_count}\u5f20"
        )
        if psn_uncertain:
            psn_summary += f"\n{MANUAL_REVIEW_SUMMARY}{PSN_LABEL}{len(psn_uncertain)}{COUNT_SUFFIX}"
        if expected_psn_total and len(psn_lines) < expected_psn_total:
            psn_summary += f"\n{MANUAL_REVIEW_SUMMARY}{PSN_LABEL}{expected_psn_total - len(psn_lines)}{COUNT_SUFFIX}"
        if psn_duplicate_lines:
            psn_summary += "\n" + "\n".join(psn_duplicate_lines)
        sections.append(f"<b>【{PSN_LABEL}】</b>\n\n{format_card_codes(psn_lines)}\n\n{psn_summary}")

    if uncertain_count:
        if conflict_lines:
            sections.append(f"{UNCERTAIN_PREFIX}\n<pre>{html.escape(chr(10).join(conflict_lines))}</pre>\n{UNCERTAIN_PREFIX}{uncertain_count}{UNCERTAIN_SUFFIX}")
        else:
            sections.append(f"{UNCERTAIN_PREFIX}{uncertain_count}{UNCERTAIN_SUFFIX}")
    if not sections:
        sections.append("\u672a\u8bc6\u522b\u5230\u5361\u5bc6")
    return "\n\n".join(sections)


def result_card_lines(results: list[OcrResult]) -> tuple[list[str], list[str]]:
    pubg_cards: list[str] = []
    psn_lines: list[str] = []
    seen_pubg: set[str] = set()
    for occurrence in ordered_pubg_occurrences(results):
        if occurrence.duplicate_key in seen_pubg:
            continue
        seen_pubg.add(occurrence.duplicate_key)
        pubg_cards.append(occurrence.card)
    seen_psn: set[str] = set()
    for occurrence in ordered_psn_occurrences(results):
        if occurrence.duplicate_key in seen_psn:
            continue
        seen_psn.add(occurrence.duplicate_key)
        psn_lines.append(occurrence.display)
    return pubg_cards, psn_lines


def has_card_results(results: list[OcrResult]) -> bool:
    pubg_cards, psn_lines = result_card_lines(results)
    return bool(pubg_cards or psn_lines)


def extract_card_lines_from_text(text: str) -> tuple[list[str], list[str]]:
    pubg_cards = exact_unique(extract_cards(text))
    psn_lines = unique_psn_lines(extract_psn_ordered(text, force=True))
    return pubg_cards, [line for line in psn_lines if not line.endswith(FUZZY_SUFFIX)]


def ocr_text_keys(text: str) -> list[str]:
    normalized = normalize_text(text)
    compact = re.sub(r"[^A-Z0-9$]", "", normalized)
    keys: list[str] = []
    for match in re.finditer(r"[SP5$][A-Z0-9$]{10,25}", compact):
        keys.append(match.group(0))
    if len(compact) >= 10:
        keys.append(compact)
    return exact_unique_text(keys)


def card_type_for(card: str) -> str | None:
    if valid_card(card):
        return "PUBG"
    if valid_psn_card(card):
        return "PSN"
    return None


def apply_card_corrections(chat_id: int, result: OcrResult) -> OcrResult:
    corrected_cards: list[str] = []
    learned_pubg_cards: list[str] = []
    correction_applied = False
    learned_pubg_targets = exact_unique_text(
        [correction.correct_card for correction in ledger_store.list_ocr_text_corrections(chat_id) if correction.card_type == "PUBG"]
        + [correction.correct_card for correction in ledger_store.list_card_corrections(chat_id) if correction.card_type == "PUBG"]
    )
    for card in result.cards:
        corrected = ledger_store.get_card_correction(chat_id, "PUBG", card)
        if corrected and corrected != card:
            correction_applied = True
        if not corrected:
            learned_match = next((target for target in learned_pubg_targets if likely_learned_variant(card, target)), None)
            if learned_match and learned_match != card:
                corrected = learned_match
                correction_applied = True
                learned_pubg_cards.append(learned_match)
        corrected_cards.append(corrected or card)

    def correct_psn(line: str) -> str:
        nonlocal correction_applied
        key = psn_key(line)
        if not key:
            return line
        corrected = ledger_store.get_card_correction(chat_id, "PSN", key)
        if corrected and corrected != key:
            correction_applied = True
        return corrected or line

    corrected_psn_cards: list[str] = []
    for card in result.psn_cards:
        corrected = ledger_store.get_card_correction(chat_id, "PSN", card)
        if corrected and corrected != card:
            correction_applied = True
        corrected_psn_cards.append(corrected or card)
    corrected_psn_ordered = [correct_psn(line) for line in result.psn_ordered]
    corrected_psn_uncertain = [correct_psn(line) for line in result.psn_uncertain]
    raw_keys = set(ocr_text_keys(result.raw_text))
    for correction in ledger_store.list_ocr_text_corrections(chat_id):
        if correction.wrong_text not in raw_keys:
            continue
        correction_applied = True
        if correction.card_type == "PUBG":
            corrected_cards.append(correction.correct_card)
            learned_pubg_cards.append(correction.correct_card)
        elif correction.card_type == "PSN":
            corrected_psn_ordered.append(correction.correct_card)
    if learned_pubg_cards:
        corrected_cards = [
            card
            for card in corrected_cards
            if card in learned_pubg_cards
        ]
    return replace(
        result,
        cards=tuple(exact_unique(corrected_cards)),
        psn_cards=tuple(exact_unique_psn(corrected_psn_cards)),
        psn_ordered=tuple(unique_psn_lines(corrected_psn_ordered)),
        psn_uncertain=tuple(exact_unique_text(corrected_psn_uncertain)),
        uncertain_count=0 if correction_applied else result.uncertain_count,
        corrections_applied=result.corrections_applied,
    )


def learn_card_corrections_from_reply(update: Update) -> str | None:
    if not update.message or not update.effective_chat or not is_owner_update(update):
        return None
    reply_message = update.message.reply_to_message
    if not reply_message:
        return None
    correction_text = update.message.text or ""
    replied_text = reply_message.text or reply_message.caption or ""
    if not correction_text or not replied_text:
        return None
    wrong_pubg, wrong_psn = extract_card_lines_from_text(replied_text)
    correct_pubg, correct_psn = extract_card_lines_from_text(correction_text)
    source_user = user_label(update)
    learned_lines: list[str] = []

    def learn_pairs(card_type: str, wrong_cards: list[str], correct_cards: list[str]) -> None:
        if not wrong_cards or not correct_cards or len(wrong_cards) != len(correct_cards):
            return
        for wrong_card, correct_card in zip(wrong_cards, correct_cards):
            if wrong_card == correct_card:
                continue
            ledger_store.set_card_correction(
                update.effective_chat.id,
                card_type,
                wrong_card,
                correct_card,
                source_user,
            )
            learned_lines.append(f"{card_type} {wrong_card} -> {correct_card}")

    learn_pairs("PUBG", wrong_pubg, correct_pubg)
    learn_pairs("PSN", wrong_psn, correct_psn)
    if not learned_lines:
        return None
    return "已学习纠错\n" + "\n".join(learned_lines)


def card_text_result(text: str) -> OcrResult | None:
    pubg_cards, psn_lines = extract_card_lines_from_text(text)
    if not pubg_cards and not psn_lines:
        return None
    return OcrResult(cards=tuple(pubg_cards), psn_ordered=tuple(psn_lines))


def unlearnable_correction_feedback(update: Update) -> str | None:
    if not update.message:
        return None
    reply_message = update.message.reply_to_message
    if not reply_message:
        return None
    correction_result = card_text_result(update.message.text or "")
    if correction_result is None:
        return None
    replied_text = reply_message.text or reply_message.caption or ""
    wrong_pubg, wrong_psn = extract_card_lines_from_text(replied_text)
    if wrong_pubg or wrong_psn:
        return None
    return (
        "已收到正确卡密，但原回复里没有错误卡密，不能建立纠错映射。\n"
        "以后如果机器人把卡密识别错了，请回复那条包含错误卡密的结果，再发送正确卡密。"
    )


async def learn_ocr_sample_from_replied_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str | None:
    if not update.message or not update.effective_chat or not is_owner_update(update):
        return None
    reply_message = update.message.reply_to_message
    if not reply_message or not getattr(reply_message, "photo", None):
        return None
    correct_result = card_text_result(update.message.text or "")
    if correct_result is None:
        return None
    correct_pubg, correct_psn = result_card_lines([correct_result])
    correct_cards = [("PUBG", card) for card in correct_pubg] + [("PSN", card) for card in correct_psn]
    if not correct_cards:
        return None

    image_path = await download_message_photo(reply_message, context)
    try:
        result = await asyncio.to_thread(run_ocr, image_path)
    finally:
        try:
            image_path.unlink(missing_ok=True)
            image_path.parent.rmdir()
        except OSError:
            pass

    keys = ocr_text_keys(result.raw_text)
    if not keys:
        return "没有从这张图读到可学习的文字特征，请换更清晰的原图再回复正确卡密。"

    source_user = user_label(update)
    learned_lines: list[str] = []
    for card_type, correct_card in correct_cards:
        for key in keys[:8]:
            ledger_store.set_ocr_text_correction(
                update.effective_chat.id,
                card_type,
                key,
                correct_card,
                source_user,
            )
        learned_lines.append(f"{card_type} {correct_card}")
    return "已学习这张图片的OCR特征\n" + "\n".join(learned_lines)


def card_history_day_key(chat_id: int, now: datetime | None = None) -> str:
    reset_hour = ledger_store.get_ledger_reset_hour(chat_id)
    local_now = (now or datetime.now(LEDGER_TZ)).astimezone(LEDGER_TZ)
    day = local_now.date()
    if local_now.hour < reset_hour:
        day -= timedelta(days=1)
    return day.isoformat()


def format_history_time(created_at: str) -> str:
    parsed = datetime.fromisoformat(created_at)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(LEDGER_TZ).strftime("%H:%M:%S")


def register_card_history(updates: list[Update], results: list[OcrResult]) -> list[CardHistoryDuplicate]:
    if not updates:
        return []
    chat = updates[-1].effective_chat
    if not chat:
        return []
    chat_id = chat.id
    day_key = card_history_day_key(chat_id)
    ledger_store.clear_recognized_cards_before(day_key)
    duplicates: list[CardHistoryDuplicate] = []
    seen_reported: set[tuple[str, str]] = set()
    for update, result in zip(updates, results):
        source_user = user_label(update)
        source_message_id = update.message.message_id if update.message else None
        pubg_cards, psn_lines = result_card_lines([result])
        for card_type, cards in (("PUBG", pubg_cards), ("PSN", [card for card in psn_lines if not card.endswith(FUZZY_SUFFIX)])):
            for card in cards:
                record = ledger_store.record_recognized_card(
                    chat_id=chat_id,
                    card_type=card_type,
                    card=card,
                    day_key=day_key,
                    source_user=source_user,
                    source_message_id=source_message_id,
                )
                key = (card_type, card)
                if record is None or key in seen_reported:
                    continue
                seen_reported.add(key)
                duplicates.append(
                    CardHistoryDuplicate(
                        card_type=card_type,
                        card=card,
                        first_seen_at=record.created_at,
                        first_source_user=record.source_user,
                    )
                )
    return duplicates


def append_history_duplicates(reply: str, duplicates: list[CardHistoryDuplicate]) -> str:
    if not duplicates:
        return reply
    lines = ["<b>今日重复出现卡密</b>"]
    for duplicate in duplicates:
        source_user = html.escape(source_username_only(duplicate.first_source_user))
        lines.extend(
            [
                f"{duplicate.card_type}：{format_underlined_card_code(duplicate.card)}",
                "已出现过",
                f"首次 {format_history_time(duplicate.first_seen_at)} 来自 | {source_user} |",
            ]
        )
    return reply + "\n\n" + "\n".join(lines)


def format_cards_only(results: list[OcrResult]) -> str:
    pubg_cards, psn_lines = result_card_lines(results)
    sections: list[str] = []
    if pubg_cards:
        sections.append(f"<b>【{PUBG_LABEL}】</b>\n\n{format_card_codes(pubg_cards)}")
    if psn_lines:
        sections.append(f"<b>【{PSN_LABEL}】</b>\n\n{format_card_codes(psn_lines)}")
    if not sections:
        sections.append("\u672a\u8bc6\u522b\u5230\u5361\u5bc6")
    return "\n\n".join(sections)


def split_html_message(text: str, limit: int = TELEGRAM_SAFE_TEXT_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    in_pre = False

    def emit() -> None:
        nonlocal current, current_len
        if not current:
            return
        chunk = "\n".join(current)
        if in_pre and not chunk.endswith("</pre>"):
            chunk += "\n</pre>"
        chunks.append(chunk)
        current = ["<pre>"] if in_pre else []
        current_len = len("<pre>") if in_pre else 0

    for line in text.splitlines():
        add_len = len(line) + (1 if current else 0)
        if current and current_len + add_len > limit:
            emit()
        while len(line) > limit:
            if current:
                emit()
            chunks.append(line[:limit])
            line = line[limit:]
        if not line and not current:
            continue
        current.append(line)
        current_len += add_len
        if "<pre>" in line and "</pre>" not in line:
            in_pre = True
        if "</pre>" in line:
            in_pre = False

    emit()
    return chunks or [text[:limit]]


async def reply_html_chunks(message, text: str, **kwargs) -> None:
    chunks = split_html_message(text)
    for index, chunk in enumerate(chunks):
        await message.reply_text(
            chunk,
            parse_mode=ParseMode.HTML,
            **(kwargs if index == 0 else {}),
        )


async def send_html_chunks(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str) -> None:
    for chunk in split_html_message(text):
        await context.bot.send_message(
            chat_id=chat_id,
            text=chunk,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


CALC_RE = re.compile(r"^[\d\s+\-*/().xX×÷]+$")
CALC_HAS_OPERATOR_RE = re.compile(r"[\d)]\s*[+\-*/xX×÷]\s*[\d(]")


def calculate_expression(text: str) -> str | None:
    expression = normalize_calc_expression(text)
    if not expression or not CALC_RE.fullmatch(expression) or not CALC_HAS_OPERATOR_RE.search(expression):
        return None
    try:
        tree = ast.parse(expression, mode="eval")
        value = _eval_calc_node(tree.body)
    except (SyntaxError, ValueError, InvalidOperation, DivisionByZero, OverflowError):
        return None
    display_expression = re.sub(r"\s+", "", expression)
    return f"{display_expression}={_format_calc_result(value)}"


def normalize_calc_expression(text: str) -> str:
    return (
        text.strip()
        .translate(FULLWIDTH_MAP)
        .replace("＋", "+")
        .replace("－", "-")
        .replace("＊", "*")
        .replace("／", "/")
        .replace("（", "(")
        .replace("）", ")")
        .replace("×", "*")
        .replace("x", "*")
        .replace("X", "*")
        .replace("÷", "/")
    )


def _eval_calc_node(node: ast.AST) -> Decimal:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return Decimal(str(node.value))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        value = _eval_calc_node(node.operand)
        return value if isinstance(node.op, ast.UAdd) else -value
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
        left = _eval_calc_node(node.left)
        right = _eval_calc_node(node.right)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if right == 0:
            raise DivisionByZero
        return left / right
    raise ValueError("unsupported expression")


def _format_calc_result(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


TRC20_ADDRESS_RE = re.compile(r"(?<![A-Za-z0-9])T[1-9A-HJ-NP-Za-km-z]{33}(?![A-Za-z0-9])")


def extract_trc20_address(text: str) -> str | None:
    match = TRC20_ADDRESS_RE.search(text.strip())
    return match.group(0) if match else None


def _font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    candidates = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        r"C:\Windows\Fonts\msyhbd.ttc" if bold else r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            try:
                return ImageFont.truetype(candidate, size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def _center_text(draw: ImageDraw.ImageDraw, box: tuple[int, int, int, int], text: str, font: ImageFont.ImageFont, fill: str) -> None:
    bbox = draw.textbbox((0, 0), text, font=font)
    width = bbox[2] - bbox[0]
    height = bbox[3] - bbox[1]
    x = box[0] + (box[2] - box[0] - width) / 2
    y = box[1] + (box[3] - box[1] - height) / 2 - 2
    draw.text((x, y), text, font=font, fill=fill)


def make_trc20_verify_image(address: str, created_at: datetime | None = None) -> BytesIO:
    created_at = created_at or datetime.now(LEDGER_TZ)
    timestamp = created_at.astimezone(LEDGER_TZ).strftime("%Y-%m-%d %H:%M:%S")
    width, height = 860, 300
    image = Image.new("RGB", (width, height), "#0aa77c")
    draw = ImageDraw.Draw(image)

    title_font = _font(40, bold=True)
    subtitle_font = _font(20)
    address_font = _font(34, bold=True)
    time_font = _font(20)

    _center_text(draw, (0, 34, width, 80), "USDT防篡改验证核对", title_font, "#fff238")
    _center_text(draw, (0, 78, width, 112), "（请双方谨慎核对地址是否与图中一致，如有误停止付款）", subtitle_font, "#003b30")

    bar = (34, 128, width - 34, 200)
    draw.rounded_rectangle(bar, radius=4, fill="#e87700")
    _center_text(draw, bar, address, address_font, "#ffffff")

    draw.rounded_rectangle((34, 222, width - 34, 268), radius=4, fill="#08936e")
    _center_text(draw, (34, 222, width - 34, 268), f"生成时间：{timestamp}", time_font, "#ffffff")

    output = BytesIO()
    image.save(output, format="PNG")
    output.seek(0)
    output.name = "trc20-verify.png"
    return output


async def reply_trc20_verify_image(message, address: str) -> None:
    image = make_trc20_verify_image(address)
    await message.reply_photo(photo=image, caption=address)


def parse_okx_c2c_usdt_cny_prices(payload: dict, limit: int = 5) -> list[Decimal]:
    sell_orders = payload.get("data", {}).get("sell", [])
    if not sell_orders:
        raise ValueError("empty OKX C2C sell order book")
    prices: list[Decimal] = []
    for order in sell_orders:
        price = order.get("price")
        if price is None:
            continue
        prices.append(Decimal(str(price)))
        if len(prices) >= limit:
            break
    if not prices:
        raise ValueError("missing OKX C2C prices")
    return prices


def parse_okx_exchange_rate_price(payload: dict) -> Decimal:
    data = payload.get("data", [])
    if not data:
        raise ValueError("empty OKX exchange-rate data")
    price = data[0].get("usdCny")
    if price is None:
        raise ValueError("missing OKX usdCny")
    return Decimal(str(price))


def format_okx_prices(prices: list[Decimal], source: str) -> str:
    lines = ["欧意USDT/CNY 最新5档"]
    lines.extend(f"{index}. {_format_calc_result(price)}" for index, price in enumerate(prices, start=1))
    lines.append(f"来源：{source}")
    return "\n".join(lines)


def is_price_command(text: str) -> bool:
    normalized = text.strip()
    lowered = normalized.lower()
    return normalized == "币价" or lowered in {"bj", "z0"} or normalized == "/price"


def is_realtime_rate_command(text: str) -> bool:
    return text.strip() == "设置实时汇率"


async def fetch_okx_usdt_cny_prices() -> tuple[list[Decimal], str]:
    async with httpx.AsyncClient(timeout=15, headers=OKX_HTTP_HEADERS) as client:
        try:
            response = await client.get(OKX_C2C_USDT_CNY_URL)
            response.raise_for_status()
            return parse_okx_c2c_usdt_cny_prices(response.json(), limit=5), "OKX C2C卖单"
        except Exception:
            logger.exception("OKX C2C USDT/CNY price fetch failed, falling back to exchange-rate")
        response = await client.get(OKX_EXCHANGE_RATE_URL)
        response.raise_for_status()
        return [parse_okx_exchange_rate_price(response.json())], "OKX官方USD/CNY汇率"


async def reply_okx_price(message) -> None:
    try:
        prices, source = await fetch_okx_usdt_cny_prices()
    except Exception:
        logger.exception("OKX USDT/CNY price fetch failed")
        await message.reply_text("币价获取失败，请稍后再试。")
        return
    await message.reply_text(format_okx_prices(prices, source))


async def set_realtime_ledger_rate(update: Update) -> bool:
    if not update.message or not update.effective_chat:
        return False
    if not is_realtime_rate_command(update.message.text or ""):
        return False
    if update.effective_chat.id >= 0:
        await update.message.reply_text("请在群内设置实时汇率。")
        return True
    user_id = update.effective_user.id if update.effective_user else 0
    owner_ids = ledger_owner_ids(update.effective_chat.id)
    if not ledger_store.is_operator(update.effective_chat.id, user_id, owner_ids):
        await update.message.reply_text("无权限设置实时汇率。")
        return True
    old_rate, _fee = ledger_store.get_settings(update.effective_chat.id)
    try:
        prices, source = await fetch_okx_usdt_cny_prices()
    except Exception:
        logger.exception("OKX realtime ledger rate fetch failed")
        await update.message.reply_text(f"❌ 获取欧意实时汇率失败，已保留当前群原汇率：{_format_calc_result(old_rate)}")
        return True
    if not prices:
        await update.message.reply_text(f"❌ 获取欧意实时汇率失败，已保留当前群原汇率：{_format_calc_result(old_rate)}")
        return True
    new_rate = ledger_store.set_rate(update.effective_chat.id, prices[0])
    updated_at = datetime.now(LEDGER_TZ).strftime("%Y-%m-%d %H:%M:%S")
    await update.message.reply_text(
        "\n".join(
            [
                "✅ 当前群实时汇率已更新",
                "",
                f"汇率：{_format_calc_result(new_rate)}",
                f"来源：欧意 USDT/CNY 最新 1 档（{source}）",
                f"更新时间：{updated_at}",
            ]
        )
    )
    return True


def start_help_text() -> str:
    return ledger_commands.HELP_TEXT


def add_group_keyboard(bot_username: str) -> InlineKeyboardMarkup:
    add_group_url = f"https://t.me/{bot_username}?startgroup=true" if bot_username else "https://t.me/"
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("➕ 拉机器人进群", url=add_group_url)]]
    )


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([[TEXT_LEDGER_ADD_GROUP]], resize_keyboard=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(
            start_help_text(),
            reply_markup=main_menu_keyboard(),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )


async def handle_add_group_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    bot_user = await context.bot.get_me()
    await update.message.reply_text(
        "🤖 点击下方按钮拉机器人进群。",
        reply_markup=add_group_keyboard(bot_user.username or ""),
    )


async def handle_ledger_add_group_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    remember_ledger_user(update)
    ensure_private_ledger_owner(update)
    bot_user = await context.bot.get_me()
    await update.message.reply_text(
        "🤖 点击下方按钮拉机器人进群，群里可直接记账。",
        reply_markup=add_group_keyboard(bot_user.username or ""),
    )


async def handle_ledger_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    remember_ledger_user(update)
    ensure_private_ledger_owner(update)
    await reply_ledger(update.message, ledger_commands.HELP_TEXT)


async def show_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(str(update.message.chat_id))


async def show_version(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(BOT_VERSION)


def ocr_debug_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> str:
    if context.args:
        return " ".join(context.args)
    if update.message and update.message.reply_to_message:
        return update.message.reply_to_message.text or update.message.reply_to_message.caption or ""
    return ""


async def ocr_debug_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not is_owner_update(update):
        return
    raw_text = ocr_debug_input(update, context)
    if not raw_text:
        await update.message.reply_text("Usage: /ocr_debug raw_ocr_text")
        return
    await update.message.reply_text(format_ocr_debug(raw_text, card_type="PUBG"))


async def ocr_candidates_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not is_owner_update(update):
        return
    raw_text = ocr_debug_input(update, context)
    if not raw_text:
        await update.message.reply_text("Usage: /ocr_candidates raw_ocr_text")
        return
    await update.message.reply_text(format_ocr_candidates_debug(raw_text, card_type="PUBG"))


async def ocr_font_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not is_owner_update(update):
        return
    await update.message.reply_text(format_ocr_font_stats_plus(font_repository))


async def ocr_review_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not is_owner_update(update):
        return
    await update.message.reply_text(format_ocr_review(OCR_CANDIDATES_PATH))


async def ocr_export_fonts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not is_owner_update(update):
        return
    path = export_font_templates(Path("outputs") / "font_templates.json")
    if not path.exists():
        await update.message.reply_text("font_templates.json not found")
        return
    with path.open("rb") as template_file:
        await update.message.reply_document(document=template_file, filename="font_templates.json")


async def ocr_import_fonts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not is_owner_update(update):
        return
    payload = command_body(update, "ocr_import_fonts")
    if not payload and update.message.reply_to_message and update.message.reply_to_message.document:
        document = update.message.reply_to_message.document
        tg_file = await context.bot.get_file(document.file_id)
        temp_dir = Path(tempfile.mkdtemp(prefix="ocr_font_import_"))
        temp_path = temp_dir / (document.file_name or "font_templates.json")
        try:
            await tg_file.download_to_drive(custom_path=temp_path)
            payload = temp_path.read_text(encoding="utf-8")
        finally:
            try:
                temp_path.unlink(missing_ok=True)
                temp_dir.rmdir()
            except OSError:
                pass
    if not payload:
        await update.message.reply_text("Usage: /ocr_import_fonts JSON 或回复 font_templates.json 文件")
        return
    try:
        count = import_font_templates(payload, Path("outputs") / "font_templates.json")
    except Exception as exc:
        await update.message.reply_text(f"OCR font import failed: {exc}")
        return
    await update.message.reply_text(f"OCR font templates imported: {count}")


async def ocr_version_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not is_owner_update(update):
        return
    await update.message.reply_text(format_ocr_version(Path("."), current_version=BOT_VERSION))


async def ocr_cache_today_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not is_owner_update(update):
        return
    summary = today_ocr_cache_summary(TODAY_OCR_CACHE_PATH)
    lines = [
        "OCR Cache Today",
        f"Date: {summary.date}",
        f"Images: {summary.images}",
        f"OCR cards: {summary.ocr_count}",
        f"Path: {summary.path}",
        "First 10:",
        *(summary.first_cards or ("-",)),
    ]
    await update.message.reply_text("\n".join(lines))


async def ocr_health_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not is_owner_update(update):
        return
    summary = today_ocr_cache_summary(TODAY_OCR_CACHE_PATH)
    lines = [
        "OCR Health",
        f"Provider: {OCR_PROVIDER}",
        f"OCRSpace keys: {len(OCR_SPACE_API_KEYS)}",
        f"Local fallback: {LOCAL_FALLBACK}",
        f"Today cache: {'ready' if summary.exists else 'missing'}",
        f"Today images: {summary.images}",
        f"Today OCR cards: {summary.ocr_count}",
    ]
    await update.message.reply_text("\n".join(lines))


async def remote_ocr_status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not is_owner_update(update):
        return
    available, reason = await asyncio.to_thread(remote_ocr_available)
    remote_calls = int(remote_ocr_status["today_remote_calls"])
    lines = [
        "Remote OCR Status",
        f"remote_enabled: {REMOTE_OCR_ENABLED}",
        f"remote_url: {REMOTE_OCR_URL or '-'}",
        f"remote_health: {available}",
        f"health_reason: {reason}",
        f"last_success_at: {remote_ocr_status['last_success_at'] or '-'}",
        f"last_failed_at: {remote_ocr_status['last_failed_at'] or '-'}",
        f"last_error: {remote_ocr_status['last_error'] or '-'}",
        f"today_remote_calls: {remote_ocr_status['today_remote_calls']}",
        f"today_remote_success: {remote_ocr_status['today_remote_success']}",
        f"today_remote_failed: {remote_ocr_status['today_remote_failed']}",
        f"today_fallback_count: {remote_ocr_status['today_fallback_count']}",
        f"avg_remote_latency_ms: {avg_remote_latency_ms()}",
        f"enhanced_rate: {percent_rate(int(remote_ocr_status['today_enhanced_used']), remote_calls)}",
        f"cache_hit_rate: {percent_rate(int(remote_ocr_status['today_cache_hits']), remote_calls)}",
        f"current_provider: {current_ocr_provider()}",
    ]
    await update.message.reply_text("\n".join(lines))


async def status_panel_allowed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if is_owner_update(update):
        return True
    if not update.effective_chat or not update.effective_user or not context:
        return False
    try:
        member = await asyncio.wait_for(
            context.bot.get_chat_member(update.effective_chat.id, update.effective_user.id),
            timeout=1.5,
        )
    except Exception:
        return False
    return getattr(member, "status", "") in {"administrator", "creator"}


async def status_panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not await status_panel_allowed(update, context):
        await update.message.reply_text("无权限。")
        return
    try:
        text = await asyncio.to_thread(build_status_panel)
    except Exception as exc:
        logger.exception("Failed to build status panel")
        text = f"状态查询失败：{exc.__class__.__name__}"
    await update.message.reply_text(text)


def command_body(update: Update, command: str) -> str:
    if not update.message or not update.message.text:
        return ""
    text = update.message.text
    first_line, _, rest = text.partition("\n")
    if first_line.strip().split(maxsplit=1)[0].split("@", 1)[0] != f"/{command}":
        return text
    inline = first_line.strip().split(maxsplit=1)
    values = []
    if len(inline) > 1:
        values.append(inline[1])
    if rest:
        values.append(rest)
    return "\n".join(values).strip()


def learn_cards_body(update: Update) -> str:
    text = command_body(update, "learn_cards")
    stripped = text.strip()
    chinese_command = stripped[1:] if stripped.startswith("/") else stripped
    if chinese_command == "学习卡密":
        return ""
    if chinese_command.startswith("学习卡密"):
        return chinese_command[len("学习卡密") :].lstrip(" \t\r\n")
    return text


async def learn_cards_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not is_owner_update(update):
        return
    text = learn_cards_body(update)
    if not text:
        await update.message.reply_text("请在“学习卡密”后粘贴人工确认的卡密列表。")
        return
    preview = build_learning_preview(text)
    if not preview.ocr_cache_found:
        await update.message.reply_text(preview.message)
        return
    if preview.card_count < 5:
        await update.message.reply_text(f"仅检测到 {preview.card_count} 条合法卡密，至少需要 5 条才进入批量学习确认。")
        return
    pending_learning_texts[update.effective_user.id] = text
    await update.message.reply_text(preview.message)


async def auto_learn_cards_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    return


async def learn_confirm_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not is_owner_update(update) or not update.effective_user:
        return
    text = pending_learning_texts.pop(update.effective_user.id, "")
    if not text:
        await update.message.reply_text("没有待确认的OCR学习任务。")
        return
    await update.message.reply_text(execute_learning(text))


async def learn_cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not is_owner_update(update) or not update.effective_user:
        return
    pending_learning_texts.pop(update.effective_user.id, None)
    await update.message.reply_text("已取消今日OCR学习。")


async def ocr_learning_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not is_owner_update(update):
        return
    await update.message.reply_text(format_learning_stats())


def ledger_owner_ids(chat_id: int | None = None) -> set[int]:
    if chat_id is not None:
        chat_owner_id = ledger_store.get_chat_owner_id(chat_id)
        if chat_owner_id is not None:
            return {chat_owner_id}
    owner_id = parse_chat_id(OWNER_CHAT_ID)
    return {owner_id} if owner_id is not None else set()


def ledger_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("昨日账单", callback_data="ledger:yesterday"),
                InlineKeyboardButton("今日账单", callback_data="ledger:today"),
            ],
            [
                InlineKeyboardButton("完整账单", callback_data="ledger:full"),
                InlineKeyboardButton("使用说明", callback_data="ledger:help"),
            ],
        ]
    )


def ledger_actor(update: Update) -> LedgerActor:
    user = update.effective_user
    if not user:
        return LedgerActor(user_id=0, username="", display_name="")
    display_name = " ".join(part for part in [user.first_name, user.last_name] if part)
    return LedgerActor(user_id=user.id, username=user.username or "", display_name=display_name)


def remember_ledger_user(update: Update) -> None:
    if not update.effective_chat or not update.effective_user:
        return
    actor = ledger_actor(update)
    ledger_store.remember_user(
        update.effective_chat.id,
        actor.user_id,
        actor.username,
        actor.display_name,
        bool(getattr(update.effective_user, "is_bot", False)),
    )


def ensure_private_ledger_owner(update: Update) -> None:
    if not update.effective_chat or not update.effective_user:
        return
    if getattr(update.effective_chat, "type", "") == "private":
        ledger_store.set_chat_owner(update.effective_chat.id, update.effective_user.id)


def remember_bot_chat(update: Update) -> None:
    chat = update.effective_chat
    if not chat:
        return
    chat_type = getattr(chat, "type", "")
    if chat_type not in {"group", "supergroup"}:
        return
    title = getattr(chat, "title", "") or getattr(chat, "full_name", "") or str(chat.id)
    ledger_store.remember_bot_chat(chat.id, title, chat_type)


def owner_user_id() -> int | None:
    return parse_chat_id(OWNER_CHAT_ID)


def is_owner_update(update: Update | None) -> bool:
    owner_id = owner_user_id()
    return bool(owner_id is not None and update and update.effective_user and update.effective_user.id == owner_id)


def ledger_actor_from_message(message) -> LedgerActor | None:
    user = getattr(message, "from_user", None)
    if not user:
        return None
    display_name = " ".join(part for part in [user.first_name, user.last_name] if part)
    return LedgerActor(user_id=user.id, username=user.username or "", display_name=display_name)


async def reply_ledger(message, text: str) -> None:
    await message.reply_text(
        text,
        reply_markup=ledger_keyboard(),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def handle_ledger_text(update: Update, context: ContextTypes.DEFAULT_TYPE, allow_trc20: bool = True) -> bool:
    if not update.message or not update.effective_chat:
        return False
    remember_bot_chat(update)
    remember_ledger_user(update)
    ensure_private_ledger_owner(update)
    normalized_text = (update.message.text or "").strip()
    if normalized_text in {"开启识别", "打开识别", "启用识别"}:
        if update.effective_user and update.effective_user.id not in ledger_owner_ids(update.effective_chat.id):
            await update.message.reply_text("只有拉机器人进群的人可以开启识别。")
            return True
        ledger_store.set_recognition_enabled(update.effective_chat.id, True)
        await update.message.reply_text("卡密识别已开启。")
        return True
    if normalized_text in {"关闭识别", "停止识别", "停用识别", "暂停识别"}:
        if update.effective_user and update.effective_user.id not in ledger_owner_ids(update.effective_chat.id):
            await update.message.reply_text("只有拉机器人进群的人可以关闭识别。")
            return True
        ledger_store.set_recognition_enabled(update.effective_chat.id, False)
        await update.message.reply_text("卡密识别已关闭，后续图片不会识别卡密。发送“开启识别”可重新开启。")
        return True
    recognition_enabled = ledger_store.is_recognition_enabled(update.effective_chat.id)
    if recognition_enabled:
        learned = learn_card_corrections_from_reply(update)
        if learned:
            await update.message.reply_text(learned)
            return True
        learned_sample = await learn_ocr_sample_from_replied_photo(update, context)
        if learned_sample:
            await update.message.reply_text(learned_sample)
            return True
        correction_feedback = unlearnable_correction_feedback(update)
        if correction_feedback:
            await update.message.reply_text(correction_feedback)
            return True
    trc20_address = extract_trc20_address(update.message.text or "") if allow_trc20 else None
    if trc20_address:
        await reply_trc20_verify_image(update.message, trc20_address)
        return True
    if await set_realtime_ledger_rate(update):
        return True
    if is_price_command(update.message.text or ""):
        await reply_okx_price(update.message)
        return True
    calculation = calculate_expression(update.message.text or "")
    if calculation is not None:
        await update.message.reply_text(calculation)
        return True
    reply_message = update.message.reply_to_message
    reply_user = ledger_actor_from_message(reply_message) if reply_message else None
    reply_text = None
    reply_message_id = None
    if reply_message:
        reply_text = reply_message.text or reply_message.caption
        reply_message_id = reply_message.message_id

    result = handle_ledger_command_text(
        store=ledger_store,
        chat_id=update.effective_chat.id,
        actor=ledger_actor(update),
        text=update.message.text or "",
        owner_ids=ledger_owner_ids(update.effective_chat.id),
        reply_user=reply_user,
        reply_text=reply_text,
        message_id=update.message.message_id,
        reply_message_id=reply_message_id,
    )
    if result:
        await reply_ledger(update.message, result.text)
        return True
    return False


async def handle_priority_ledger_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await handle_broadcast_text(update, context):
        raise ApplicationHandlerStop
    handled = await handle_ledger_text(update, context, allow_trc20=False)
    if handled:
        raise ApplicationHandlerStop


async def handle_ledger_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.message:
        return
    text = LEDGER_CALLBACK_TEXT.get(query.data or "")
    await query.answer()
    if not text:
        return
    result = handle_ledger_command_text(
        store=ledger_store,
        chat_id=query.message.chat_id,
        actor=LedgerActor(
            user_id=query.from_user.id,
            username=query.from_user.username or "",
            display_name=" ".join(part for part in [query.from_user.first_name, query.from_user.last_name] if part),
        ),
        text=text,
        owner_ids=ledger_owner_ids(query.message.chat_id),
    )
    if result:
        await reply_ledger(query.message, result.text)


def broadcast_group_keyboard(selected: set[int] | None = None) -> InlineKeyboardMarkup:
    selected = selected or set()
    rows: list[list[InlineKeyboardButton]] = []
    for row in ledger_store.list_active_bot_groups():
        chat_id = int(row["chat_id"])
        title = row["title"] or str(chat_id)
        prefix = "✅ " if chat_id in selected else "⬜ "
        rows.append([InlineKeyboardButton(f"{prefix}{title}", callback_data=f"broadcast:toggle:{chat_id}")])
    rows.append(
        [
            InlineKeyboardButton("下一步", callback_data="broadcast:next"),
            InlineKeyboardButton("取消", callback_data="broadcast:cancel"),
        ]
    )
    return InlineKeyboardMarkup(rows)


async def start_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not is_owner_update(update):
        await update.message.reply_text("只有老板可以使用广播。")
        return
    groups = ledger_store.list_active_bot_groups()
    if not groups:
        await update.message.reply_text("还没有记录到机器人所在群。先把机器人拉进群，或在群里发一条消息后再试。")
        return
    context.user_data["broadcast_selected"] = set()
    context.user_data["broadcast_waiting_text"] = False
    await update.message.reply_text("请选择要广播的群：", reply_markup=broadcast_group_keyboard(set()))


async def handle_broadcast_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    if not is_owner_update(update):
        await query.edit_message_text("只有老板可以使用广播。")
        return
    data = query.data or ""
    selected = context.user_data.get("broadcast_selected")
    if not isinstance(selected, set):
        selected = set()
    if data == "broadcast:cancel":
        context.user_data.pop("broadcast_selected", None)
        context.user_data.pop("broadcast_waiting_text", None)
        await query.edit_message_text("已取消广播。")
        return
    if data == "broadcast:next":
        if not selected:
            await query.edit_message_text("请至少选择一个群。", reply_markup=broadcast_group_keyboard(selected))
            return
        context.user_data["broadcast_selected"] = selected
        context.user_data["broadcast_waiting_text"] = True
        await query.edit_message_text("请发送要广播的文字。发送“取消广播”可退出。")
        return
    match = re.fullmatch(r"broadcast:toggle:(-?\d+)", data)
    if match:
        chat_id = int(match.group(1))
        if chat_id in selected:
            selected.remove(chat_id)
        else:
            selected.add(chat_id)
        context.user_data["broadcast_selected"] = selected
        await query.edit_message_reply_markup(reply_markup=broadcast_group_keyboard(selected))


async def handle_broadcast_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not update.message or not is_owner_update(update):
        return False
    if not context.user_data.get("broadcast_waiting_text"):
        return False
    text = update.message.text or ""
    if text.strip() == "取消广播":
        context.user_data.pop("broadcast_selected", None)
        context.user_data.pop("broadcast_waiting_text", None)
        await update.message.reply_text("已取消广播。")
        return True
    selected = context.user_data.get("broadcast_selected")
    if not isinstance(selected, set) or not selected:
        context.user_data.pop("broadcast_waiting_text", None)
        await update.message.reply_text("没有选择群，请重新发送“广播”。")
        return True
    success = 0
    failed = 0
    for chat_id in sorted(selected):
        try:
            await context.bot.send_message(chat_id=chat_id, text=text)
            success += 1
        except Exception:
            logger.exception("Broadcast to chat %s failed", chat_id)
            failed += 1
    context.user_data.pop("broadcast_selected", None)
    context.user_data.pop("broadcast_waiting_text", None)
    await update.message.reply_text(f"广播完成：成功 {success} 个群，失败 {failed} 个群。")
    return True


def broadcast_all_targets() -> list[int]:
    targets: list[int] = []
    seen: set[int] = set()
    for row in ledger_store.list_known_users_for_broadcast():
        user_id = int(row["user_id"])
        if user_id in seen:
            continue
        seen.add(user_id)
        targets.append(user_id)
    return targets


def extract_broadcast_all_text(text: str, command: str) -> str:
    stripped = text.strip()
    if stripped == command:
        return ""
    if stripped.startswith(command):
        return stripped[len(command) :].lstrip(" \t\r\n")
    return stripped


def format_broadcast_preview(text: str, target_count: int) -> str:
    return (
        "广播预览\n\n"
        f"目标用户：{target_count}\n"
        "内容：\n"
        f"{text}\n\n"
        "发送“通知所有人”将发送当前预览内容。\n"
        "发送 /broadcast_cancel 可取消。"
    )


async def broadcast_preview_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not is_owner_update(update):
        await update.message.reply_text("无权限。")
        return
    text = extract_broadcast_all_text(update.message.text or "", "/broadcast_preview")
    if not text:
        text = str(context.user_data.get("broadcast_all_pending_text") or "")
    if not text:
        await update.message.reply_text("请在 /broadcast_preview 后面填写要预览的通知内容。")
        return
    context.user_data["broadcast_all_pending_text"] = text
    await update.message.reply_text(
        format_broadcast_preview(text, len(broadcast_all_targets())),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def broadcast_cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not is_owner_update(update):
        await update.message.reply_text("无权限。")
        return
    context.user_data.pop("broadcast_all_pending_text", None)
    context.user_data.pop("broadcast_selected", None)
    context.user_data.pop("broadcast_waiting_text", None)
    await update.message.reply_text("已取消广播任务。")


async def notify_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if not is_owner_update(update):
        await update.message.reply_text("无权限。")
        return
    text = extract_broadcast_all_text(update.message.text or "", "通知所有人")
    if not text:
        text = str(context.user_data.get("broadcast_all_pending_text") or "")
    if not text:
        await update.message.reply_text("请发送：通知所有人\\n通知内容，或先使用 /broadcast_preview 预览。")
        return
    targets = broadcast_all_targets()
    if not targets:
        await update.message.reply_text("没有可广播的用户。")
        return
    started_at = time.monotonic()
    success = 0
    failed = 0
    for user_id in targets:
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            success += 1
        except Exception:
            logger.exception("Broadcast to user %s failed", user_id)
            failed += 1
    context.user_data.pop("broadcast_all_pending_text", None)
    elapsed = time.monotonic() - started_at
    await update.message.reply_text(
        "通知所有人完成\n\n"
        f"成功数量：{success}\n"
        f"失败数量：{failed}\n"
        f"耗时：{elapsed:.2f} 秒"
    )


async def handle_new_chat_members(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.effective_chat or not update.effective_user:
        return
    remember_bot_chat(update)
    bot_user = await context.bot.get_me()
    for member in update.message.new_chat_members or []:
        if member.id == bot_user.id:
            ledger_store.set_chat_owner(update.effective_chat.id, update.effective_user.id)
            logger.info(
                "Set ledger owner for chat %s to inviter %s.",
                update.effective_chat.id,
                update.effective_user.id,
            )
            break


def group_welcome_message() -> str:
    return (
        "🎉 记账与卡密识别机器人已加入本群\n\n"
        "主要功能：\n"
        "• 发送图片可识别 PUBG / PSN 卡密\n"
        "• <code>+10000</code>：新增入款\n"
        "• <code>-100 备注</code>：新增下发\n"
        "• <code>账单</code>：查看当前账单\n"
        "• <code>设置汇率 10</code>：设置群汇率\n"
        "• <code>设置费率 10</code>：设置群费率\n"
        "• <code>设置实时汇率</code>：采用欧意 USDT/CNY 最新 1 档价格更新本群汇率\n"
        "• <code>设置日切 1点</code>：设置每日账务日切时间\n"
        "• <code>使用说明</code>：查看完整功能说明\n\n"
        "当前默认设置：\n"
        "汇率：1\n"
        "费率：0%\n"
        "日切：每天 00:00（北京时间）"
    )


async def handle_bot_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.my_chat_member or not update.effective_chat:
        return
    chat = update.effective_chat
    chat_type = getattr(chat, "type", "")
    if chat_type not in {"group", "supergroup"}:
        return
    old_status = getattr(update.my_chat_member.old_chat_member, "status", "")
    new_status = getattr(update.my_chat_member.new_chat_member, "status", "")
    if old_status not in {"left", "kicked"} or new_status not in {"member", "administrator"}:
        return
    now = time.monotonic()
    if now - welcome_sent_at.get(chat.id, 0) < 300:
        return
    welcome_sent_at[chat.id] = now
    title = getattr(chat, "title", "") or str(chat.id)
    ledger_store.remember_bot_chat(chat.id, title, chat_type)
    ledger_store.ensure_chat(chat.id)
    inviter_id = update.effective_user.id if update.effective_user else 0
    if inviter_id:
        ledger_store.set_chat_owner(chat.id, inviter_id)
    await context.bot.send_message(
        chat_id=chat.id,
        text=group_welcome_message(),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("使用说明", callback_data="ledger:help")]]),
        disable_web_page_preview=True,
    )


def user_label(update: Update) -> str:
    user = update.effective_user
    if not user:
        return "Unknown user"
    parts = [str(user.id)]
    if user.username:
        parts.append(f"@{user.username}")
    name = " ".join(part for part in [user.first_name, user.last_name] if part)
    if name:
        parts.append(name)
    return " | ".join(parts)


def chat_label(update: Update | None) -> str:
    chat = update.effective_chat if update else None
    if not chat:
        return "未知"
    chat_type = getattr(chat, "type", "")
    if chat_type == "private":
        return "私聊"
    title = getattr(chat, "title", "") or getattr(chat, "full_name", "") or "未命名群组"
    return f"群组（{title}）"


def audit_source_text(update: Update | None) -> str:
    if not update:
        return "来源: Unknown\n发送用户: Unknown"
    return f"来源: {html.escape(chat_label(update))}\n发送用户: {html.escape(user_label(update))}"


def audit_photo_file_ids(updates: list[Update]) -> list[str]:
    file_ids: list[str] = []
    seen: set[str] = set()
    for update in updates:
        message = getattr(update, "message", None)
        photos = getattr(message, "photo", None)
        if not photos:
            continue
        photo = photos[-1]
        file_id = getattr(photo, "file_id", "")
        if file_id and file_id not in seen:
            seen.add(file_id)
            file_ids.append(file_id)
    return file_ids


async def download_audit_photo_paths(updates: list[Update], context: ContextTypes.DEFAULT_TYPE) -> list[Path]:
    paths: list[Path] = []
    seen: set[str] = set()
    for update in updates:
        message = getattr(update, "message", None)
        photos = getattr(message, "photo", None)
        if not photos:
            continue
        photo = photos[-1]
        unique_id = getattr(photo, "file_unique_id", "") or getattr(photo, "file_id", "")
        if unique_id in seen:
            continue
        seen.add(unique_id)
        tg_file = await context.bot.get_file(photo.file_id)
        temp_dir = Path(tempfile.mkdtemp(prefix="s07_audit_"))
        image_path = temp_dir / f"{unique_id}.jpg"
        await tg_file.download_to_drive(custom_path=image_path)
        paths.append(image_path)
    return paths


def cleanup_audit_photo_paths(paths: list[Path]) -> None:
    for path in paths:
        try:
            path.unlink(missing_ok=True)
            path.parent.rmdir()
        except OSError:
            logger.warning("Failed to clean audit photo temp path: %s", path)


def parse_chat_id(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def update_is_from_owner(update: Update | None) -> bool:
    owner_id = parse_chat_id(OWNER_CHAT_ID)
    if owner_id is None or not update:
        return False
    if update.effective_user and update.effective_user.id == owner_id:
        return True
    return bool(update.effective_chat and update.effective_chat.id == owner_id)


def update_is_private_chat(update: Update | None) -> bool:
    if not update or not update.effective_chat:
        return False
    return getattr(update.effective_chat, "type", "") == "private"


def should_send_audit(updates: list[Update]) -> bool:
    first = updates[0] if updates else None
    if update_is_from_owner(first) and update_is_private_chat(first):
        return False
    return bool(AUDIT_BOT_TOKEN or AUDIT_CHAT_ID or OWNER_CHAT_ID)


def should_reply_to_source(updates: list[Update]) -> bool:
    return True


def format_source_reply(
    updates: list[Update],
    results: list[OcrResult],
    history_duplicates: list[CardHistoryDuplicate] | None = None,
) -> str:
    return append_history_duplicates(format_reply(results), history_duplicates or [])


async def notify_owner(
    updates: list[Update],
    results: list[OcrResult],
    context: ContextTypes.DEFAULT_TYPE,
    history_duplicates: list[CardHistoryDuplicate] | None = None,
) -> None:
    if not should_send_audit(updates):
        return
    first = updates[0] if updates else None
    target_chat_id = AUDIT_CHAT_ID or OWNER_CHAT_ID
    if not target_chat_id:
        return
    target_chat_id_int = parse_chat_id(target_chat_id)
    if target_chat_id_int is None:
        return
    if not AUDIT_BOT_TOKEN and first and first.effective_chat and first.effective_chat.id == target_chat_id_int:
        return
    text = audit_source_text(first) + "\n\n" + append_history_duplicates(format_reply(results), history_duplicates or [])
    if AUDIT_BOT_TOKEN:
        try:
            photo_paths = await download_audit_photo_paths(updates, context)
            if photo_paths:
                try:
                    await send_audit_bot_photos(target_chat_id_int, photo_paths, text)
                finally:
                    cleanup_audit_photo_paths(photo_paths)
            else:
                await send_audit_bot_message(target_chat_id_int, text)
        except Exception:
            logger.exception("Audit bot forwarding failed")
            try:
                await send_audit_bot_message(target_chat_id_int, text)
            except Exception:
                logger.exception("Audit bot text fallback failed")
        return
    await send_html_chunks(context, target_chat_id_int, text)


async def send_audit_bot_message(chat_id: int, text: str) -> None:
    url = f"https://api.telegram.org/bot{AUDIT_BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=TELEGRAM_TIMEOUT) as client:
        for chunk in split_html_message(text):
            response = await client.post(
                url,
                data={
                    "chat_id": str(chat_id),
                    "text": chunk,
                    "parse_mode": ParseMode.HTML,
                    "disable_web_page_preview": "true",
                },
            )
            response.raise_for_status()


async def send_audit_bot_photos(chat_id: int, photo_paths: list[Path], caption_text: str) -> None:
    photo_url = f"https://api.telegram.org/bot{AUDIT_BOT_TOKEN}/sendPhoto"
    caption_chunks = split_html_message(caption_text, limit=900)
    first_caption = caption_chunks[0] if caption_chunks else ""
    async with httpx.AsyncClient(timeout=TELEGRAM_TIMEOUT) as client:
        for index, photo_path in enumerate(photo_paths):
            data = {
                "chat_id": str(chat_id),
            }
            if index == 0 and first_caption:
                data["caption"] = first_caption
                data["parse_mode"] = ParseMode.HTML
            with photo_path.open("rb") as photo_file:
                response = await client.post(
                    photo_url,
                    data=data,
                    files={"photo": (photo_path.name, photo_file, "image/jpeg")},
                )
            response.raise_for_status()
    for extra_chunk in caption_chunks[1:]:
        await send_audit_bot_message(chat_id, extra_chunk)


def _trim_rate_window(records: list[float], now: float) -> None:
    cutoff = now - PHOTO_RATE_WINDOW_SECONDS
    while records and records[0] < cutoff:
        records.pop(0)


def photo_rate_limit_reason(update: Update, now: float | None = None) -> str | None:
    if not update.message or not update.effective_chat:
        return "消息无效"
    if is_owner_update(update):
        return None
    now = now if now is not None else time.time()
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id if update.effective_user else 0

    chat_records = photo_rate_chat[chat_id]
    _trim_rate_window(chat_records, now)
    if len(chat_records) >= PHOTO_RATE_LIMIT_PER_CHAT:
        return f"当前群图片发送太快，{PHOTO_RATE_WINDOW_SECONDS}秒内最多处理{PHOTO_RATE_LIMIT_PER_CHAT}张。"

    user_key = (chat_id, user_id)
    user_records = photo_rate_user[user_key]
    _trim_rate_window(user_records, now)
    if len(user_records) >= PHOTO_RATE_LIMIT_PER_USER:
        return f"当前用户图片发送太快，{PHOTO_RATE_WINDOW_SECONDS}秒内最多处理{PHOTO_RATE_LIMIT_PER_USER}张。"

    chat_records.append(now)
    user_records.append(now)
    return None


async def warn_photo_rate_limited(message, key: tuple[str, int], text: str) -> None:
    now = time.time()
    if now - photo_rate_warned_at.get(key, 0) < PHOTO_RATE_WINDOW_SECONDS:
        return
    photo_rate_warned_at[key] = now
    await message.reply_text(text)


async def flush_chat_batch(chat_id: int, context: ContextTypes.DEFAULT_TYPE, wait_seconds: float) -> None:
    async with chat_flush_locks[chat_id]:
        await asyncio.sleep(wait_seconds)
        updates = chat_buffers.pop(chat_id, [])
        chat_tasks.pop(chat_id, None)
        if not updates:
            return
        updates.sort(key=photo_display_order)
        message = updates[-1].message
        if not message:
            return

        await message.chat.send_action("typing")
        results: list[OcrResult] = []
        for batch_index, update in enumerate(updates, start=1):
            try:
                result = await recognize_update(update, context)
                result = replace(result, sequence_index=batch_index)
                corrected = apply_card_corrections(chat_id, result)
                corrected_pubg, corrected_psn = result_card_lines([corrected])
                if not corrected_pubg and not corrected_psn and result.raw_text.strip():
                    logger.info("Unrecognized OCR raw text: %s", result.raw_text.strip().replace("\n", " | ")[:1000])
                results.append(corrected)
                try:
                    append_today_ocr_cache(
                        list(corrected.cards) + [psn_key(line) or line for line in corrected.psn_ordered],
                        raw_candidates=exact_unique_text(list(result.cards) + list(result.psn_ordered)),
                        image_count=1,
                        path=TODAY_OCR_CACHE_PATH,
                    )
                except Exception:
                    logger.exception("Failed to write today OCR cache")
            except Exception:
                logger.exception("Batch image OCR failed")
                results.append(OcrResult(cards=tuple()))
                try:
                    append_today_ocr_cache([], image_count=1, path=TODAY_OCR_CACHE_PATH)
                except Exception:
                    logger.exception("Failed to write empty today OCR cache")

        if not has_card_results(results):
            return

        history_duplicates = register_card_history(updates, results)
        if should_reply_to_source(updates):
            await reply_html_chunks(
                message,
                format_source_reply(updates, results, history_duplicates),
            )
        await notify_owner(updates, results, context, history_duplicates)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    remember_bot_chat(update)
    remember_ledger_user(update)
    chat_id = update.message.chat_id
    if not ledger_store.is_recognition_enabled(chat_id):
        return
    rate_limit_reason = photo_rate_limit_reason(update)
    if rate_limit_reason:
        await warn_photo_rate_limited(update.message, ("rate", chat_id), rate_limit_reason)
        return
    owner_photo = is_owner_update(update)
    if not owner_photo and len(chat_buffers[chat_id]) >= PHOTO_BATCH_MAX_IMAGES:
        await warn_photo_rate_limited(
            update.message,
            ("batch", chat_id),
            f"当前批次图片已达到{PHOTO_BATCH_MAX_IMAGES}张，后续图片已保护性忽略，请等本批识别完成后再发。",
        )
        return
    await assign_photo_sequence(update)
    chat_buffers[chat_id].append(update)
    old_task = chat_tasks.get(chat_id)
    if old_task and not old_task.done():
        old_task.cancel()
    wait_seconds = 0.05 if owner_photo else (MULTI_BATCH_WAIT_SECONDS if len(chat_buffers[chat_id]) > 1 else SINGLE_WAIT_SECONDS)
    chat_tasks[chat_id] = asyncio.create_task(flush_chat_batch(chat_id, context, wait_seconds))


notify_all_cooldowns: dict[int, float] = {}


def is_group_update(update: Update | None) -> bool:
    if not update or not update.effective_chat:
        return False
    return getattr(update.effective_chat, "type", "") in {"group", "supergroup"}


def can_use_group_notify(update: Update | None) -> bool:
    if not update or not update.effective_chat or not update.effective_user:
        return False
    if is_owner_update(update):
        return True
    return ledger_store.is_operator(
        update.effective_chat.id,
        update.effective_user.id,
        ledger_owner_ids(update.effective_chat.id),
    )


def broadcast_group_keyboard(selected: set[int] | None = None) -> InlineKeyboardMarkup:
    selected = selected or set()
    rows: list[list[InlineKeyboardButton]] = []
    for row in ledger_store.list_active_bot_groups():
        chat_id = int(row["chat_id"])
        title = row["title"] or str(chat_id)
        prefix = "√" if chat_id in selected else "□"
        rows.append([InlineKeyboardButton(f"{prefix} {title}", callback_data=f"broadcast:toggle:{chat_id}")])
    rows.append(
        [
            InlineKeyboardButton("下一步", callback_data="broadcast:next"),
            InlineKeyboardButton("取消", callback_data="broadcast:cancel"),
        ]
    )
    return InlineKeyboardMarkup(rows)


async def start_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update_is_private_chat(update):
        return
    if not is_owner_update(update):
        await update.message.reply_text("无权限。")
        return
    groups = ledger_store.list_active_bot_groups()
    if not groups:
        await update.message.reply_text("还没有记录到可广播的群。请先让机器人加入群，并让群里产生一条消息。")
        return
    context.user_data["broadcast_selected"] = set()
    context.user_data["broadcast_waiting_text"] = False
    context.user_data.pop("broadcast_pending_text", None)
    await update.message.reply_text("请选择要广播的群：", reply_markup=broadcast_group_keyboard(set()))


async def broadcast_preview_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update_is_private_chat(update):
        return
    if not is_owner_update(update):
        await update.message.reply_text("无权限。")
        return
    selected = context.user_data.get("broadcast_selected")
    if not isinstance(selected, set) or not selected:
        await update.message.reply_text("请先使用 /broadcast 或“广播”选择要广播的群。")
        return
    text = extract_broadcast_all_text(update.message.text or "", "/broadcast_preview")
    if text:
        context.user_data["broadcast_pending_text"] = text
    text = str(context.user_data.get("broadcast_pending_text") or "")
    if not text:
        await update.message.reply_text("当前没有可预览的广播内容。")
        return
    titles = "\n".join(f"- {html.escape(title)}" for title in selected_broadcast_titles(selected))
    await update.message.reply_text(
        f"广播目标：\n{titles}\n\n广播内容：\n{html.escape(text)}",
        parse_mode=ParseMode.HTML,
    )


async def broadcast_cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update_is_private_chat(update):
        return
    if not is_owner_update(update):
        await update.message.reply_text("无权限。")
        return
    context.user_data.pop("broadcast_selected", None)
    context.user_data.pop("broadcast_waiting_text", None)
    context.user_data.pop("broadcast_pending_text", None)
    await update.message.reply_text("已取消广播。")


def selected_broadcast_titles(selected: set[int]) -> list[str]:
    groups = {int(row["chat_id"]): (row["title"] or str(row["chat_id"])) for row in ledger_store.list_active_bot_groups()}
    return [groups.get(chat_id, str(chat_id)) for chat_id in sorted(selected)]


async def handle_broadcast_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    if not is_owner_update(update):
        await query.edit_message_text("无权限。")
        return
    data = query.data or ""
    selected = context.user_data.get("broadcast_selected")
    if not isinstance(selected, set):
        selected = set()
    if data == "broadcast:cancel":
        context.user_data.pop("broadcast_selected", None)
        context.user_data.pop("broadcast_waiting_text", None)
        context.user_data.pop("broadcast_pending_text", None)
        await query.edit_message_text("已取消广播。")
        return
    if data == "broadcast:next":
        if not selected:
            await query.edit_message_text("请至少选择一个群。", reply_markup=broadcast_group_keyboard(selected))
            return
        context.user_data["broadcast_selected"] = selected
        context.user_data["broadcast_waiting_text"] = True
        await query.edit_message_text("请输入要广播的内容。")
        return
    if data == "broadcast:confirm":
        text = str(context.user_data.get("broadcast_pending_text") or "")
        if not selected or not text:
            await query.edit_message_text("广播任务已失效，请重新发送 /broadcast。")
            return
        started_at = time.monotonic()
        success = 0
        failed = 0
        for chat_id in sorted(selected):
            try:
                await context.bot.send_message(chat_id=chat_id, text=text)
                success += 1
            except Exception:
                logger.exception("Broadcast to chat %s failed", chat_id)
                failed += 1
        context.user_data.pop("broadcast_selected", None)
        context.user_data.pop("broadcast_waiting_text", None)
        context.user_data.pop("broadcast_pending_text", None)
        await query.edit_message_text(f"广播完成\n成功：{success}\n失败：{failed}\n耗时：{time.monotonic() - started_at:.2f}秒")
        return
    match = re.fullmatch(r"broadcast:toggle:(-?\d+)", data)
    if match:
        chat_id = int(match.group(1))
        if chat_id in selected:
            selected.remove(chat_id)
        else:
            selected.add(chat_id)
        context.user_data["broadcast_selected"] = selected
        await query.edit_message_reply_markup(reply_markup=broadcast_group_keyboard(selected))


async def handle_broadcast_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not update.message or not update_is_private_chat(update) or not is_owner_update(update):
        return False
    if not context.user_data.get("broadcast_waiting_text"):
        return False
    text = update.message.text or ""
    if text.strip() in {"取消", "取消广播", "/broadcast_cancel"}:
        context.user_data.pop("broadcast_selected", None)
        context.user_data.pop("broadcast_waiting_text", None)
        context.user_data.pop("broadcast_pending_text", None)
        await update.message.reply_text("已取消广播。")
        return True
    selected = context.user_data.get("broadcast_selected")
    if not isinstance(selected, set) or not selected:
        context.user_data.pop("broadcast_waiting_text", None)
        await update.message.reply_text("没有选择群，请重新发送 /broadcast。")
        return True
    context.user_data["broadcast_pending_text"] = text
    titles = "\n".join(f"- {html.escape(title)}" for title in selected_broadcast_titles(selected))
    preview = f"广播目标：\n{titles}\n\n广播内容：\n{html.escape(text)}"
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("确认发送", callback_data="broadcast:confirm"), InlineKeyboardButton("取消", callback_data="broadcast:cancel")]]
    )
    await update.message.reply_text(preview, reply_markup=keyboard, parse_mode=ParseMode.HTML)
    return True


def extract_notify_all_text(text: str) -> str:
    stripped = text.strip()
    for command in ("通知所有人", "/notify_all", "/at_all"):
        if stripped == command:
            return ""
        if stripped.startswith(command):
            return stripped[len(command) :].strip()
    return ""


def html_mention_for_member(row) -> str:
    username = (row["username"] or "").strip()
    if username:
        return "@" + html.escape(username.lstrip("@"))
    display_name = html.escape((row["display_name"] or "").strip() or str(row["user_id"]))
    return f'<a href="tg://user?id={int(row["user_id"])}">{display_name}</a>'


def chunked(values: list[str], size: int) -> list[list[str]]:
    return [values[index : index + size] for index in range(0, len(values), size)]


async def notify_all_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not is_group_update(update):
        return
    remember_bot_chat(update)
    remember_ledger_user(update)
    if not can_use_group_notify(update):
        await update.message.reply_text("无权限。")
        return
    chat_id = update.effective_chat.id
    now = time.monotonic()
    last_sent_at = notify_all_cooldowns.get(chat_id, 0)
    if now - last_sent_at < 300:
        await update.message.reply_text(f"通知所有人冷却中，请 {int(300 - (now - last_sent_at))} 秒后再试。")
        return
    members = ledger_store.list_active_known_members(chat_id, days=30)
    mentions = [html_mention_for_member(row) for row in members]
    if not mentions:
        await update.message.reply_text("当前群没有最近30天活跃成员缓存。")
        return
    content = extract_notify_all_text(update.message.text or "")
    notify_all_cooldowns[chat_id] = now
    chunks = chunked(mentions, 50)
    for index, mention_chunk in enumerate(chunks):
        parts = ["📢 通知所有人"]
        if content and index == 0:
            parts.extend(["", html.escape(content)])
        parts.extend(["", " ".join(mention_chunk)])
        await context.bot.send_message(
            chat_id=chat_id,
            text="\n".join(parts),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        if index < len(chunks) - 1:
            await asyncio.sleep(1)


async def notify_members_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not is_group_update(update):
        return
    remember_bot_chat(update)
    remember_ledger_user(update)
    if not can_use_group_notify(update):
        await update.message.reply_text("无权限。")
        return
    chat_id = update.effective_chat.id
    total = ledger_store.count_active_known_members(chat_id)
    recent_7 = ledger_store.count_active_known_members(chat_id, days=7)
    recent_30 = ledger_store.count_active_known_members(chat_id, days=30)
    await update.message.reply_text(
        "当前群成员缓存\n"
        f"缓存人数：{total}\n"
        f"最近7天活跃：{recent_7}\n"
        f"最近30天活跃：{recent_30}"
    )


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("Please set BOT_TOKEN in .env first")

    request_kwargs = {
        "connect_timeout": TELEGRAM_TIMEOUT,
        "read_timeout": TELEGRAM_TIMEOUT,
        "write_timeout": TELEGRAM_TIMEOUT,
        "pool_timeout": TELEGRAM_TIMEOUT,
    }
    if PROXY_URL:
        request_kwargs["proxy_url"] = PROXY_URL

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(HTTPXRequest(**request_kwargs))
        .get_updates_request(HTTPXRequest(**request_kwargs))
        .post_init(start_background_tasks)
        .post_shutdown(stop_background_tasks)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", show_id))
    app.add_handler(CommandHandler("version", show_version))
    app.add_handler(CommandHandler(["status", "ocr_status"], status_panel_command))
    app.add_handler(MessageHandler(filters.Regex(r"^/状态(?:@\w+)?(?:\s|$)"), status_panel_command))
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
            ["help", "bill", "fullbill", "yesterday", "undo", "clear", "in", "income", "out", "payout", "set_cutoff", "cutoff"],
            handle_ledger_text,
        )
    )
    app.add_handler(CallbackQueryHandler(handle_ledger_callback, pattern=r"^ledger:"))
    app.add_handler(ChatMemberHandler(handle_bot_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_new_chat_members))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_ledger_text))
    logger.info("Bot is starting. Version=%s.", BOT_VERSION)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
