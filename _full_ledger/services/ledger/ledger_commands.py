from __future__ import annotations

import re
from html import escape
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timezone, timedelta
from decimal import Decimal, InvalidOperation

from storage.repositories.ledger_storage import LedgerEntry, LedgerStore, LedgerSummary, money


@dataclass(frozen=True)
class Actor:
    user_id: int
    username: str
    display_name: str

    @property
    def label(self) -> str:
        if self.username:
            return f"@{self.username}"
        return self.display_name or str(self.user_id)


@dataclass(frozen=True)
class CommandResult:
    text: str
    changed: bool = False


AMOUNT_RE = re.compile(r"(?P<amount>\d+(?:\.\d+)?)")
SIGNED_AMOUNT_RE = re.compile(r"(?P<amount>-?\d+(?:\.\d+)?)")
ENTRY_NUMBER_RE = re.compile(r"#(?P<number>\d+)")
LOCAL_TZ = timezone(timedelta(hours=8))
RECENT_LIMIT = 3
BLUE_LINK = "https://t.me/"


HELP_TEXT = """【记账】

<code>+10000</code>：入款 10000 RMB

<code>-100 备注</code>：下发 100 U

<code>入款 100 备注</code>：新增入款

<code>下发 100 备注</code>：新增下发

<code>账单</code>：查看总额和最近流水

<code>撤销</code>：撤销最后一笔或回复指定流水撤销

<code>清空</code>：清空当前群账单

<code>关闭记账</code> / <code>开启记账</code>：暂停或恢复记账

<code>日切0</code>：默认每天凌晨 0 点账单自动归 0

【汇率与费率】

<code>设置汇率 1</code>：设置当前群固定汇率

<code>设置费率 0</code>：设置当前群费率为 0%

<code>查看费率</code>：查看当前群汇率和费率

<code>设置实时汇率</code>：使用欧意 USDT/CNY 最新 1 档价格更新当前群汇率

<code>币价</code> / <code>bj</code> / <code>z0</code>：查看欧意 USDT/CNY 最新 5 档价格

<code>设置日切 1点</code>：设置当前群每天 01:00 日切

<code>查看日切</code>：查看当前群日切时间和下次日切时间

日切后会自动开始新的当前账期。
历史流水不会删除。
修改日切只影响后续账期，不重算历史账单。
所有日切时间均为北京时间。

【说明】

默认新群汇率为 1。
默认新群费率为 0%。
费率从入款金额中扣除。
修改汇率和费率只影响后续新建账单。
历史账单使用创建时的汇率与费率快照，不会改变。

【权限】

<code>添加权限</code>：回复某人消息后发送

<code>删除权限</code>：回复某人消息后发送

<code>操作员</code>：查看操作员

【识别】

<code>关闭识别</code> / <code>开启识别</code>：暂停或恢复卡密识别
"""


def handle_text(
    store: LedgerStore,
    chat_id: int,
    actor: Actor,
    text: str,
    owner_ids: set[int],
    reply_user: Actor | None = None,
    reply_text: str | None = None,
    message_id: int | None = None,
    reply_message_id: int | None = None,
) -> CommandResult | None:
    raw = text.strip()
    if not raw:
        return None

    normalized = raw.replace("：", ":").strip()

    if normalized in {"开启记账", "打开记账", "启用记账", "开启"}:
        if not _is_owner(actor.user_id, owner_ids):
            return CommandResult("只有拉机器人进群的人可以开启记账。")
        store.set_ledger_enabled(chat_id, True)
        return CommandResult("记账功能已开启。", changed=True)

    if normalized in {"关闭记账", "停止记账", "停用记账", "暂停记账", "暂停"}:
        if not _is_owner(actor.user_id, owner_ids):
            return CommandResult("只有拉机器人进群的人可以关闭记账。")
        store.set_ledger_enabled(chat_id, False)
        return CommandResult("记账功能已关闭，已暂停记账，后续只识别卡密。发送“开启”可重新开启。", changed=True)

    if (
        normalized.startswith("日切")
        or normalized.startswith("设置日切")
        or normalized.startswith("/set_cutoff")
    ):
        if not _is_owner(actor.user_id, owner_ids):
            return CommandResult("只有拉机器人进群的人可以设置日切时间。")
        value = _first_signed_decimal(normalized)
        if value is None:
            hour = store.get_ledger_reset_hour(chat_id)
            return CommandResult(_format_cutoff_status(store, chat_id, hour))
        if value != value.to_integral_value():
            return CommandResult("格式：设置日切 0 到 设置日切 23，例如：设置日切 1点")
        try:
            hour = store.set_ledger_reset_hour(chat_id, int(value))
        except ValueError as exc:
            return CommandResult(str(exc))
        return CommandResult(
            "\n".join(
                [
                    f"✅ 当前群日切时间已设置为：每天 {hour:02d}:00",
                    "",
                    "当前账期不回溯修改。",
                    f"下一次日切时间：{store.next_cutoff_at(chat_id).strftime('%Y-%m-%d %H:%M')}（北京时间）",
                ]
            ),
            changed=True,
        )

    if not store.is_ledger_enabled(chat_id):
        return None

    if normalized in {"/start", "/help", "help", "帮助", "菜单"}:
        return CommandResult(HELP_TEXT)

    if normalized == "/id":
        return CommandResult(f"你的 ID：{actor.user_id}\n当前群 ID：{chat_id}")

    if normalized in {"查看日切", "/cutoff"}:
        return CommandResult(_format_cutoff_status(store, chat_id))

    if normalized in {"今日账单", "账单", "账目", "查账", "/bill"}:
        return CommandResult(format_bill(store, chat_id, scope="today", show_all_records=True))

    if normalized in {"昨日账单", "昨天账单", "/yesterday"}:
        return CommandResult(format_bill(store, chat_id, scope="yesterday", show_all_records=True))

    if normalized in {"完整账单", "全部账单", "总账单", "/fullbill"}:
        return CommandResult(format_bill(store, chat_id, scope="full", show_all_records=True))

    if normalized in {"操作员", "操作员列表"}:
        return CommandResult(format_operators(store, chat_id, owner_ids))

    if normalized.startswith("添加操作员") or normalized.startswith("添加权限"):
        if not _is_owner(actor.user_id, owner_ids):
            return CommandResult("只有拉机器人进群的人可以添加操作员。")
        if reply_user is None:
            return CommandResult("请回复要添加的用户消息，再发送：添加权限")
        store.add_operator(chat_id, reply_user.user_id, reply_user.username, reply_user.display_name, actor.user_id)
        return CommandResult(f"已添加操作员：{reply_user.label}", changed=True)

    if normalized.startswith("删除操作员") or normalized.startswith("删除权限"):
        if not _is_owner(actor.user_id, owner_ids):
            return CommandResult("只有拉机器人进群的人可以删除操作员。")
        if reply_user is None:
            return CommandResult("请回复要删除的用户消息，再发送：删除权限")
        removed = store.remove_operator(chat_id, reply_user.user_id)
        if not removed:
            return CommandResult("这个用户不是操作员。")
        return CommandResult(f"已删除操作员：{reply_user.label}", changed=True)

    if normalized in {"查看费率", "/fee_rate"}:
        if chat_id >= 0:
            return CommandResult("请在群内查看费率。")
        if not _can_operate(store, chat_id, actor.user_id, owner_ids):
            return CommandResult("无权限查看费率。")
        current_rate, fee_percent = store.get_settings(chat_id)
        return CommandResult(
            f"当前汇率：{_format_money(current_rate)}\n当前费率：{_format_percent(fee_percent)}"
        )

    if normalized.startswith("汇率") or normalized.startswith("设置汇率"):
        if chat_id >= 0:
            return CommandResult("请在群内设置汇率。")
        if not _can_operate(store, chat_id, actor.user_id, owner_ids):
            return CommandResult("无权限设置汇率。")
        value = _first_signed_decimal(normalized)
        if value is None:
            return CommandResult("格式：汇率 7.2 或 设置汇率7.2")
        try:
            new_rate = store.set_rate(chat_id, value)
        except ValueError as exc:
            return CommandResult(str(exc))
        return CommandResult(f"✅ 当前群汇率已设置为：{_format_money(new_rate)}", changed=True)

    if normalized.startswith("设置费率") or normalized.startswith("/set_fee") or normalized.startswith("费率"):
        if chat_id >= 0:
            return CommandResult("请在群内设置费率。")
        if not _can_operate(store, chat_id, actor.user_id, owner_ids):
            return CommandResult("无权限设置费率。")
        value = _first_signed_decimal(normalized)
        if value is None:
            return CommandResult("格式：设置费率10 或 /set_fee 10")
        try:
            fee = store.set_fee_percent(chat_id, value)
        except ValueError as exc:
            return CommandResult(str(exc))
        current_rate, current_fee = store.get_settings(chat_id)
        return CommandResult(
            "\n".join(
                [
                    f"✅ 当前群费率已设置为：{_format_percent(fee)}",
                    "",
                    f"当前汇率：{_format_money(current_rate)}",
                    f"当前费率：{_format_percent(current_fee)}",
                ]
            ),
            changed=True,
        )

    if normalized in {"撤销", "撤销账单", "回滚", "/undo"}:
        entry = store.entry_for_source_message(chat_id, reply_message_id) if reply_message_id is not None else None
        entry_number = store.active_entry_number(chat_id, entry.id) if entry is not None else None
        if entry is None:
            entry_number = _reply_entry_number(reply_text or "")
            if entry_number is None:
                return CommandResult("请回复要撤销的加分或下发消息，再发送：撤销")
            entry_id = store.entry_id_for_number(chat_id, entry_number)
            entry = store.void_entry(chat_id, entry_id) if entry_id is not None else None
        else:
            entry = store.void_entry(chat_id, entry.id)
        if entry is None:
            return CommandResult("没有找到这笔可撤销流水。")
        return CommandResult(
            f"已撤销：{format_entry(store, chat_id, entry, number=entry_number)}\n\n{format_bill(store, chat_id)}",
            changed=True,
        )

    if normalized in {"清账", "清空", "清空账单", "清除账单", "/clear"}:
        if not _is_owner(actor.user_id, owner_ids):
            return CommandResult("只有拉机器人进群的人可以清账。")
        count = store.clear_entries(chat_id)
        return CommandResult(f"已清空 {count} 笔流水，账单已重新计数。", changed=True)

    parsed = _parse_entry(normalized)
    if parsed is not None:
        kind, amount, note = parsed
        if amount == 0:
            return CommandResult(format_bill(store, chat_id, scope="today", show_all_records=True))
        try:
            entry = store.add_entry(
                chat_id=chat_id,
                kind=kind,
                amount=amount,
                currency="USDT",
                note=note,
                operator_id=actor.user_id,
                operator_name=actor.label,
                source_message_id=message_id,
            )
        except ValueError as exc:
            return CommandResult(str(exc))
        return CommandResult(format_bill(store, chat_id), changed=True)

    return None


def format_bill(store: LedgerStore, chat_id: int, scope: str = "today", show_all_records: bool = False) -> str:
    entries = _entries_for_scope(store, chat_id, scope)
    summary = _summarize_entries(store, chat_id, entries)
    title = _bill_title(scope)
    recent_entries = entries if show_all_records else entries[-RECENT_LIMIT:]
    numbered_entries = list(enumerate(entries, start=1))
    all_income_entries = [(number, entry) for number, entry in numbered_entries if entry.kind == "income"]
    all_payout_entries = [(number, entry) for number, entry in numbered_entries if entry.kind == "payout"]
    income_entries = all_income_entries[-RECENT_LIMIT:]
    payout_entries = all_payout_entries[-RECENT_LIMIT:]
    lines = [
        f"已入款({len(all_income_entries)}笔)",
        *_format_group_lines(income_entries),
        "--------------------------------",
        f"已下发({len(all_payout_entries)}笔)",
        *_format_group_lines(payout_entries),
        "--------------------------------",
        title,
        f"总入款金额：{summary.income}",
        f"汇率：{_format_money(summary.rate)}",
        f"费率：{_format_percent(summary.fee_percent)}",
        f"手续费：{summary.fees}",
        f"应下发：{summary.payable_amount} | {_format_usdt(summary.income_usdt)}U",
        f"已下发：{_format_usdt(summary.payout_usdt)}U",
        f"未下发：【{_blue(f'{_format_usdt(summary.balance_usdt)}U')}】",
    ]
    if recent_entries:
        lines.append("")
        lines.append("最近流水：")
        start_number = len(entries) - len(recent_entries) + 1
        for number, entry in enumerate(recent_entries, start=start_number):
            lines.append(format_entry(store, chat_id, entry, number=number, for_bill=True))
    return "\n".join(lines)


def format_entry(
    store: LedgerStore,
    chat_id: int,
    entry: LedgerEntry,
    number: int | None = None,
    for_bill: bool = False,
) -> str:
    sign = "+" if entry.kind == "income" else "-"
    label = "加分" if entry.kind == "income" else "下发"
    entry_time = _format_entry_time(entry.created_at)
    display_number = number if number is not None else store.active_entry_number(chat_id, entry.id)
    note = f" {escape(entry.note)}" if entry.note else ""
    operator_name = escape(entry.operator_name)
    display_amount = entry.net_amount if entry.kind == "income" else entry.net_amount
    amount_value = f"{sign}{display_amount} U"
    amount_text = _blue(amount_value) if entry.kind == "payout" else amount_value
    if for_bill:
        return f"#{display_number} {label}  {entry_time}：{amount_text}{note}\n ({operator_name})"
    return f"#{display_number} {label}  {entry_time}：{amount_text}{note}\n ({operator_name})"


def _format_entry_time(created_at: str) -> str:
    parsed = datetime.fromisoformat(created_at)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(LOCAL_TZ).strftime("%H:%M:%S")


def _format_group_lines(entries: list[tuple[int, LedgerEntry]]) -> list[str]:
    if not entries:
        return []
    lines = []
    for _, entry in entries:
        amount = f"{entry.amount}"
        net_amount = f"{entry.net_amount}U"
        if entry.kind == "payout":
            amount = f"-{amount}"
            calculation = _blue(f"{amount}U")
        else:
            calculation = f"{amount}/{_format_rate(entry.rate)}={net_amount}"
        lines.append(f"{_format_entry_time(entry.created_at)} {calculation}")
    return lines


def _format_rate(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _format_summary_rate(value: Decimal) -> str:
    text = _format_rate(value)
    return text if "." in text else f"{text}.0"


def _format_usdt(value: Decimal) -> str:
    return _format_money(value)


def _format_money(value: Decimal) -> str:
    return f"{money(value):.2f}"


def _format_percent(value: Decimal) -> str:
    return f"{money(value):.2f}%"


def _blue(value: object) -> str:
    return f'<a href="{BLUE_LINK}">{escape(str(value))}</a>'


def _reply_entry_number(text: str) -> int | None:
    match = ENTRY_NUMBER_RE.search(text)
    if match is None:
        return None
    return int(match.group("number"))


def _entries_for_scope(store: LedgerStore, chat_id: int, scope: str) -> list[LedgerEntry]:
    if scope == "today":
        return store.entries(chat_id, accounting_date=store.current_accounting_date(chat_id))
    if scope == "yesterday":
        return store.entries(chat_id, accounting_date=store.previous_accounting_date(chat_id))
    return store.entries(chat_id)


def _clear_previous_days(store: LedgerStore, chat_id: int) -> int:
    return 0


def _ledger_day_range_utc(store: LedgerStore, chat_id: int, offset_days: int) -> tuple[str, str]:
    return _day_range_utc(offset_days, reset_hour=store.get_ledger_reset_hour(chat_id))


def _day_range_utc(offset_days: int, reset_hour: int = 0) -> tuple[str, str]:
    now_local = datetime.now(LOCAL_TZ)
    local_day = now_local.date()
    if now_local.hour < reset_hour:
        local_day -= timedelta(days=1)
    local_day += timedelta(days=offset_days)
    start_local = datetime.combine(local_day, time(hour=reset_hour), tzinfo=LOCAL_TZ)
    end_local = start_local + timedelta(days=1)
    return (
        start_local.astimezone(UTC).isoformat(timespec="seconds"),
        end_local.astimezone(UTC).isoformat(timespec="seconds"),
    )


def _bill_title(scope: str) -> str:
    if scope == "today":
        return "今日账单"
    if scope == "yesterday":
        return "昨日账单"
    return "完整账单"


def _format_cutoff_status(store: LedgerStore, chat_id: int, hour: int | None = None) -> str:
    cutoff_hour = store.get_ledger_reset_hour(chat_id) if hour is None else hour
    current_rate, current_fee = store.get_settings(chat_id)
    current_period = store.current_accounting_date(chat_id)
    next_cutoff = store.next_cutoff_at(chat_id).strftime("%Y-%m-%d %H:%M")
    return "\n".join(
        [
            "📅 当前群账务设置",
            "",
            f"日切时间：每天 {cutoff_hour:02d}:00（北京时间）",
            f"当前账期：{current_period}",
            f"下次日切：{next_cutoff}",
            f"当前汇率：{_format_money(current_rate)}",
            f"当前费率：{_format_percent(current_fee)}",
        ]
    )


def _format_local_date(value: date) -> str:
    return f"{value.month}月{value.day}日"


def _summarize_entries(store: LedgerStore, chat_id: int, entries: list[LedgerEntry]) -> LedgerSummary:
    current_rate, fee_percent = store.get_settings(chat_id)
    income_rmb = sum((entry.amount for entry in entries if entry.kind == "income"), Decimal("0"))
    payable_rmb = sum((entry.payable_amount for entry in entries if entry.kind == "income"), Decimal("0"))
    income_usdt = sum((entry.payable_usdt for entry in entries if entry.kind == "income"), Decimal("0"))
    payout_usdt = sum((entry.net_amount for entry in entries if entry.kind == "payout"), Decimal("0"))
    fees = sum((entry.fee_amount for entry in entries if entry.kind == "income"), Decimal("0"))
    return LedgerSummary(
        income=money(income_rmb),
        payout=money(payout_usdt),
        fees=money(fees),
        payable_amount=money(payable_rmb),
        balance=money(max(income_usdt - payout_usdt, Decimal("0"))),
        income_usdt=money(income_usdt),
        payout_usdt=money(payout_usdt),
        balance_usdt=money(max(income_usdt - payout_usdt, Decimal("0"))),
        count=len(entries),
        rate=current_rate,
        fee_percent=fee_percent,
    )


def format_operators(store: LedgerStore, chat_id: int, owner_ids: set[int]) -> str:
    rows = store.list_operators(chat_id)
    lines = ["操作员列表"]
    if owner_ids:
        lines.append("老板：" + ", ".join(str(item) for item in sorted(owner_ids)))
    if not rows:
        lines.append("暂无操作员。")
        return "\n".join(lines)
    for row in rows:
        name = row["display_name"] or row["username"] or str(row["user_id"])
        username = f" @{row['username']}" if row["username"] else ""
        lines.append(f"- {name}{username} ({row['user_id']})")
    return "\n".join(lines)


def _parse_entry(text: str) -> tuple[str, Decimal, str] | None:
    if text.startswith("+"):
        value, note = _amount_after_prefix(text, "+")
        return ("income", value, note) if value is not None else None
    if text.startswith("-"):
        value, note = _amount_after_prefix(text, "-")
        return ("payout", value, note) if value is not None else None

    for prefix, kind in (
        ("/in", "income"),
        ("/income", "income"),
        ("/out", "payout"),
        ("/payout", "payout"),
        ("入款", "income"),
        ("收款", "income"),
        ("上分", "income"),
        ("下发", "payout"),
        ("出款", "payout"),
        ("下分", "payout"),
    ):
        if text.startswith(prefix):
            value, note = _amount_after_prefix(text, prefix)
            return (kind, value, note) if value is not None else None
    return None


def _amount_after_prefix(text: str, prefix: str) -> tuple[Decimal | None, str]:
    tail = text[len(prefix) :].strip()
    match = AMOUNT_RE.search(tail)
    if not match:
        return None, ""
    try:
        amount = Decimal(match.group("amount"))
    except InvalidOperation:
        return None, ""
    note = (tail[: match.start()] + tail[match.end() :]).strip(" -:，,")
    return amount, note


def _first_decimal(text: str) -> Decimal | None:
    match = AMOUNT_RE.search(text)
    if not match:
        return None
    try:
        return Decimal(match.group("amount"))
    except InvalidOperation:
        return None


def _first_signed_decimal(text: str) -> Decimal | None:
    match = SIGNED_AMOUNT_RE.search(text)
    if not match:
        return None
    try:
        return Decimal(match.group("amount"))
    except InvalidOperation:
        return None


def _is_owner(user_id: int, owner_ids: set[int]) -> bool:
    return user_id in owner_ids


def _can_operate(store: LedgerStore, chat_id: int, user_id: int, owner_ids: set[int]) -> bool:
    return store.is_operator(chat_id, user_id, owner_ids)

