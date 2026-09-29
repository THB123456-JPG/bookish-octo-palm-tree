from __future__ import annotations

import re
from html import escape
from dataclasses import dataclass
from datetime import date, datetime, time, timezone, timedelta
from decimal import Decimal, InvalidOperation

from .storage import LedgerEntry, LedgerStore, LedgerSummary, money


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
RECENT_OPERATOR_NAME_LIMIT = 6
MINE_LIMIT = 50          # 「/我」最多列几笔（列太多群里翻不动）
BLUE_LINK = "https://t.me/"


HELP_TEXT = """【记账】
<code>+100</code>入款｜<code>-100</code>减分｜<code>账单</code>/<code>+0</code>/<code>昨日账单</code>
<code>撤销</code>/<code>清空</code>｜<code>开启</code>（<code>上课</code>）/<code>关闭记账</code>（<code>下课</code>）
<code>/我</code>：只看<b>你自己</b>记的账（私聊发可选群统计）

【设置】
<code>设置汇率 1</code>/<code>设置费率 0</code>/<code>查看费率</code>
<code>设置实时汇率</code>/<code>币价</code>｜<code>日切3</code>/<code>查看日切</code>（北京时间）

【权限】
回复消息：<code>添加权限</code>/<code>删除权限</code>｜<code>操作员</code>

【其他】
<code>广播</code>（主人私聊）｜<code>/del</code>清理消息｜<code>通知所有人 内容</code>
算式→结果｜<b>群里</b>发TRC20地址→防篡改核对图

【查U · 监听】
<b>私聊机器人</b>发一个TRC20地址→查TRX/USDT余额和最近20笔转账
查完点「📡 加入监听」→ 有转入转出就通知你
币种、只看转入转出、最低金额，在地址簿里点那个地址改
查不动时（提示限流）可以配TronGrid Key（去 trongrid.io 免费申请）：
<code>设置密钥 你的Key</code> 或 <code>添加密钥 你的Key</code>（可加多把轮着用）
<code>密钥列表</code> 看配了几把
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

    # ★ 「上课 / 下课」是用户 2026-09-29 要的别名（群里喊一嗓子就开关）
    if normalized in {"开启记账", "打开记账", "启用记账", "开启", "上课"}:
        if not _can_operate(store, chat_id, actor.user_id, owner_ids):
            return CommandResult("只有群主或操作员可以开启记账。")
        store.set_ledger_enabled(chat_id, True)
        return CommandResult("记账功能已开启。", changed=True)

    if normalized in {"关闭记账", "停止记账", "停用记账", "暂停记账", "暂停",
                      "下课"}:
        if not _can_operate(store, chat_id, actor.user_id, owner_ids):
            return CommandResult("只有群主或操作员可以关闭记账。")
        store.set_ledger_enabled(chat_id, False)
        return CommandResult("记账功能已关闭，已暂停记账。发送“开启”可重新开启。", changed=True)

    if (
        normalized.startswith("日切")
        or normalized.startswith("设置日切")
        or normalized.startswith("/set_cutoff")
    ):
        if not _can_operate(store, chat_id, actor.user_id, owner_ids):
            return CommandResult("只有群主或操作员可以设置日切时间。")
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
                    "当前流水保留到下次日切，再开启新账期。",
                    f"下一次日切时间：{store.next_cutoff_at(chat_id).strftime('%Y-%m-%d %H:%M')}（北京时间）",
                ]
            ),
            changed=True,
        )

    if not store.is_ledger_enabled(chat_id):
        return None

    if normalized in {"/start", "/help", "help", "帮助", "菜单", "/使用说明"}:
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
        return CommandResult(format_bill(store, chat_id), changed=True)

    if normalized in {"清账", "清空", "清空账单", "清除账单", "/clear"}:
        if not _can_operate(store, chat_id, actor.user_id, owner_ids):
            return CommandResult("只有群主或操作员可以清账。")
        count = store.clear_entries(chat_id)
        return CommandResult(f"已清空 {count} 笔流水，账单已重新计数。", changed=True)

    parsed = _parse_entry(normalized)
    if parsed is not None:
        kind, amount, note = parsed
        if amount == 0:
            scope = "full" if normalized == "+0" else "today"
            return CommandResult(format_bill(store, chat_id, scope=scope, show_all_records=True))
        try:
            entry_note = _entry_attribution(kind, note, actor, reply_user)
            entry = store.add_entry(
                chat_id=chat_id,
                kind=kind,
                amount=amount,
                currency="USDT",
                note=entry_note,
                operator_id=actor.user_id,
                operator_name=actor.display_name or actor.label,
                source_message_id=message_id,
            )
        except ValueError as exc:
            return CommandResult(str(exc))
        if entry is None:
            return CommandResult("")
        return CommandResult(format_bill(store, chat_id), changed=True)

    return None


def is_mine_command(text: str) -> bool:
    """「/我」—— 统计**自己**的加账明细"""
    return text.strip() in {"/我", "/我的", "/me"}


def _mine_totals(entries: list[LedgerEntry]) -> tuple[Decimal, Decimal]:
    """(加分合计, 下发合计)。★ 跟 format_entry 显示的是同一个数 ——
    入款看 net_amount、下发取绝对值，不然两处对不上会让人以为算错了"""
    income = sum((e.net_amount for e in entries if e.kind == "income"),
                 Decimal("0"))
    payout = sum((abs(e.net_amount) for e in entries if e.kind == "payout"),
                 Decimal("0"))
    return income, payout


def _mine_entry_line(store: LedgerStore, chat_id: int,
                     entry: LedgerEntry) -> str:
    """「/我」里的一行：**带计算过程**、**不带操作人昵称**

    ★ 计算过程必须跟账单里长得一模一样（`100/6.67=14.84U`）——
      用户在群里看账单是这个格式，明细里对不上他就会怀疑算错了。
      （账单那边是 `_format_group_lines`，改这里时两边一起看）
    ★ 末尾**不显示操作人昵称**：抬头已经写了「灰产王 @HCW2026」，
      每条后面再挂一遍纯属啰嗦。**备注留着** —— 那是用户自己写的，
      跟「谁记的账」不是一回事。
    """
    number = store.active_entry_number(chat_id, entry.id)
    label = "减分" if entry.kind == "income" and entry.amount < 0 else (
        "加分" if entry.kind == "income" else "下发")
    if entry.kind == "payout":
        calc = _blue("-%sU" % _format_compact_money(abs(entry.amount)))
    else:
        calc = "%s/%s=%sU" % (_format_compact_money(entry.amount),
                              _format_compact_money(entry.rate),
                              _format_compact_money(entry.net_amount))
    # ★★ 账本有个自动规则：**没写备注时，会把发消息的人的名字填进 note**
    #    （见 _entry_attribution）。所以在「/我」里看到的 note 经常就等于
    #    操作人名 —— 那是系统填的、不是用户写的，**必须藏掉**。
    #    （跟账单里 hide_operator 是一个道理；第一版就是漏了这一步，
    #      用户截图里每条后面还挂着「偏门神」。）
    #    ★ 真备注照常显示：比如回复某人记的账、或者 `+100 张三` 那种。
    note = ""
    if entry.note and entry.note != entry.operator_name:
        note = " %s" % escape(entry.note)
    return "#%s %s  %s  %s%s" % (
        number if number is not None else "-", label,
        _format_entry_time(entry.created_at), calc, note)


def _mine_lines(store: LedgerStore, chat_id: int, entries: list[LedgerEntry],
                limit: int) -> list[str]:
    lines = []
    if len(entries) > limit:
        lines.append("（共 %d 笔，只列最近 %d 笔）" % (len(entries), limit))
        lines.append("")
    for entry in entries[-limit:]:
        lines.append(_mine_entry_line(store, chat_id, entry))
    income, payout = _mine_totals(entries)
    lines.append("")
    lines.append("共 %d 笔 ｜ 加分 +%s U ｜ 下发 -%s U"
                 % (len(entries), _format_compact_money(income),
                    _format_compact_money(payout)))
    return lines


def format_mine(store: LedgerStore, chat_id: int, user_id: int,
                title: str = "", limit: int = MINE_LIMIT) -> str:
    """某个人在**这个群**的加账明细（只算他自己记的账）

    ★ 为什么按 `operator_id` 而不是名字：名字会改、会重名，
      而 operator_id 就是「发那条记账消息的人」，唯一。
    """
    entries = store.entries(chat_id, operator_id=user_id)
    head = "📒 <b>加账明细</b>"
    if title:
        head += "\n" + escape(title)
    if not entries:
        return head + "\n\n这个群还没有你的记账记录。"
    return "\n".join([head, ""] + _mine_lines(store, chat_id, entries, limit))


def format_mine_multi(store: LedgerStore, user_id: int,
                      chats: list[tuple[int, str]], title: str = "",
                      limit: int = MINE_LIMIT) -> str:
    """跨多个群的加账明细。chats = [(chat_id, 群名), …]

    ★ 每个群给一段小计，最后给总计 —— 只看总计的话，
      某个群记错了根本发现不了。
    """
    head = "📒 <b>加账明细</b>"
    if title:
        head += "\n" + escape(title)
    blocks, total_in, total_out, total_n = [], Decimal("0"), Decimal("0"), 0
    for cid, cname in chats:
        try:
            entries = store.entries(cid, operator_id=user_id)
        except Exception:
            continue
        if not entries:
            continue
        total_n += len(entries)
        inc, out = _mine_totals(entries)
        total_in += inc
        total_out += out
        blocks.append("\n".join(
            ["<b>【%s】</b>" % escape(cname or str(cid))]
            + _mine_lines(store, cid, entries, limit)))
    if not blocks:
        return head + "\n\n这些群里都没有你的记账记录。"
    tail = ["", "━━━━━━━━━━", "合计 %d 笔 ｜ 加分 +%s U ｜ 下发 -%s U"
            % (total_n, _format_compact_money(total_in),
               _format_compact_money(total_out))]
    return "\n".join([head, ""] + blocks + tail)


def format_bill(store: LedgerStore, chat_id: int, scope: str = "today", show_all_records: bool = False) -> str:
    entries = _entries_for_scope(store, chat_id, scope)
    summary = _summarize_entries(store, chat_id, entries)
    view_mode = store.get_ledger_view_mode(chat_id)
    rate_label = _format_money(summary.rate) if store.is_realtime_rate(chat_id) else _format_rate(summary.rate)
    title = _bill_title(scope)
    recent_entries = entries if show_all_records else entries[-RECENT_LIMIT:]
    numbered_entries = list(enumerate(entries, start=1))
    all_income_entries = [(number, entry) for number, entry in numbered_entries if entry.kind == "income"]
    all_payout_entries = [(number, entry) for number, entry in numbered_entries if entry.kind == "payout"]
    income_entries = all_income_entries if show_all_records else all_income_entries[-RECENT_LIMIT:]
    payout_entries = all_payout_entries if show_all_records else all_payout_entries[-RECENT_LIMIT:]
    lines = [
        f"已入款({len(all_income_entries)}笔)",
        *_format_group_lines(income_entries, hide_operator=view_mode == "compact"),
        "--------------------------------",
        f"已下发({len(all_payout_entries)}笔)",
        *_format_group_lines(payout_entries, hide_operator=view_mode == "compact"),
        "--------------------------------",
        title,
        f"总入款金额：{_format_compact_money(summary.income)}",
        f"汇率：{rate_label} | 费率：{_format_percent(summary.fee_percent)}",
        "",
        f"应下发：{_format_compact_money(summary.payable_amount)} | {_format_usdt(summary.income_usdt)}U",
        f"已下发：{_format_usdt(summary.payout_usdt)}U",
        f"未下发：【{_blue(f'{_format_usdt(summary.balance_usdt)}U')}】",
    ]
    if recent_entries and view_mode == "detailed":
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
    label = "减分" if entry.kind == "income" and entry.amount < 0 else ("加分" if entry.kind == "income" else "下发")
    entry_time = _format_entry_time(entry.created_at)
    display_number = number if number is not None else store.active_entry_number(chat_id, entry.id)
    note = f" {escape(entry.note)}" if entry.note else ""
    operator_name = escape(entry.operator_name)
    display_amount = entry.net_amount if entry.kind == "income" else abs(entry.net_amount)
    amount_sign = "+" if entry.kind == "income" and display_amount >= 0 else ""
    amount_value = f"{amount_sign}{_format_compact_money(display_amount)} U" if entry.kind == "income" else f"{sign}{_format_compact_money(display_amount)} U"
    amount_text = _blue(amount_value) if entry.kind == "payout" else amount_value
    if for_bill:
        attribution = entry.note or entry.operator_name[:RECENT_OPERATOR_NAME_LIMIT]
        entry_prefix = f"#{display_number} {label}  "
        if not attribution:
            return f"{entry_prefix}{entry_time}：{amount_text}"
        return f"{entry_prefix}{entry_time}：{amount_text} {escape(attribution)}"
    return f"#{display_number} {label}  {entry_time}：{amount_text}{note}\n ({operator_name})"


def _format_entry_time(created_at: str) -> str:
    parsed = datetime.fromisoformat(created_at)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(LOCAL_TZ).strftime("%H:%M:%S")


def _format_group_lines(
    entries: list[tuple[int, LedgerEntry]],
    *,
    hide_operator: bool = False,
) -> list[str]:
    if not entries:
        return []
    lines = []
    for _, entry in entries:
        amount = _format_compact_money(entry.amount)
        net_amount = f"{_format_compact_money(entry.net_amount)}U"
        if entry.kind == "payout":
            calculation = _blue(f"-{_format_compact_money(abs(entry.amount))}U")
        else:
            calculation = f"{amount}/{_format_compact_money(entry.rate)}={net_amount}"
        attribution = entry.note or entry.operator_name
        if hide_operator and attribution == entry.operator_name:
            attribution = ""
        suffix = f" {escape(attribution)}" if attribution else ""
        lines.append(f"{_format_entry_time(entry.created_at)} {calculation}{suffix}")
    return lines


def _format_rate(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _format_summary_rate(value: Decimal) -> str:
    text = _format_rate(value)
    return text if "." in text else f"{text}.0"


def _format_usdt(value: Decimal) -> str:
    return _format_compact_money(value)


def _format_money(value: Decimal) -> str:
    return f"{money(value):.2f}"


def _format_compact_money(value: Decimal) -> str:
    return _format_rate(money(value))


def _format_percent(value: Decimal) -> str:
    return f"{_format_compact_money(value)}%"


def _blue(value: object) -> str:
    return f'<a href="{BLUE_LINK}">{escape(str(value))}</a>'


def _entry_attribution(kind: str, note: str, actor: Actor, reply_user: Actor | None) -> str:
    if reply_user is not None:
        return reply_user.display_name or reply_user.label
    if note:
        return note
    return actor.display_name or actor.label


def _reply_entry_number(text: str) -> int | None:
    match = ENTRY_NUMBER_RE.search(text)
    if match is None:
        return None
    return int(match.group("number"))


def _entries_for_scope(store: LedgerStore, chat_id: int, scope: str) -> list[LedgerEntry]:
    if scope in {"today", "full"}:
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
        start_local.astimezone(timezone.utc).isoformat(timespec="seconds"),
        end_local.astimezone(timezone.utc).isoformat(timespec="seconds"),
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
    current_period = store.current_accounting_date(chat_id).replace("T", " ").removesuffix("+08:00")
    period_end = store.next_cutoff_at(chat_id)
    closed = store.latest_closed_period(chat_id, period_end - timedelta(microseconds=1))
    if closed is not None:
        current_period = closed[1].strftime("%Y-%m-%d %H:%M")
    next_cutoff = period_end.strftime("%Y-%m-%d %H:%M")
    return "\n".join(
        [
            "📅 当前群账务设置",
            "",
            f"日切时间：每天 {cutoff_hour:02d}:00（北京时间）",
            f"当前账期：{current_period} 至 {next_cutoff}",
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
        balance=money(income_usdt - payout_usdt),
        income_usdt=money(income_usdt),
        payout_usdt=money(payout_usdt),
        balance_usdt=money(income_usdt - payout_usdt),
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
        return ("income", -value, note) if value is not None else None

    for prefix, kind in (
        ("/in", "income"),
        ("/income", "income"),
        ("入款", "income"),
        ("收款", "income"),
        ("上分", "income"),
        ("下发", "payout"),
        # ★ 下面这 4 个是**完整版有、面板版漏掉的**下发别名
        #   （2026-09-29 拿服务器上那套完整版逐字对比出来的）
        ("/out", "payout"),
        ("/payout", "payout"),
        ("出款", "payout"),
        ("下分", "payout"),
    ):
        if text.startswith(prefix):
            value, note = _amount_after_prefix(text, prefix, allow_signed=kind == "payout")
            return (kind, value, note) if value is not None else None
    return None


def _amount_after_prefix(text: str, prefix: str, *, allow_signed: bool = False) -> tuple[Decimal | None, str]:
    tail = text[len(prefix) :].strip()
    match = (SIGNED_AMOUNT_RE if allow_signed else AMOUNT_RE).search(tail)
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
