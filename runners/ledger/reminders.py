# -*- coding: utf-8 -*-
"""日切账单提醒

原版是 asyncio 循环 + PTB 的 send_message，这里改成面板的 on_tick 驱动。

★ 保留原作者的防重设计：**投递前先「占坑」再发消息** ——
  万一超时了，也不会重复推给同一个群。
★ 判定条件也原样保留：**余额为负才推**，为正只记一行 skipped，不打扰群。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from core import log
from .commands import _summarize_entries
from .storage import LEDGER_TZ, LedgerStore

REMIND_HOUR = 9          # 每天几点检查（北京时间）


def format_cutoff_reminder(cutoff_at: datetime, balance: Decimal) -> str:
    cutoff_at = cutoff_at.astimezone(LEDGER_TZ)
    return (
        '📝系统 %s 日切账单\n'
        '▫️时间截: %s\n'
        '▫️余款:  %.2f'
        % (cutoff_at.strftime('%H:%M'),
           cutoff_at.strftime('%Y-%m-%d %H:%M:%S'),
           abs(balance))
    )


def initialize_schedule(store: LedgerStore, now: datetime) -> None:
    """记下「下次该检查的时间」（每天 9:00）"""
    now = now.astimezone(LEDGER_TZ)
    starts = now.replace(hour=REMIND_HOUR, minute=0, second=0, microsecond=0)
    if starts < now:
        starts += timedelta(days=1)
    with store.conn:
        store.conn.execute(
            'INSERT OR IGNORE INTO ledger_reminder_schedule VALUES (1, ?)',
            (starts.isoformat(),))


def send_due_reminders(api, store: LedgerStore, now: datetime) -> int:
    """到点了就推。返回推了几个群"""
    now = now.astimezone(LEDGER_TZ)
    due = now.replace(hour=REMIND_HOUR, minute=0, second=0, microsecond=0)
    row = store.conn.execute(
        'SELECT starts_at FROM ledger_reminder_schedule WHERE id = 1'
    ).fetchone()
    if row is None or now < due or due < datetime.fromisoformat(row['starts_at']):
        return 0

    sent = 0
    for group in store.list_active_bot_groups():
        try:
            chat_id = int(group['chat_id'])
        except (TypeError, ValueError):
            continue
        if not store.is_ledger_enabled(chat_id):
            continue
        closed = store.latest_closed_period(chat_id, due)
        if closed is None:
            continue
        period, cutoff_at = closed
        if store.conn.execute(
                'SELECT 1 FROM ledger_cutoff_reminders '
                'WHERE chat_id = ? AND accounting_date = ?',
                (chat_id, period)).fetchone():
            continue

        entries = store.entries(chat_id, accounting_date=period)
        balance = _summarize_entries(store, chat_id, entries).balance_usdt

        # ★ 先占坑再发：这样就算发送超时，也不会重复推
        with store.conn:
            claimed = store.conn.execute(
                'INSERT OR IGNORE INTO ledger_cutoff_reminders '
                '(chat_id, accounting_date, cutoff_at, balance_usdt, status) '
                'VALUES (?, ?, ?, ?, ?)',
                (chat_id, period, cutoff_at.isoformat(), str(balance),
                 'sending' if balance < 0 else 'skipped')).rowcount
        if not claimed or balance >= 0:
            continue

        try:
            r = api.call('sendMessage', chat_id=chat_id,
                         text=format_cutoff_reminder(cutoff_at, balance))
        except Exception as e:
            with store.conn:
                store.conn.execute(
                    'UPDATE ledger_cutoff_reminders SET status = "unconfirmed" '
                    'WHERE chat_id = ? AND accounting_date = ?',
                    (chat_id, period))
            log('日切提醒投递失败（%s），先记着，人工看一眼' % type(e).__name__)
            continue

        mid = (r or {}).get('message_id')
        with store.conn:
            store.conn.execute(
                'UPDATE ledger_cutoff_reminders SET status = "sent", '
                'message_id = ? WHERE chat_id = ? AND accounting_date = ?',
                (mid, chat_id, period))
        sent += 1
    return sent


def should_check(now: datetime) -> bool:
    """现在是不是该检查的时间（每分钟醒一次，但只有 9 点那一分钟才真查）"""
    n = now.astimezone(LEDGER_TZ)
    return n.hour == REMIND_HOUR and n.minute == 0
