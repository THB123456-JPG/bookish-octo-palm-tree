from __future__ import annotations


TELEGRAM_SAFE_TEXT_LIMIT = 3600


def split_html_message(text: str, limit: int = TELEGRAM_SAFE_TEXT_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    open_tag = ""

    def emit() -> None:
        nonlocal current, current_len
        if not current:
            return
        chunk = "\n".join(current)
        if open_tag and not chunk.endswith(f"</{open_tag}>"):
            chunk += f"\n</{open_tag}>"
        chunks.append(chunk)
        current = [f"<{open_tag}>"] if open_tag else []
        current_len = len(f"<{open_tag}>") if open_tag else 0

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
        for tag in ("pre", "blockquote"):
            if f"<{tag}>" in line and f"</{tag}>" not in line:
                open_tag = tag
            if f"</{tag}>" in line:
                open_tag = ""

    emit()
    return chunks or [text[:limit]]
