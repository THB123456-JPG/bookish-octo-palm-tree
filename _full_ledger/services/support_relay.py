from __future__ import annotations

import html
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from telegram import Message, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes


RELAY_MAP_PATH = Path("outputs/support_relay_map.json")
RELAY_MAP_TTL_SECONDS = 30 * 24 * 3600


@dataclass(frozen=True)
class RelayTarget:
    chat_id: int
    user_id: int
    source_message_id: int
    created_at: float


def parse_owner_chat_id(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def relay_is_owner(update: Update | None, owner_chat_id: str) -> bool:
    owner_id = parse_owner_chat_id(owner_chat_id)
    if owner_id is None or not update:
        return False
    if update.effective_user and update.effective_user.id == owner_id:
        return True
    return bool(update.effective_chat and update.effective_chat.id == owner_id)


def relay_is_private(update: Update | None) -> bool:
    return bool(update and update.effective_chat and getattr(update.effective_chat, "type", "") == "private")


def relay_user_label(update: Update) -> str:
    user = update.effective_user
    chat = update.effective_chat
    if not user:
        return "Unknown"
    parts = [part for part in [user.first_name, user.last_name] if part]
    name = " ".join(parts) or user.username or str(user.id)
    username = f"@{user.username}" if user.username else ""
    chat_id = chat.id if chat else user.id
    return f"{html.escape(name)} {html.escape(username)}\nuser_id: <code>{user.id}</code>\nchat_id: <code>{chat_id}</code>"


def relay_map_path(path: Path | None = None) -> Path:
    return path or RELAY_MAP_PATH


def load_relay_map(path: Path | None = None, now: float | None = None) -> dict[str, RelayTarget]:
    path = relay_map_path(path)
    now = now if now is not None else time.time()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    targets: dict[str, RelayTarget] = {}
    for key, value in data.items():
        try:
            target = RelayTarget(
                chat_id=int(value["chat_id"]),
                user_id=int(value["user_id"]),
                source_message_id=int(value["source_message_id"]),
                created_at=float(value["created_at"]),
            )
        except (KeyError, TypeError, ValueError):
            continue
        if now - target.created_at <= RELAY_MAP_TTL_SECONDS:
            targets[str(key)] = target
    return targets


def save_relay_map(targets: dict[str, RelayTarget], path: Path | None = None) -> None:
    path = relay_map_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {key: asdict(value) for key, value in targets.items()}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def remember_relay_message(owner_message_id: int, target: RelayTarget, path: Path | None = None) -> None:
    targets = load_relay_map(path)
    targets[str(owner_message_id)] = target
    save_relay_map(targets, path)


def find_relay_target(owner_message_id: int, path: Path | None = None) -> RelayTarget | None:
    return load_relay_map(path).get(str(owner_message_id))


async def copy_or_send_text(context: ContextTypes.DEFAULT_TYPE, chat_id: int, source: Message, text: str) -> Message:
    try:
        return await context.bot.copy_message(
            chat_id=chat_id,
            from_chat_id=source.chat_id,
            message_id=source.message_id,
        )
    except Exception:
        return await context.bot.send_message(chat_id=chat_id, text=text)


async def relay_incoming_private_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    owner_chat_id: str,
    path: Path | None = None,
) -> bool:
    if not update.message or not relay_is_private(update) or relay_is_owner(update, owner_chat_id):
        return False
    owner_id = parse_owner_chat_id(owner_chat_id)
    if owner_id is None:
        return False

    header = "📨 用户通过机器人联系你\n\n" + relay_user_label(update) + "\n\n回复这条消息即可回给用户。"
    header_message = await context.bot.send_message(chat_id=owner_id, text=header, parse_mode=ParseMode.HTML)
    copied = await copy_or_send_text(
        context,
        owner_id,
        update.message,
        update.message.text or update.message.caption or "[非文本消息]",
    )
    target = RelayTarget(
        chat_id=update.effective_chat.id,
        user_id=update.effective_user.id if update.effective_user else update.effective_chat.id,
        source_message_id=update.message.message_id,
        created_at=time.time(),
    )
    remember_relay_message(header_message.message_id, target, path)
    remember_relay_message(copied.message_id, target, path)
    await update.message.reply_text("已收到，消息已转给管理员。")
    return True


async def relay_owner_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    owner_chat_id: str,
    path: Path | None = None,
) -> bool:
    if not update.message or not relay_is_private(update) or not relay_is_owner(update, owner_chat_id):
        return False
    reply = update.message.reply_to_message
    if not reply:
        return False
    target = find_relay_target(reply.message_id, path)
    if not target:
        return False
    await copy_or_send_text(
        context,
        target.chat_id,
        update.message,
        update.message.text or update.message.caption or "[非文本回复]",
    )
    await update.message.reply_text("已发送给用户。")
    return True
