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
# ★ 金额后面**紧跟**的「/数字」= 这一笔单独的汇率（+9000/9 → 9000÷9=1000U）。
#   只认紧贴的：`+9000 定金1/2` 里那个斜杠是备注的一部分，不是汇率。
#   负号也收进来（`+9000/-3`）—— 不然后面那个 `-3` 会被当成普通备注，
#   而他明明是写错了汇率，得**报错**而不是默默按群汇率算
ENTRY_AMOUNT_RE = re.compile(
    r"(?P<amount>-?\d+(?:\.\d+)?)(?P<unit>[uU]?)(?P<modifiers>(?:[/*]-?\d+(?:\.\d+)?){0,2})")

# ★★ 「这就是一条记账，一个字都不多」的样子：`+9000`、`+9000/9`、`-30`…
#   为什么要单独认这个（2026-10-08 用户实测报的）：
#     `+1000/9` **既是**合法算式（1000÷9=111.11）**又是**合法记账（按 9 汇率），
#     而算式那关排在记账前面 → 不带备注时被算式**抢走**了
#     （`+1000/9 努力` 不是合法算式，才轮得到记账 —— 所以他看到的是
#      「加备注就对、不加备注就错」这种怪现象）。
#   判据要**卡死整串**（fullmatch）：`1000/9`（没符号）还是交给算式，
#   `+1000+200` 这种也还是算式，一律不受影响。
ENTRY_ONLY_RE = re.compile(r"[+-]\s*\d+(?:\.\d+)?[uU]?(?:[/*]-?\d+(?:\.\d+)?){0,2}")


def is_entry_text(text: str) -> bool:
    """这段文字是不是「一条记账」（最简形式，能带单笔汇率）"""
    return bool(ENTRY_ONLY_RE.fullmatch((text or "").strip()))
ENTRY_NUMBER_RE = re.compile(r"#(?P<number>\d+)")
LOCAL_TZ = timezone(timedelta(hours=8))
RECENT_LIMIT = 3
RECENT_OPERATOR_NAME_LIMIT = 6
MINE_LIMIT = 50          # 「/我」最多列几笔（列太多群里翻不动）
BLUE_LINK = "https://t.me/"


GROUP_READY_TEXT = ('✅️ 记账机器人已就绪。\n\n'
                    '授权操作人可记账。\n'
                    '添加操作人：<code>添加操作人 @用户名</code>\n'
                    '记账仅限授权人员。\n'
                    '使用说明：<code>帮助</code>')


HELP_TEXT = """【记账】
<code>+1000</code>入款｜<code>+1000*0.12</code>单笔费率｜<code>+1000u</code>U入款｜<code>-1000</code>入款修正｜<code>账单</code>/<code>+0</code>/<code>昨日账单</code>
<code>撤销</code>/<code>清空</code>｜<code>开启</code>（<code>上课</code>）/<code>关闭记账</code>（<code>下课</code>）
<code>/我</code>：只看<b>你自己</b>记的账（私聊发可选群统计）
<code>/统计</code>：仅机器人拥有者私聊，选群查看每个人的记账笔数与合计（不列明细）

【设置】
<code>设置汇率 1</code>/<code>设置费率 0</code>/<code>查看费率</code>（本群默认）
<code>设置汇率@aaaa7.3</code>/<code>设置费率@aaaa3</code>（个人参数，也可回复人员消息设置）
<code>设置汇率 @aaaa 默认</code>/<code>设置费率 @aaaa 默认</code>（恢复该项群默认）
<code>设置实时汇率</code>/<code>币价</code>/<code>bj</code>/<code>z0</code>
<code>设置日切时间3</code>/<code>关闭日切</code>/<code>显示日切</code>（北京时间）

【权限】
<code>添加操作人 @aaa @bbb</code>/<code>删除操作人 @aaa @bbb</code>（也可回复人员消息）
<code>显示操作人</code>｜仅拥有者、内部授权操作人员和本群操作人可记账

【其他】
<code>广播</code>（主人私聊）｜<code>/del</code>清理消息｜<code>通知所有人 内容</code>
算式→结果｜输入<code>G</code>查询贵金属实时价｜<b>群里</b>发TRC20地址→防篡改核对图
发号码查归属地：手机号/身份证/银行卡（群里私聊都行）

【入群欢迎语】
<b>私聊机器人</b>发 <code>设置欢迎语</code>→按提示发内容｜可发图｜「默认」恢复｜「关闭」不再欢迎
<code>{name}</code>换成成员名字（不写自动加在末尾）

【查U · 监听】
<b>私聊机器人</b>发TRC20地址→查TRX/USDT余额和最近20笔转账
点「📡 加入监听」→有转入转出就通知你｜币种/金额在地址簿里改
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
    options = getattr(store, 'customer_options', {})
    pure_u = options.get('pure_u', False)
    if pure_u:
        if normalized.startswith(('下发', '/下发', '/out', '/payout', '出款', '下分')) or re.match(r'\S+/?下发-?\d', normalized):
            return CommandResult('')
        if normalized.startswith('分红'):
            normalized = '下发' + normalized[2:]

    if chat_id < 0 and normalized == '/start':
        return CommandResult(GROUP_READY_TEXT)

    # ★ 「上课 / 下课」是用户 2026-09-29 要的别名（群里喊一嗓子就开关）
    if normalized in {"开启记账", "打开记账", "启用记账", "开启", "上课"}:
        if not _can_operate(store, chat_id, actor.user_id, owner_ids):
            return CommandResult("只有老板或操作人可以开启记账。")
        store.set_ledger_enabled(chat_id, True)
        return CommandResult("记账功能已开启。", changed=True)

    if normalized in {"关闭记账", "停止记账", "停用记账", "暂停记账", "暂停",
                      "下课"}:
        if not _can_operate(store, chat_id, actor.user_id, owner_ids):
            return CommandResult("只有老板或操作人可以关闭记账。")
        store.set_ledger_enabled(chat_id, False)
        return CommandResult("记账功能已关闭，已暂停记账。发送“开启”可重新开启。", changed=True)

    if normalized == "关闭日切":
        if chat_id >= 0:
            return CommandResult("请在群内发送：关闭日切")
        if not _can_operate(store, chat_id, actor.user_id, owner_ids):
            return CommandResult("只有老板或操作人可以关闭日切。")
        store.set_ledger_cutoff_enabled(chat_id, False)
        return CommandResult("✅ 当前群日切已关闭。\n从当前账期继续累计，不重新计入历史账期。\n"
                             "发送「设置日切时间3」等指令可恢复日切。", changed=True)

    if (
        normalized.startswith("日切")
        or normalized.startswith("设置日切")
        or normalized.startswith("/set_cutoff")
    ):
        if not _can_operate(store, chat_id, actor.user_id, owner_ids):
            return CommandResult("只有老板或操作人可以设置日切时间。")
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

    # Personnel management remains available while accounting is paused.
    if normalized == "显示操作人":
        if chat_id >= 0:
            return CommandResult("请在群内发送：显示操作人")
        return CommandResult(format_operators(store, chat_id, owner_ids))

    personnel_command = next((prefix for prefix in (
        "添加操作人", "删除操作人"
    ) if normalized.startswith(prefix)), None)
    if personnel_command:
        return _manage_operators(store, chat_id, actor, normalized, personnel_command,
                                 owner_ids, reply_user)

    if normalized in {"设置全员", "取消全员"}:
        if normalized == '设置全员':
            return CommandResult('仅拥有者、内部授权操作人员和本群操作人可记账，请先添加操作人。')
        if chat_id >= 0:
            return CommandResult("请在群内设置全员记账权限。")
        if not _is_owner(actor.user_id, owner_ids):
            return CommandResult("只有本群老板可以设置全员记账权限。")
        enabled = normalized == "设置全员"
        store.set_all_members_can_record(chat_id, enabled)
        return CommandResult("已设置全员：普通成员可以记账。" if enabled else
                             "已取消全员：仅老板和本群操作人可以记账。", changed=True)

    if not store.is_ledger_enabled(chat_id):
        return None

    if normalized in {"/start", "/help", "help", "帮助", "菜单", "/使用说明"}:
        return CommandResult(HELP_TEXT)

    if normalized == "/id":
        return CommandResult(f"你的 ID：{actor.user_id}\n当前群 ID：{chat_id}")

    if normalized in {"显示日切", "查看日切", "/cutoff"}:
        return CommandResult(_format_cutoff_status(store, chat_id))

    if normalized in {"今日账单", "账单", "账目", "查账", "/bill"}:
        return CommandResult(format_bill(store, chat_id, scope="today", show_all_records=True))

    if normalized in {"昨日账单", "昨天账单", "/yesterday"}:
        return CommandResult(format_bill(store, chat_id, scope="yesterday", show_all_records=True))

    if normalized in {"完整账单", "全部账单", "总账单", "/fullbill"}:
        return CommandResult(format_bill(store, chat_id, scope="full", show_all_records=True))

    if normalized.startswith("查看费率"):
        if chat_id >= 0:
            return CommandResult("请在群内查看费率。")
        if not _can_operate(store, chat_id, actor.user_id, owner_ids):
            return CommandResult("无权限查看费率。")
        prefix = '查看费率'
        tail = normalized[len(prefix):].strip()
        try:
            target = _pricing_actor(store, chat_id, tail) if tail else reply_user
        except ValueError as exc:
            return CommandResult(escape(str(exc)))
        current_rate, fee_percent = store.get_user_settings(chat_id, target.user_id) if target else store.get_settings(chat_id)
        return CommandResult(
            (f"{escape(target.label)} 的当前参数：\n" if target else '') +
            f"当前汇率：{_format_money(current_rate)}\n当前费率：{_format_percent(fee_percent)}"
        )

    if normalized.startswith("设置汇率"):
        if chat_id >= 0:
            return CommandResult("请在群内设置汇率。")
        if not _can_operate(store, chat_id, actor.user_id, owner_ids):
            return CommandResult("无权限设置汇率。")
        try:
            prefix = '设置汇率'
            target, value = _pricing_value(store, chat_id, normalized[len(prefix):], reply_user)
            if target:
                store.set_user_setting(chat_id, target.user_id, 'rate', value)
                return CommandResult(_personal_pricing_status(store, chat_id, target), changed=True)
            if value is None:
                raise ValueError("格式：设置汇率7.3；个人设置：设置汇率 @aaaa 7.3 或回复人员消息设置")
            new_rate = store.set_rate(chat_id, value)
        except ValueError as exc:
            return CommandResult(str(exc))
        return CommandResult(f"✅ 当前群汇率已设置为：{_format_money(new_rate)}", changed=True)

    if normalized.startswith("设置费率"):
        if chat_id >= 0:
            return CommandResult("请在群内设置费率。")
        if not _can_operate(store, chat_id, actor.user_id, owner_ids):
            return CommandResult("无权限设置费率。")
        try:
            prefix = '设置费率'
            target, value = _pricing_value(store, chat_id, normalized[len(prefix):], reply_user)
            if target:
                store.set_user_setting(chat_id, target.user_id, 'fee_percent', value)
                return CommandResult(_personal_pricing_status(store, chat_id, target), changed=True)
            if value is None:
                raise ValueError("格式：设置费率3；个人设置：设置费率 @aaaa 3 或回复人员消息设置")
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
        if not _can_record(store, chat_id, actor.user_id, owner_ids):
            return CommandResult("没有本群记账操作权限，不能撤销流水。")
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
            return CommandResult("只有老板或操作人可以清账。")
        count = store.clear_entries(chat_id)
        archived = getattr(store, 'last_customer_archive', None)
        suffix = f'\n存档编号：{archived}' if archived else ''
        return CommandResult(f"已清空 {count} 笔流水，账单已重新计数。" + suffix, changed=True)

    try:
        parsed = _parse_entry(normalized)
    except ValueError as exc:
        # 单笔汇率写得不对（0 或负数）—— 直接告诉他，别默默记账
        return CommandResult(str(exc))
    if parsed is not None:
        kind, amount, note, entry_rate, entry_fee, currency = parsed
        if kind == 'income' and (pure_u or (currency == 'CNY' and getattr(store, 'default_currency', {}).get(str(chat_id)) == 'U')):
            currency = 'U'
            if pure_u:
                entry_rate, entry_fee = Decimal('1'), Decimal('0')
        if amount == 0:
            scope = "full" if normalized == "+0" else "today"
            return CommandResult(format_bill(store, chat_id, scope=scope, show_all_records=True))
        if not _can_record(store, chat_id, actor.user_id, owner_ids):
            return CommandResult("没有本群记账权限，请由拥有者授权内部人员或本群操作人。")
        try:
            entry_note = _entry_attribution(kind, note, actor, reply_user)
            entry = store.add_entry(
                chat_id=chat_id,
                kind=kind,
                amount=amount,
                currency=currency,
                note=entry_note,
                operator_id=actor.user_id,
                operator_name=actor.display_name or actor.label,
                source_message_id=message_id,
                rate=entry_rate,
                pricing_user_id=reply_user.user_id if reply_user else actor.user_id,
                entry_fee_percent=entry_fee,
                flow_label='入款' if kind=='income' else ('分红' if pure_u else ('出款' if raw.startswith(('出款','下分','/out')) else '下发')),
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
    payout = sum((e.net_amount for e in entries if e.kind == "payout"),
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
    calc = _entry_calculation(entry)
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


def format_owner_statistics(store: LedgerStore, chats: list[tuple[int, str]]) -> str:
    """Summarize retained non-voided records by group and actual recorder ID."""
    lines = ["📊 <b>人员记账统计</b>", "统计全部未撤销流水（含历史账期），不列逐笔明细。", ""]
    total_n, total_in, total_out = 0, Decimal("0"), Decimal("0")
    for cid, title in chats:
        entries = store.entries(cid)
        lines.append("<b>【%s】</b>" % escape(title or str(cid)))
        if not entries:
            lines.extend(["本群暂无未撤销流水。", ""])
            continue
        people = {}
        for entry in entries:
            people.setdefault(entry.operator_id, []).append(entry)
        for uid, records in people.items():
            user = store.conn.execute("SELECT username, display_name FROM known_users WHERE chat_id=? AND user_id=?",
                                      (cid, uid)).fetchone()
            name = (user["display_name"] if user else "") or records[-1].operator_name or str(uid)
            username = (user["username"] if user else "").lstrip("@")
            label = name + (" @" + username if username else "")
            lines.append("<b>%s（ID %s）</b>" % (escape(label), uid))
            income, payout = _mine_totals(records)
            lines.append("共 %d 笔 ｜ 加分 +%s U ｜ 下发 -%s U" %
                         (len(records), _format_compact_money(income), _format_compact_money(payout)))
            lines.append("")
        income, payout = _mine_totals(entries)
        total_n += len(entries)
        total_in += income
        total_out += payout
        lines.extend(["本群合计 %d 笔 ｜ 加分 +%s U ｜ 下发 -%s U" %
                      (len(entries), _format_compact_money(income), _format_compact_money(payout)), ""])
    lines.extend(["━━━━━━━━━━", "全部所选群合计 %d 笔 ｜ 加分 +%s U ｜ 下发 -%s U" %
                  (total_n, _format_compact_money(total_in), _format_compact_money(total_out))])
    return "\n".join(lines)


def format_bill(store: LedgerStore, chat_id: int, scope: str = "today", show_all_records: bool = False) -> str:
    entries = _entries_for_scope(store, chat_id, scope)
    summary = _summarize_entries(store, chat_id, entries)
    view_mode = store.get_ledger_view_mode(chat_id)
    rate_label = _format_money(summary.rate) if store.is_realtime_rate(chat_id) else _format_rate(summary.rate)
    default_label = "默认" if store.conn.execute(
        "SELECT 1 FROM user_pricing WHERE chat_id=? AND (rate IS NOT NULL OR fee_percent IS NOT NULL) LIMIT 1",
        (chat_id,)).fetchone() else ""
    title = _bill_title(scope)
    recent_entries = entries if show_all_records else entries[-RECENT_LIMIT:]
    numbered_entries = list(enumerate(entries, start=1))
    all_income_entries = [(number, entry) for number, entry in numbered_entries if entry.kind == "income"]
    all_payout_entries = [(number, entry) for number, entry in numbered_entries if entry.kind == "payout"]
    income_entries = all_income_entries if show_all_records else all_income_entries[-RECENT_LIMIT:]
    payout_entries = all_payout_entries if show_all_records else all_payout_entries[-RECENT_LIMIT:]
    income_categories = []
    if view_mode == 'detailed' and all_income_entries:
        categories = {}
        for _, entry in all_income_entries:
            label = entry.note or entry.operator_name or str(entry.operator_id)
            totals = categories.setdefault(label, [0, Decimal('0'), Decimal('0')])
            totals[0] += 1
            totals[1] += entry.payable_amount if entry.currency == 'U' else entry.amount
            totals[2] += entry.payable_usdt
        income_categories = ['入款分类：', *[
            f'{escape(label)}({count}笔) {_format_compact_money(amount)} | {_format_usdt(usdt)}U'
            for label, (count, amount, usdt) in categories.items()
        ], '--------------------------------']
    lines = [
        f"已入款({len(all_income_entries)}笔)",
        *_format_group_lines(income_entries, hide_operator=view_mode == "compact"),
        "--------------------------------",
        f"已下发({len(all_payout_entries)}笔)",
        *_format_group_lines(payout_entries, hide_operator=view_mode == "compact"),
        "--------------------------------",
        *income_categories,
        title,
        f"总入款金额：{_format_compact_money(summary.income)}",
        f"{default_label}汇率：{rate_label} | {default_label}费率：{_format_percent(summary.fee_percent)}",
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
    options = getattr(store, 'customer_options', {})
    if scope != 'archive':
        period=store.previous_accounting_date(chat_id) if scope=='yesterday' else store.current_accounting_date(chat_id)
        month_opening=store.opening_balance(chat_id,period)
        if month_opening:
            lines.extend([f'月初结转余额：{_format_usdt(month_opening)}U',
                          f'含结转未下发：{_format_usdt(month_opening+summary.balance_usdt)}U'])
    if options.get('pure_u'):
        lines = [line.replace('下发', '分红') for line in lines
                 if '汇率：' not in line]
        lines = [line.replace('总入款金额：', '总入款金额（U）：') for line in lines]
    if options.get('carry_balance') and scope not in ('yesterday', 'archive'):
        previous = store.entries(chat_id, accounting_date=store.previous_accounting_date(chat_id))
        opening = (_summarize_entries(store, chat_id, previous).balance_usdt +
                   store.opening_balance(chat_id,store.previous_accounting_date(chat_id)))
        lines.extend([f'上期结余（期初）：{_format_usdt(opening)}U',
                      f'含期初未下发：{_format_usdt(opening + summary.balance_usdt)}U'])
    if options.get('categories') or (scope == 'full' and options.get('category_settlement')):
        categories = {}
        for entry in entries:
            categories.setdefault(entry.note or '未分类', []).append(entry)
        lines.append('\n备注分类结算：')
        for label, records in categories.items():
            subtotal = _summarize_entries(store, chat_id, records)
            count = sum(e.kind == 'income' for e in records)
            lines.append(f'{escape(label)}：入款{count}笔 | 应下发{_format_usdt(subtotal.income_usdt)}U | 已下发{_format_usdt(subtotal.payout_usdt)}U | 未下发{_format_usdt(subtotal.balance_usdt)}U')
    return "\n".join(lines)


def format_entry(
    store: LedgerStore,
    chat_id: int,
    entry: LedgerEntry,
    number: int | None = None,
    for_bill: bool = False,
) -> str:
    label = "减分" if entry.kind == "income" and entry.amount < 0 else ("入款" if entry.kind == "income" else "下发")
    entry_time = _format_entry_time(entry.created_at)
    display_number = number if number is not None else store.active_entry_number(chat_id, entry.id)
    note = f" {escape(entry.note)}" if entry.note else ""
    operator_name = escape(entry.operator_name)
    display_amount = entry.net_amount if entry.kind == "income" else -entry.net_amount
    amount_sign = "+" if display_amount >= 0 else ""
    amount_value = f"{amount_sign}{_format_compact_money(display_amount)} U"
    amount_text = _blue(amount_value) if entry.kind == "payout" else amount_value
    if for_bill:
        attribution = entry.note or entry.operator_name[:RECENT_OPERATOR_NAME_LIMIT]
        entry_prefix = f"#{display_number} {label}"
        if not attribution:
            return f"{entry_prefix}{entry_time}：{amount_text}"
        return f"{entry_prefix}{entry_time}：{amount_text} {escape(attribution)}"
    return f"#{display_number} {label}{entry_time}：{amount_text}{note}\n ({operator_name})"


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
        calculation = _entry_calculation(entry)
        attribution = entry.note or entry.operator_name
        if hide_operator and attribution == entry.operator_name:
            attribution = ""
        suffix = f" {escape(attribution)}" if attribution else ""
        lines.append(f"{_format_entry_time(entry.created_at)} {calculation}{suffix}")
    return lines


def _entry_calculation(entry: LedgerEntry) -> str:
    amount = _format_compact_money(entry.amount)
    if entry.kind == 'payout':
        value = _format_compact_money(-entry.amount)
        return _blue(f'{"+" if entry.amount < 0 else ""}{value}U')
    if entry.currency == 'U':
        return f'{_format_compact_money(entry.payable_amount)}/{_format_compact_money(entry.rate)}={_format_compact_money(entry.net_amount)}U'
    calculation = f'{amount}/{_format_compact_money(entry.rate)}={_format_compact_money(entry.net_amount)}U'
    if entry.fee_percent:
        calculation += f'（费率{_format_percent(entry.fee_percent)}）'
    return calculation


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
    if scope == 'archive':
        return store.entries(chat_id)
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
    if period_end is None:
        return "\n".join(["📅 当前群账务设置", "", "日切状态：已关闭，当前账期持续累计",
                          f"当前账期：{current_period} 起", "下次日切：已关闭",
                          "恢复日切：设置日切时间0 至 设置日切时间23",
                          f"当前汇率：{_format_money(current_rate)}",
                          f"当前费率：{_format_percent(current_fee)}"])
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
    income_rmb = sum((entry.payable_amount if entry.currency == 'U' else entry.amount
                      for entry in entries if entry.kind == "income"), Decimal("0"))
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
    lines = ["操作人列表"]
    if owner_ids:
        lines.append("老板：" + ", ".join(str(item) for item in sorted(owner_ids)))
    lines.append("记账权限：仅拥有者、内部授权操作人员和本群操作人")
    if not rows:
        lines.append("暂无操作人。")
        return "\n".join(lines)
    for row in rows:
        name = row["display_name"] or row["username"] or str(row["user_id"])
        username = f" @{row['username']}" if row["username"] else ""
        lines.append(f"- {escape(name)}{escape(username)} ({row['user_id']})")
    return "\n".join(lines)


def _parse_entry(text: str) -> tuple[str, Decimal, str, Decimal | None, Decimal | None, str] | None:
    name = ''
    if text.startswith(('+', '-')):
        if not re.match(r'[+-]\s*\d', text):
            return None
    elif not text.startswith(('/下发', '下发', '/out', '/payout', '出款', '下分')):
        named = re.fullmatch(r'([^+\-*/\s]{1,64}?)(/?下发-?\d.*)', text)
        if named is None:
            return None
        name, text = named.groups()
    prefixes = (('+', 'income'), ('-', 'income'), ('/下发', 'payout'), ('下发', 'payout'),
                ('/out', 'payout'), ('/payout', 'payout'), ('出款', 'payout'), ('下分', 'payout'))
    for prefix, kind in prefixes:
        if not text.startswith(prefix):
            continue
        tail = text[len(prefix):].strip()
        match = ENTRY_AMOUNT_RE.match(tail)
        if match is None:
            return None
        rest = tail[match.end():]
        if rest.startswith(('/', '*', 'u', 'U')):
            raise ValueError('格式：+1000/7.3、+1000*0.12、+1000u/7.3；参数不能重复')
        amount = Decimal(match['amount'])
        if prefix == '-':
            amount = -abs(amount)
        elif kind == 'income' and amount < 0:
            raise ValueError('入款修正请使用 -1000')
        modifiers = {}
        for symbol, number in re.findall(r'([/*])(-?\d+(?:\.\d+)?)', match['modifiers']):
            if symbol in modifiers:
                raise ValueError('单笔汇率或费率不能重复填写')
            modifiers[symbol] = Decimal(number)
        entry_rate = modifiers.get('/')
        entry_fee = modifiers.get('*')
        if kind == 'payout' and modifiers:
            raise ValueError('下发直接按 U 记账，不填写单笔汇率或费率')
        if entry_rate is not None and entry_rate <= 0:
            raise ValueError('汇率要大于 0，比如：+1000/7.3')
        if entry_fee is not None:
            if abs(entry_fee) < 1:
                entry_fee *= 100
            if entry_fee >= 100:
                raise ValueError('单笔费率必须小于100%，支持负数返佣')
        note = (tail[:match.start()] + rest).strip(' -:，,')
        if name:
            note = (name+' '+note).strip()
        currency = 'U' if match['unit'] or kind == 'payout' else 'CNY'
        return kind, amount, note, entry_rate, entry_fee, currency
    return None


def _pricing_actor(store: LedgerStore, chat_id: int, name: str) -> Actor:
    if not re.fullmatch(r'@[A-Za-z0-9_]{1,32}', name):
        raise ValueError('格式：@用户名；用户名和数值可用空格分开')
    row = store.find_known_user_by_username(chat_id, name)
    if row is None:
        row = next((r for r in store.list_operators(chat_id)
                    if r['username'].lower().lstrip('@') == name[1:].lower()), None)
    if row is None:
        raise ValueError('本群尚未识别该用户名，请让对方先发言或回复对方消息设置')
    return Actor(row['user_id'], row['username'], row['display_name'])


def _pricing_value(store: LedgerStore, chat_id: int, tail: str,
                   reply_user: Actor | None) -> tuple[Actor | None, Decimal | None]:
    tail = tail.strip(' :')
    target = reply_user
    if '@' in tail:
        parts = tail.split()
        if len(parts) == 2:
            target = _pricing_actor(store, chat_id, parts[0])
            tail = parts[1]
        elif len(parts) == 1 and tail.startswith('@'):
            names = {r[0].lower().lstrip('@') for r in store.conn.execute(
                'SELECT username FROM known_users WHERE chat_id=? UNION SELECT username FROM operators WHERE chat_id=?',
                (chat_id, chat_id)) if r[0]}
            matches = [(name, tail[len(name)+1:]) for name in names
                       if tail.lower().startswith('@'+name) and
                       re.fullmatch(r'-?\d+(?:\.\d+)?', tail[len(name)+1:])]
            if len(matches) != 1:
                raise ValueError('用户名未识别或写法有歧义，请加空格：设置汇率 @aaaa 7.3 / 设置费率 @aaaa 3')
            name, tail = matches[0]
            target = _pricing_actor(store, chat_id, '@'+name)
        else:
            raise ValueError('格式：设置汇率 @aaaa 7.3 / 设置费率 @aaaa 3')
    if tail == '默认' and target:
        return target, None
    if not re.fullmatch(r'-?\d+(?:\.\d+)?', tail):
        raise ValueError('请填写数值；也可回复人员消息设置，恢复个人群默认时填写「默认」')
    return target, Decimal(tail)


def _personal_pricing_status(store: LedgerStore, chat_id: int, target: Actor) -> str:
    current_rate, fee = store.get_user_settings(chat_id, target.user_id)
    return (f'✅ {escape(target.label)}（ID {target.user_id}）个人参数已更新。\n'
            f'当前汇率：{_format_money(current_rate)}\n当前费率：{_format_percent(fee)}\n'
            '仅影响后续入款，未单独设置的项目沿用本群默认。')


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


def _can_record(store: LedgerStore, chat_id: int, user_id: int, owner_ids: set[int]) -> bool:
    return store.is_operator(chat_id, user_id, owner_ids)


def _manage_operators(store: LedgerStore, chat_id: int, actor: Actor, text: str,
                      prefix: str, owner_ids: set[int], reply_user: Actor | None) -> CommandResult:
    if chat_id >= 0:
        return CommandResult("请在群内添加或删除操作人。")
    if not _is_owner(actor.user_id, owner_ids):
        return CommandResult("只有本群老板可以添加或删除操作人。")
    add = prefix.startswith("添加")
    command = "添加操作人" if add else "删除操作人"
    names = text[len(prefix):].split()
    if names and any(not re.fullmatch(r"@[A-Za-z0-9_]{1,32}", name) for name in names):
        return CommandResult(f"格式：{command} @aaa @bbb；也可回复人员消息发送：{command}")
    targets = []
    missing = []
    for name in names:
        row = store.find_known_user_by_username(chat_id, name)
        if row is None and not add:
            row = next((r for r in store.list_operators(chat_id)
                        if r["username"].lstrip("@").lower() == name[1:].lower()), None)
        if row is None:
            missing.append(name)
        else:
            targets.append(Actor(row["user_id"], row["username"], row["display_name"]))
    if missing:
        return CommandResult("本群尚未识别这些用户名：" + escape("、".join(missing)) +
                             "。请让对方先在本群发言，或回复对方消息操作；本次未修改权限。")
    if not names:
        if reply_user is None:
            return CommandResult(f"请回复要操作的人员消息，或发送：{command} @aaa @bbb")
        targets = [reply_user]
    targets = list({target.user_id: target for target in targets}.values())
    changed = False
    lines = []
    for target in targets:
        label = escape(target.label)
        if add:
            store.add_operator(chat_id, target.user_id, target.username, target.display_name, actor.user_id)
            changed = True
            lines.append("已添加操作人：" + label)
        elif store.remove_operator(chat_id, target.user_id):
            changed = True
            lines.append("已删除操作人：" + label)
        else:
            lines.append(label + " 不是本群操作人。")
    return CommandResult("\n".join(lines), changed=changed)
