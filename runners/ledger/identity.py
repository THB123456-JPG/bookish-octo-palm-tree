from __future__ import annotations

from .commands import Actor


def actor_from_message(message) -> Actor | None:
    """Resolve the visible Telegram sender identity for ledger attribution."""
    user = getattr(message, "from_user", None)
    if user is not None:
        return _actor_from_user(user)

    sender_chat = getattr(message, "sender_chat", None)
    if sender_chat is not None:
        return _actor_from_chat(sender_chat)

    origin = getattr(message, "forward_origin", None)
    if origin is None:
        return None

    origin_user = getattr(origin, "sender_user", None)
    if origin_user is not None:
        return _actor_from_user(origin_user)

    origin_chat = getattr(origin, "sender_chat", None) or getattr(origin, "chat", None)
    if origin_chat is not None:
        return _actor_from_chat(origin_chat)

    hidden_name = str(getattr(origin, "sender_user_name", "") or "").strip()
    if hidden_name:
        return Actor(user_id=0, username="", display_name=hidden_name)
    return None


def _actor_from_user(user) -> Actor:
    display_name = " ".join(
        str(part).strip()
        for part in [getattr(user, "first_name", ""), getattr(user, "last_name", "")]
        if part
    )
    return Actor(
        user_id=int(getattr(user, "id", 0) or 0),
        username=str(getattr(user, "username", "") or ""),
        display_name=display_name,
    )


def _actor_from_chat(chat) -> Actor:
    username = str(getattr(chat, "username", "") or "")
    display_name = str(
        getattr(chat, "title", "")
        or getattr(chat, "full_name", "")
        or username
        or getattr(chat, "id", "")
    )
    return Actor(
        user_id=int(getattr(chat, "id", 0) or 0),
        username=username,
        display_name=display_name,
    )
