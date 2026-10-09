from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, time, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Iterable


MONEY_QUANT = Decimal("0.01")
RATE_QUANT = Decimal("0.0001")
LEDGER_TZ = timezone(timedelta(hours=8), "Asia/Shanghai")


def money(value: Decimal | int | float | str) -> Decimal:
    return Decimal(str(value)).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def rate(value: Decimal | int | float | str) -> Decimal:
    return Decimal(str(value)).quantize(RATE_QUANT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class LedgerEntry:
    id: int
    chat_id: int
    kind: str
    amount: Decimal
    currency: str
    rate: Decimal
    fee_percent: Decimal
    fee_amount: Decimal
    payable_amount: Decimal
    payable_usdt: Decimal
    net_amount: Decimal
    note: str
    operator_id: int
    operator_name: str
    source_message_id: int | None
    accounting_date: str
    created_at: str
    voided_at: str | None


@dataclass(frozen=True)
class LedgerSummary:
    income: Decimal
    payout: Decimal
    fees: Decimal
    payable_amount: Decimal
    balance: Decimal
    income_usdt: Decimal
    payout_usdt: Decimal
    balance_usdt: Decimal
    count: int
    rate: Decimal
    fee_percent: Decimal


class LedgerStore:
    def __init__(self, path: Path | str, *, initialize: bool = True):
        self.path = Path(path)
        if self.path.parent:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if initialize:
            self.init_schema()

    def close(self) -> None:
        self.conn.close()

    def init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS chat_settings (
                chat_id INTEGER PRIMARY KEY,
                rate TEXT NOT NULL DEFAULT '1.0000',
                fee_percent TEXT NOT NULL DEFAULT '0.0000',
                ledger_enabled INTEGER NOT NULL DEFAULT 1,
                ledger_reset_hour INTEGER NOT NULL DEFAULT 3,
                ledger_view_mode TEXT NOT NULL DEFAULT 'detailed',
                owner_id INTEGER,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS operators (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL DEFAULT '',
                display_name TEXT NOT NULL DEFAULT '',
                added_by INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (chat_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS user_pricing (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                rate TEXT,
                fee_percent TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (chat_id, user_id)
            );

            CREATE TABLE IF NOT EXISTS known_users (
                chat_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL DEFAULT '',
                display_name TEXT NOT NULL DEFAULT '',
                is_bot INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (chat_id, user_id)
            );


            CREATE TABLE IF NOT EXISTS entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                kind TEXT NOT NULL CHECK (kind IN ('income', 'payout')),
                amount TEXT NOT NULL,
                currency TEXT NOT NULL,
                rate TEXT NOT NULL,
                fee_percent TEXT NOT NULL,
                fee_amount TEXT NOT NULL DEFAULT '0.00',
                payable_amount TEXT NOT NULL DEFAULT '0.00',
                payable_usdt TEXT NOT NULL DEFAULT '0.00',
                net_amount TEXT NOT NULL,
                note TEXT NOT NULL DEFAULT '',
                operator_id INTEGER NOT NULL,
                operator_name TEXT NOT NULL DEFAULT '',
                accounting_date TEXT,
                created_at TEXT NOT NULL,
                voided_at TEXT
            );


            CREATE TABLE IF NOT EXISTS bot_chats (
                chat_id INTEGER PRIMARY KEY,
                title TEXT NOT NULL DEFAULT '',
                chat_type TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ledger_entry_messages (
                chat_id INTEGER NOT NULL,
                source_message_id INTEGER NOT NULL,
                PRIMARY KEY (chat_id, source_message_id)
            );

            CREATE TABLE IF NOT EXISTS ledger_bill_pages (
                chat_id INTEGER NOT NULL,
                root_message_id INTEGER NOT NULL,
                page_message_id INTEGER NOT NULL,
                PRIMARY KEY (chat_id, root_message_id, page_message_id)
            );

            CREATE TABLE IF NOT EXISTS ledger_reminder_schedule (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                starts_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS ledger_cutoff_reminders (
                chat_id INTEGER NOT NULL,
                accounting_date TEXT NOT NULL,
                cutoff_at TEXT NOT NULL,
                balance_usdt TEXT NOT NULL,
                status TEXT NOT NULL,
                message_id INTEGER,
                PRIMARY KEY (chat_id, accounting_date)
            );
            """
        )
        legacy_fee_columns = not {'fee_amount', 'payable_amount', 'payable_usdt', 'net_amount'} <= {
            row['name'] for row in self.conn.execute('PRAGMA table_info(entries)')}
        self._add_column_if_missing("entries", "source_message_id", "INTEGER")
        self._add_column_if_missing("entries", "flow_label", "TEXT NOT NULL DEFAULT '下发'")
        self._add_column_if_missing("entries", "fee_percent", "TEXT NOT NULL DEFAULT '0.0000'")
        self._add_column_if_missing("entries", "net_amount", "TEXT NOT NULL DEFAULT '0.00'")
        self._add_column_if_missing("entries", "fee_amount", "TEXT NOT NULL DEFAULT '0.00'")
        self._add_column_if_missing("entries", "payable_amount", "TEXT NOT NULL DEFAULT '0.00'")
        self._add_column_if_missing("entries", "payable_usdt", "TEXT NOT NULL DEFAULT '0.00'")
        self._add_column_if_missing("entries", "accounting_date", "TEXT")
        self._add_column_if_missing("chat_settings", "fee_percent", "TEXT NOT NULL DEFAULT '0.0000'")
        self._add_column_if_missing("chat_settings", "rate_is_realtime", "INTEGER NOT NULL DEFAULT 0")
        self._add_column_if_missing("chat_settings", "ledger_enabled", "INTEGER NOT NULL DEFAULT 1")
        self._add_column_if_missing("chat_settings", "all_members_can_record", "INTEGER NOT NULL DEFAULT 1")
        self._add_column_if_missing("chat_settings", "ledger_reset_hour", "INTEGER NOT NULL DEFAULT 3")
        self._add_column_if_missing("chat_settings", "ledger_cutoff_enabled", "INTEGER NOT NULL DEFAULT 1")
        self._add_column_if_missing("chat_settings", "ledger_transition_at", "TEXT")
        self._add_column_if_missing("chat_settings", "ledger_transition_current", "TEXT")
        self._add_column_if_missing("chat_settings", "ledger_transition_previous", "TEXT")
        self._add_column_if_missing("chat_settings", "ledger_transition_previous_cutoff", "TEXT")
        self._add_column_if_missing("chat_settings", "ledger_view_mode", "TEXT NOT NULL DEFAULT 'detailed'")
        self._add_column_if_missing("chat_settings", "owner_id", "INTEGER")
        self._add_column_if_missing("known_users", "is_bot", "INTEGER NOT NULL DEFAULT 0")
        self._add_column_if_missing("bot_chats", "migrated_to_chat_id", "INTEGER")
        # Zero can be a valid rounded net amount; backfill only when legacy columns were missing.
        if legacy_fee_columns:
            self._migrate_legacy_fee_snapshots()
        self._migrate_legacy_accounting_dates()
        self.conn.execute(
            "INSERT OR IGNORE INTO ledger_entry_messages SELECT chat_id, source_message_id "
            "FROM entries WHERE source_message_id IS NOT NULL"
        )
        self.conn.commit()

    def _add_column_if_missing(self, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _migrate_legacy_fee_snapshots(self) -> None:
        self.conn.execute(
            """
            UPDATE entries
            SET
                fee_percent = COALESCE(NULLIF(fee_percent, ''), '0.0000'),
                fee_amount = CASE
                    WHEN fee_amount IS NULL OR fee_amount = '' THEN '0.00'
                    ELSE fee_amount
                END,
                payable_amount = CASE
                    WHEN kind = 'income' AND (payable_amount IS NULL OR payable_amount = '' OR payable_amount = '0.00') THEN amount
                    WHEN payable_amount IS NULL OR payable_amount = '' THEN amount
                    ELSE payable_amount
                END,
                payable_usdt = CASE
                    WHEN kind = 'income' AND (payable_usdt IS NULL OR payable_usdt = '' OR payable_usdt = '0.00') AND CAST(net_amount AS REAL) = 0 THEN printf('%.2f', CAST(amount AS REAL) / CAST(rate AS REAL))
                    WHEN payable_usdt IS NULL OR payable_usdt = '' OR payable_usdt = '0.00' THEN net_amount
                    ELSE payable_usdt
                END,
                net_amount = CASE
                    WHEN kind = 'income' AND (net_amount IS NULL OR net_amount = '' OR net_amount = '0.00') THEN printf('%.2f', CAST(amount AS REAL) / CAST(rate AS REAL))
                    WHEN net_amount IS NULL OR net_amount = '' THEN amount
                    ELSE net_amount
                END
            """
        )

    def _migrate_legacy_accounting_dates(self) -> None:
        rows = self.conn.execute(
            """
            SELECT id, chat_id, created_at
            FROM entries
            WHERE accounting_date IS NULL OR accounting_date = ''
            ORDER BY id
            """
        ).fetchall()
        if not rows:
            return
        cutoff_cache: dict[int, int] = {}
        for row in rows:
            chat_id = int(row["chat_id"])
            if chat_id not in cutoff_cache:
                cutoff_cache[chat_id] = self.get_ledger_reset_hour(chat_id)
            accounting_date = self.accounting_date_for(row["created_at"], cutoff_cache[chat_id])
            self.conn.execute("UPDATE entries SET accounting_date = ? WHERE id = ?", (accounting_date, row["id"]))

    def remember_bot_chat(self, chat_id: int, title: str, chat_type: str) -> None:
        self.conn.execute(
            """
            INSERT INTO bot_chats (chat_id, title, chat_type, is_active, updated_at)
            VALUES (?, ?, ?, 1, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                title = excluded.title,
                chat_type = excluded.chat_type,
                is_active = CASE WHEN bot_chats.migrated_to_chat_id IS NULL THEN 1 ELSE 0 END,
                updated_at = excluded.updated_at
            """,
            (chat_id, title, chat_type, self._now()),
        )
        self.conn.commit()

    def list_active_bot_groups(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT chat_id, title, chat_type, updated_at
                FROM bot_chats
                WHERE is_active = 1 AND migrated_to_chat_id IS NULL
                    AND chat_type IN ('group', 'supergroup')
                ORDER BY title COLLATE NOCASE, updated_at DESC
                """
            )
        )

    def deactivate_bot_chat(self, chat_id: int) -> None:
        with self.conn:
            self.conn.execute("UPDATE bot_chats SET is_active = 0 WHERE chat_id = ?", (chat_id,))

    def migrate_bot_chat(self, old_id: int, new_id: int, title: str) -> None:
        """Retire a confirmed old group ID; accounting history stays untouched."""
        if old_id >= 0 or new_id >= 0 or old_id == new_id:
            raise ValueError("Invalid group migration")
        with self.conn:
            self.conn.execute(
                "INSERT INTO bot_chats (chat_id, title, chat_type, is_active, updated_at, migrated_to_chat_id) "
                "VALUES (?, ?, 'group', 0, ?, ?) ON CONFLICT(chat_id) DO UPDATE SET "
                "is_active = 0, migrated_to_chat_id = excluded.migrated_to_chat_id",
                (old_id, title, self._now(), new_id),
            )
            self.conn.execute(
                "INSERT OR IGNORE INTO bot_chats (chat_id, title, chat_type, is_active, updated_at) "
                "VALUES (?, ?, 'supergroup', 1, ?)", (new_id, title, self._now()),
            )

    def ensure_chat(self, chat_id: int) -> None:
        self.conn.execute(
            """
            INSERT OR IGNORE INTO chat_settings (chat_id, rate, fee_percent, ledger_reset_hour, created_at)
            VALUES (?, '1.0000', '0.0000', 3, ?)
            """,
            (chat_id, self._now()),
        )
        self.conn.commit()

    def bill_pages(self, chat_id: int, root_message_id: int) -> list[int]:
        return [row[0] for row in self.conn.execute(
            "SELECT page_message_id FROM ledger_bill_pages WHERE chat_id = ? AND root_message_id = ? "
            "ORDER BY page_message_id", (chat_id, root_message_id)
        )]

    def remember_bill_page(self, chat_id: int, root_message_id: int, page_message_id: int) -> None:
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO ledger_bill_pages VALUES (?, ?, ?)",
                              (chat_id, root_message_id, page_message_id))

    def forget_bill_page(self, chat_id: int, root_message_id: int, page_message_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM ledger_bill_pages WHERE chat_id = ? AND root_message_id = ? "
                              "AND page_message_id = ?", (chat_id, root_message_id, page_message_id))

    def get_settings(self, chat_id: int) -> tuple[Decimal, Decimal]:
        self.ensure_chat(chat_id)
        row = self.conn.execute("SELECT rate, fee_percent FROM chat_settings WHERE chat_id = ?", (chat_id,)).fetchone()
        return rate(row["rate"]), rate(row["fee_percent"])

    def set_rate(self, chat_id: int, value: Decimal | str, *, is_realtime: bool = False) -> Decimal:
        self.ensure_chat(chat_id)
        new_rate = rate(value)
        if new_rate <= 0:
            raise ValueError("汇率必须大于0")
        self.conn.execute(
            "UPDATE chat_settings SET rate = ?, rate_is_realtime = ? WHERE chat_id = ?",
            (str(new_rate), int(is_realtime), chat_id),
        )
        self.conn.commit()
        return new_rate

    def get_user_settings(self, chat_id: int, user_id: int) -> tuple[Decimal, Decimal]:
        defaults = self.get_settings(chat_id)
        row = self.conn.execute("SELECT rate, fee_percent FROM user_pricing WHERE chat_id=? AND user_id=?",
                                (chat_id, user_id)).fetchone()
        return tuple(Decimal(row[i]) if row and row[i] is not None else defaults[i] for i in range(2))

    def set_user_setting(self, chat_id: int, user_id: int, field: str,
                         value: Decimal | str | None) -> None:
        if field not in ('rate', 'fee_percent'):
            raise ValueError("个人参数无效")
        if value is not None:
            value = Decimal(str(value))
            if not value.is_finite():
                raise ValueError("参数必须是有限数字")
            value = rate(value)
            if field == 'rate' and value <= 0:
                raise ValueError("汇率必须大于0")
            if field == 'fee_percent' and not 0 <= value < 100:
                raise ValueError("费率范围为0到小于100")
        # field is restricted above; each override inherits the other group default independently.
        self.conn.execute(
            f"INSERT INTO user_pricing (chat_id, user_id, {field}, updated_at) VALUES (?, ?, ?, ?) "
            f"ON CONFLICT(chat_id, user_id) DO UPDATE SET {field}=excluded.{field}, updated_at=excluded.updated_at",
            (chat_id, user_id, str(value) if value is not None else None, self._now()),
        )
        self.conn.commit()

    def is_realtime_rate(self, chat_id: int) -> bool:
        self.ensure_chat(chat_id)
        row = self.conn.execute(
            "SELECT rate_is_realtime FROM chat_settings WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        return bool(row["rate_is_realtime"])

    def set_fee_percent(self, chat_id: int, value: Decimal | str) -> Decimal:
        self.ensure_chat(chat_id)
        new_fee = rate(value)
        if new_fee < 0:
            raise ValueError("费率不能为负数")
        if new_fee >= 100:
            raise ValueError("费率必须小于100")
        self.conn.execute("UPDATE chat_settings SET fee_percent = ? WHERE chat_id = ?", (str(new_fee), chat_id))
        self.conn.commit()
        return new_fee

    def is_ledger_enabled(self, chat_id: int) -> bool:
        self.ensure_chat(chat_id)
        row = self.conn.execute("SELECT ledger_enabled FROM chat_settings WHERE chat_id = ?", (chat_id,)).fetchone()
        return bool(row["ledger_enabled"])

    def set_ledger_enabled(self, chat_id: int, enabled: bool) -> None:
        self.ensure_chat(chat_id)
        self.conn.execute(
            "UPDATE chat_settings SET ledger_enabled = ? WHERE chat_id = ?",
            (1 if enabled else 0, chat_id),
        )
        self.conn.commit()

    def all_members_can_record(self, chat_id: int) -> bool:
        self.ensure_chat(chat_id)
        row = self.conn.execute("SELECT all_members_can_record FROM chat_settings WHERE chat_id = ?",
                                (chat_id,)).fetchone()
        return bool(row["all_members_can_record"])

    def set_all_members_can_record(self, chat_id: int, enabled: bool) -> None:
        self.ensure_chat(chat_id)
        self.conn.execute("UPDATE chat_settings SET all_members_can_record = ? WHERE chat_id = ?",
                          (int(enabled), chat_id))
        self.conn.commit()

    def get_ledger_view_mode(self, chat_id: int) -> str:
        self.ensure_chat(chat_id)
        row = self.conn.execute(
            "SELECT ledger_view_mode FROM chat_settings WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        return "compact" if row["ledger_view_mode"] == "compact" else "detailed"

    def set_ledger_view_mode(self, chat_id: int, mode: str) -> None:
        if mode not in {"compact", "detailed"}:
            raise ValueError("ledger view mode must be compact or detailed")
        self.ensure_chat(chat_id)
        self.conn.execute(
            "UPDATE chat_settings SET ledger_view_mode = ? WHERE chat_id = ?",
            (mode, chat_id),
        )
        self.conn.commit()


    def get_ledger_reset_hour(self, chat_id: int) -> int:
        self.ensure_chat(chat_id)
        row = self.conn.execute("SELECT ledger_reset_hour FROM chat_settings WHERE chat_id = ?", (chat_id,)).fetchone()
        return int(row["ledger_reset_hour"])

    def is_ledger_cutoff_enabled(self, chat_id: int) -> bool:
        self.ensure_chat(chat_id)
        return bool(self.conn.execute("SELECT ledger_cutoff_enabled FROM chat_settings WHERE chat_id = ?",
                                      (chat_id,)).fetchone()[0])

    def set_ledger_cutoff_enabled(self, chat_id: int, enabled: bool) -> None:
        if enabled:
            self.set_ledger_reset_hour(chat_id, self.get_ledger_reset_hour(chat_id))
            return
        if not self.is_ledger_cutoff_enabled(chat_id):
            return
        current, previous = self._accounting_periods(chat_id)
        self.conn.execute(
            "UPDATE chat_settings SET ledger_cutoff_enabled = 0, ledger_transition_at = NULL, "
            "ledger_transition_current = ?, ledger_transition_previous = ? WHERE chat_id = ?",
            (current, previous, chat_id),
        )
        self.conn.commit()

    def set_ledger_reset_hour(self, chat_id: int, hour: int, *, enable: bool = True) -> int:
        self.ensure_chat(chat_id)
        if hour < 0 or hour > 23:
            raise ValueError("日切时间必须是0到23点")
        if not enable:
            self.set_ledger_cutoff_enabled(chat_id, False)
            self.conn.execute("UPDATE chat_settings SET ledger_reset_hour = ? WHERE chat_id = ?", (hour, chat_id))
            self.conn.commit()
            return hour
        was_enabled = self.is_ledger_cutoff_enabled(chat_id)
        if was_enabled and self.get_ledger_reset_hour(chat_id) == hour:
            return hour
        now = datetime.now(LEDGER_TZ)
        current, previous = self._accounting_periods(chat_id, now)
        closed = self.latest_closed_period(chat_id, now)
        transition = None
        if not was_enabled or self.conn.execute("SELECT 1 FROM entries WHERE chat_id = ? LIMIT 1", (chat_id,)).fetchone():
            transition = datetime.combine(now.date(), time(hour=hour), tzinfo=LEDGER_TZ)
            if transition <= now:
                transition += timedelta(days=1)
        self.conn.execute(
            "UPDATE chat_settings SET ledger_cutoff_enabled = 1, ledger_reset_hour = ?, ledger_transition_at = ?, "
            "ledger_transition_current = ?, ledger_transition_previous = ?, "
            "ledger_transition_previous_cutoff = ? WHERE chat_id = ?",
            (hour, transition.isoformat() if transition else None, current, previous,
             closed[1].isoformat() if closed else None, chat_id),
        )
        self.conn.commit()
        return hour

    def accounting_date_for(self, created_at: str | datetime | None = None, reset_hour: int | None = None) -> str:
        if created_at is None:
            local_time = datetime.now(LEDGER_TZ)
        elif isinstance(created_at, datetime):
            parsed = created_at
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            local_time = parsed.astimezone(LEDGER_TZ)
        else:
            parsed = datetime.fromisoformat(created_at)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            local_time = parsed.astimezone(LEDGER_TZ)
        cutoff = 0 if reset_hour is None else reset_hour
        return (local_time - timedelta(hours=cutoff)).date().isoformat()

    def current_accounting_date(self, chat_id: int) -> str:
        return self._accounting_periods(chat_id)[0]

    def previous_accounting_date(self, chat_id: int) -> str:
        return self._accounting_periods(chat_id)[1]

    def _accounting_periods(self, chat_id: int, now: datetime | None = None) -> tuple[str, str]:
        self.ensure_chat(chat_id)
        now = now or datetime.now(LEDGER_TZ)
        row = self.conn.execute("SELECT * FROM chat_settings WHERE chat_id = ?", (chat_id,)).fetchone()
        if not row["ledger_cutoff_enabled"]:
            return row["ledger_transition_current"], row["ledger_transition_previous"]
        current = self.accounting_date_for(now.isoformat(), int(row["ledger_reset_hour"]))
        previous = (datetime.fromisoformat(current).date() - timedelta(days=1)).isoformat()
        if row["ledger_transition_at"]:
            boundary = datetime.fromisoformat(row["ledger_transition_at"])
            if now < boundary:
                return row["ledger_transition_current"], row["ledger_transition_previous"]
            # The first new period may share a date with the old one; use its full start time.
            if now < boundary + timedelta(days=1):
                return boundary.isoformat(), row["ledger_transition_current"]
            if now < boundary + timedelta(days=2):
                return current, boundary.isoformat()
        return current, previous

    def next_cutoff_at(self, chat_id: int, now: datetime | None = None) -> datetime | None:
        if not self.is_ledger_cutoff_enabled(chat_id):
            return None
        cutoff_hour = self.get_ledger_reset_hour(chat_id)
        local_now = (now or datetime.now(LEDGER_TZ)).astimezone(LEDGER_TZ)
        candidate = datetime.combine(local_now.date(), time(hour=cutoff_hour), tzinfo=LEDGER_TZ)
        if candidate <= local_now:
            candidate += timedelta(days=1)
        return candidate

    def latest_closed_period(self, chat_id: int, now: datetime) -> tuple[str, datetime] | None:
        """Return the closed period and its actual cutoff, including a pending cutoff change."""
        if not self.is_ledger_cutoff_enabled(chat_id):
            return None
        now = now.astimezone(LEDGER_TZ)
        _, previous = self._accounting_periods(chat_id, now)
        row = self.conn.execute("SELECT * FROM chat_settings WHERE chat_id = ?", (chat_id,)).fetchone()
        if row["ledger_transition_at"] and now < datetime.fromisoformat(row["ledger_transition_at"]):
            saved = row["ledger_transition_previous_cutoff"]
            # Legacy pending transitions did not retain the old cutoff hour; do not invent one.
            return (previous, datetime.fromisoformat(saved)) if saved else None
        return previous, self.next_cutoff_at(chat_id, now) - timedelta(days=1)

    def get_chat_owner_id(self, chat_id: int) -> int | None:
        self.ensure_chat(chat_id)
        row = self.conn.execute("SELECT owner_id FROM chat_settings WHERE chat_id = ?", (chat_id,)).fetchone()
        return int(row["owner_id"]) if row and row["owner_id"] is not None else None

    def set_chat_owner(self, chat_id: int, owner_id: int, replace: bool = False) -> None:
        self.ensure_chat(chat_id)
        if replace:
            self.conn.execute("UPDATE chat_settings SET owner_id = ? WHERE chat_id = ?", (owner_id, chat_id))
        else:
            self.conn.execute(
                "UPDATE chat_settings SET owner_id = COALESCE(owner_id, ?) WHERE chat_id = ?",
                (owner_id, chat_id),
            )
        self.conn.commit()

    def add_operator(self, chat_id: int, user_id: int, username: str, display_name: str, added_by: int) -> None:
        self.ensure_chat(chat_id)
        self.conn.execute(
            """
            INSERT OR REPLACE INTO operators (chat_id, user_id, username, display_name, added_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (chat_id, user_id, username, display_name, added_by, self._now()),
        )
        self.conn.commit()

    def remove_operator(self, chat_id: int, user_id: int) -> bool:
        cursor = self.conn.execute("DELETE FROM operators WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))
        self.conn.commit()
        return cursor.rowcount > 0

    def operator_chats(self, user_id: int) -> list[int]:
        """这个人在**哪些群**是操作员。

        ★ 「/我」在私聊里要判断「你是不是操作员」——
          可是私聊没有 chat_id，而权限是**按群**授的。
          所以取「他在任意一个群是操作员」就算数。
        """
        return [r["chat_id"] for r in self.conn.execute(
            "SELECT DISTINCT chat_id FROM operators WHERE user_id = ?",
            (int(user_id),))]

    def remember_user(self, chat_id: int, user_id: int, username: str, display_name: str, is_bot: bool = False) -> None:
        self.conn.execute(
            """
            INSERT INTO known_users (chat_id, user_id, username, display_name, is_bot, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, user_id) DO UPDATE SET
                username = excluded.username,
                display_name = excluded.display_name,
                is_bot = excluded.is_bot,
                updated_at = excluded.updated_at
            """,
            (chat_id, user_id, username.lstrip("@"), display_name, 1 if is_bot else 0, self._now()),
        )
        self.conn.commit()

    def find_known_user_by_username(self, chat_id: int, username: str) -> sqlite3.Row | None:
        username = username.lstrip("@").lower()
        return self.conn.execute(
            """
            SELECT user_id, username, display_name FROM known_users
            WHERE chat_id = ? AND lower(username) = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (chat_id, username),
        ).fetchone()

    def list_known_users_for_broadcast(self) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT user_id, username, display_name, is_bot, MAX(updated_at) AS updated_at
                FROM known_users
                WHERE user_id != 0 AND is_bot = 0
                GROUP BY user_id
                ORDER BY updated_at DESC
                """
            )
        )

    def list_active_known_members(self, chat_id: int, days: int = 30) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT user_id, username, display_name, is_bot, updated_at
                FROM known_users
                WHERE chat_id = ?
                  AND user_id != 0
                  AND is_bot = 0
                  AND updated_at >= datetime('now', ?)
                ORDER BY updated_at DESC
                """,
                (chat_id, f"-{days} days"),
            )
        )

    def count_active_known_members(self, chat_id: int, days: int | None = None) -> int:
        if days is None:
            row = self.conn.execute(
                "SELECT COUNT(*) AS total FROM known_users WHERE chat_id = ? AND user_id != 0 AND is_bot = 0",
                (chat_id,),
            ).fetchone()
        else:
            row = self.conn.execute(
                """
                SELECT COUNT(*) AS total
                FROM known_users
                WHERE chat_id = ?
                  AND user_id != 0
                  AND is_bot = 0
                  AND updated_at >= datetime('now', ?)
                """,
                (chat_id, f"-{days} days"),
            ).fetchone()
        return int(row["total"] if row else 0)

    def is_operator(self, chat_id: int, user_id: int, owner_ids: Iterable[int]) -> bool:
        if chat_id in getattr(self, 'customer_group_ops', {}).get(user_id, []):
            return True
        if user_id in set(owner_ids):
            return True
        row = self.conn.execute(
            "SELECT 1 FROM operators WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        ).fetchone()
        return row is not None

    def list_operators(self, chat_id: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT user_id, username, display_name FROM operators WHERE chat_id = ? ORDER BY created_at",
                (chat_id,),
            )
        )

    def add_entry(
        self,
        chat_id: int,
        kind: str,
        amount: Decimal | str,
        currency: str,
        note: str,
        operator_id: int,
        operator_name: str,
        source_message_id: int | None = None,
        rate: Decimal | str | None = None,
        pricing_user_id: int | None = None,
        entry_fee_percent: Decimal | str | None = None,
        replace_id: int | None = None,
        expected_entry: LedgerEntry | None = None,
        flow_label: str = '下发',
    ) -> LedgerEntry | None:
        if kind not in {"income", "payout"}:
            raise ValueError("kind must be income or payout")
        amount_value = money(amount)
        if kind == "income" and amount_value == 0:
            raise ValueError("amount must not be zero")
        if kind == "payout" and amount_value == 0:
            raise ValueError("amount must not be zero")

        current_rate, fee_percent = self.get_settings(chat_id)
        if kind == "income":
            current_rate, fee_percent = self.get_user_settings(
                chat_id, operator_id if pricing_user_id is None else pricing_user_id)
        # ★ 2026-10-08：这一笔可以**单独指定汇率**（`+9000/9 努力` = 9000÷9=1000U）。
        #   不传就用群汇率，跟以前一模一样。
        #   ★ 只对入款生效 —— 下发的数字本来就是 U，不除汇率；
        #     给下发传了也会被忽略（别以为能改下发金额）。
        #   ★ 存进去的是**这一笔实际用的汇率**（entries.rate 是快照），
        #     所以账单上会显示 `9000/9=1000U`，而且以后改群汇率不会改动它。
        if kind == "income" and rate is not None:
            entry_rate = money(rate)
            if entry_rate <= 0:
                raise ValueError("汇率必须大于 0")
            current_rate = entry_rate
        if kind == "income" and currency.upper() == 'U':
            fee_percent = Decimal('0')
            fee_amount = money('0')
            payable_amount = money(amount_value * current_rate)
            payable_usdt = amount_value
            net = amount_value
        elif kind == "income":
            if entry_fee_percent is not None:
                fee_percent = Decimal(str(entry_fee_percent))
                if not fee_percent.is_finite() or fee_percent >= 100:
                    raise ValueError('单笔费率必须小于100%，支持负数返佣')
                fee_percent = fee_percent.quantize(RATE_QUANT, rounding=ROUND_HALF_UP)
                if fee_percent >= 100:
                    raise ValueError('单笔费率必须小于100%')
            fee_amount = money(amount_value * fee_percent / Decimal("100"))
            payable_amount = money(amount_value - fee_amount)
            payable_usdt = money(payable_amount / current_rate)
            net = payable_usdt
        else:
            fee_amount = money("0")
            payable_amount = money(amount_value)
            payable_usdt = money(amount_value)
            net = amount_value
        now = self._now()
        accounting_date = self._accounting_periods(chat_id, datetime.fromisoformat(now))[0]
        with self.conn:
            if replace_id is not None:
                original = self.entry_for_source_message(chat_id, expected_entry.source_message_id) if expected_entry else None
                if original != expected_entry or original is None or original.id != replace_id:
                    raise ValueError('原流水已变化，请重新回复原消息修改')
            if source_message_id is not None:
                receipt = self.conn.execute(
                    "INSERT OR IGNORE INTO ledger_entry_messages VALUES (?, ?)",
                    (chat_id, source_message_id),
                )
                if receipt.rowcount == 0:
                    return None
            self.mark_history()
            if replace_id is not None:
                self.conn.execute(
                    'UPDATE entries SET kind=?,amount=?,currency=?,rate=?,fee_percent=?,fee_amount=?, '
                    'payable_amount=?,payable_usdt=?,net_amount=?,note=?,operator_id=?,operator_name=? '
                    'WHERE id=? AND chat_id=? AND voided_at IS NULL',
                    (kind,str(amount_value),currency.upper(),str(current_rate),str(fee_percent),str(fee_amount),
                     str(payable_amount),str(payable_usdt),str(money(net)),note,operator_id,operator_name,replace_id,chat_id))
                return self.get_entry(replace_id)
            cursor = self.conn.execute(
                """
                INSERT INTO entries (
                    chat_id, kind, amount, currency, rate, fee_percent, fee_amount,
                    payable_amount, payable_usdt, net_amount, note,
                    operator_id, operator_name, accounting_date, created_at, source_message_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_id,
                    kind,
                    str(amount_value),
                    currency.upper(),
                    str(current_rate),
                    str(fee_percent),
                    str(fee_amount),
                    str(payable_amount),
                    str(payable_usdt),
                    str(money(net)),
                    note,
                    operator_id,
                    operator_name,
                    accounting_date,
                    now,
                    source_message_id,
                ),
            )
            self.conn.execute('UPDATE entries SET flow_label=? WHERE id=?', (flow_label, cursor.lastrowid))
        return self.get_entry(int(cursor.lastrowid))

    def get_entry(self, entry_id: int) -> LedgerEntry:
        row = self.conn.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
        if row is None:
            raise KeyError(entry_id)
        return self._entry_from_row(row)

    def entries(
        self,
        chat_id: int,
        limit: int | None = None,
        start_at: str | None = None,
        end_at: str | None = None,
        accounting_date: str | None = None,
        operator_id: int | None = None,
    ) -> list[LedgerEntry]:
        conditions = ["chat_id = ?", "voided_at IS NULL"]
        params: list[object] = [chat_id]
        if start_at is not None:
            conditions.append("created_at >= ?")
            params.append(start_at)
        if end_at is not None:
            conditions.append("created_at < ?")
            params.append(end_at)
        if accounting_date is not None:
            conditions.append("accounting_date = ?")
            params.append(accounting_date)
        # ★ 「/我」用：只看某个人自己记的账（operator_id 就是发那条消息的人）
        if operator_id is not None:
            conditions.append("operator_id = ?")
            params.append(int(operator_id))
        sql = f"""
            SELECT * FROM entries
            WHERE {" AND ".join(conditions)}
            ORDER BY id ASC
        """
        if limit is not None:
            sql += " LIMIT ?"
            params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        return [self._entry_from_row(row) for row in rows]

    def recent_entries(self, chat_id: int, limit: int = 10) -> list[LedgerEntry]:
        rows = self.conn.execute(
            """
            SELECT * FROM entries
            WHERE chat_id = ? AND voided_at IS NULL
            ORDER BY id DESC
            LIMIT ?
            """,
            (chat_id, limit),
        ).fetchall()
        return [self._entry_from_row(row) for row in rows]

    def void_last_entry(self, chat_id: int) -> LedgerEntry | None:
        row = self.conn.execute(
            """
            SELECT * FROM entries
            WHERE chat_id = ? AND voided_at IS NULL
            ORDER BY id DESC
            LIMIT 1
            """,
            (chat_id,),
        ).fetchone()
        if row is None:
            return None
        self.conn.execute("UPDATE entries SET voided_at = ? WHERE id = ?", (self._now(), row["id"]))
        self.conn.commit()
        return self.get_entry(row["id"])

    def void_entry(self, chat_id: int, entry_id: int) -> LedgerEntry | None:
        row = self.conn.execute(
            """
            SELECT * FROM entries
            WHERE chat_id = ? AND id = ? AND voided_at IS NULL
            """,
            (chat_id, entry_id),
        ).fetchone()
        if row is None:
            return None
        self.conn.execute("UPDATE entries SET voided_at = ? WHERE id = ?", (self._now(), entry_id))
        self.conn.commit()
        return self.get_entry(entry_id)

    def entry_id_for_number(self, chat_id: int, number: int) -> int | None:
        if number <= 0:
            return None
        row = self.conn.execute(
            """
            SELECT id FROM entries
            WHERE chat_id = ? AND voided_at IS NULL
            ORDER BY id ASC
            LIMIT 1 OFFSET ?
            """,
            (chat_id, number - 1),
        ).fetchone()
        if row is None:
            return None
        return int(row["id"])

    def entry_for_source_message(self, chat_id: int, source_message_id: int) -> LedgerEntry | None:
        row = self.conn.execute(
            """
            SELECT * FROM entries
            WHERE chat_id = ? AND source_message_id = ? AND voided_at IS NULL
            ORDER BY id DESC
            LIMIT 1
            """,
            (chat_id, source_message_id),
        ).fetchone()
        if row is None:
            return None
        return self._entry_from_row(row)

    def active_entry_number(self, chat_id: int, entry_id: int) -> int:
        row = self.conn.execute(
            """
            SELECT COUNT(*) AS count FROM entries
            WHERE chat_id = ? AND voided_at IS NULL AND id <= ?
            """,
            (chat_id, entry_id),
        ).fetchone()
        return int(row["count"])

    def clear_entries(self, chat_id: int) -> int:
        count_row = self.conn.execute(
            "SELECT COUNT(*) AS count FROM entries WHERE chat_id = ? AND voided_at IS NULL",
            (chat_id,),
        ).fetchone()
        self.last_customer_archive = None
        with self.conn:
            self.retain_statistics('chat_id=?', (chat_id,))
            if getattr(self, 'customer_options', {}).get('archive_bill'):
                from .commands import format_bill
                self.conn.execute('CREATE TABLE IF NOT EXISTS customer_bill_archives(id INTEGER PRIMARY KEY, chat_id INTEGER, created_at TEXT, body TEXT)')
                body = '完整账单存档（全部留存账期）\n' + format_bill(self, chat_id, scope='archive', show_all_records=True)
                from html import escape
                body += '\n\n流水日期与币种：\n' + '\n'.join(
                    '%s · %s · %s %s · %s' % (entry.created_at, entry.kind, entry.amount, entry.currency, escape(entry.note))
                    for entry in self.entries(chat_id))
                cursor = self.conn.execute('INSERT INTO customer_bill_archives(chat_id,created_at,body) VALUES(?,?,?)', (chat_id,self._now(),body))
                self.last_customer_archive = cursor.lastrowid
            self.conn.execute("DELETE FROM entries WHERE chat_id = ?", (chat_id,))
            if self.conn.execute("SELECT 1 FROM sqlite_master WHERE name='customer_opening_balances'").fetchone():
                self.conn.execute('DELETE FROM customer_opening_balances WHERE chat_id=?',(chat_id,))
        return int(count_row["count"])

    def clear_entries_before(self, chat_id: int, cutoff_at: str) -> int:
        count_row = self.conn.execute(
            "SELECT COUNT(*) AS count FROM entries WHERE chat_id = ? AND created_at < ?",
            (chat_id, cutoff_at),
        ).fetchone()
        self.retain_statistics('chat_id=? AND created_at<?', (chat_id,cutoff_at))
        self.conn.execute(
            "DELETE FROM entries WHERE chat_id = ? AND created_at < ?",
            (chat_id, cutoff_at),
        )
        self.conn.commit()
        return int(count_row["count"])

    def clear_all_entries(self) -> int:
        self.retain_statistics('1=1', ())
        cursor = self.conn.execute("DELETE FROM entries")
        self.conn.commit()
        return cursor.rowcount

    def retain_statistics(self, condition, params):
        """Clear the active bill while retaining this month's original monetary snapshots."""
        self.conn.execute('CREATE TABLE IF NOT EXISTS customer_month_entries AS SELECT * FROM entries WHERE 0')
        if self.conn.execute('SELECT 1 FROM entries WHERE '+condition+' LIMIT 1',params).fetchone():
            self.mark_history()
        self.conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS customer_month_entry_id ON customer_month_entries(id)')
        month = datetime.now(LEDGER_TZ).strftime('%Y-%m-01')
        self.conn.execute('INSERT OR IGNORE INTO customer_month_entries SELECT * FROM entries WHERE '
                          + condition + " AND voided_at IS NULL AND date(created_at,'+8 hours')>=?", (*params,month))

    def prune_month(self):
        """Keep current-month bills; preserve only opening balances for live financial periods."""
        month = datetime.now(LEDGER_TZ).strftime('%Y-%m-01')
        with self.conn:
            if self.conn.execute('SELECT 1 FROM entries LIMIT 1').fetchone():
                self.mark_history()
            self.conn.execute('CREATE TABLE IF NOT EXISTS customer_opening_balances(chat_id INTEGER, period TEXT, balance TEXT, PRIMARY KEY(chat_id,period))')
            for row in self.conn.execute('SELECT chat_id FROM chat_settings').fetchall():
                cid=row[0]
                keep={self.current_accounting_date(cid),self.previous_accounting_date(cid)}
                records=self.conn.execute("SELECT * FROM entries WHERE chat_id=? AND voided_at IS NULL AND date(created_at,'+8 hours')<?",(cid,month)).fetchall()
                for period in keep:
                    balance=sum((Decimal(r['payable_usdt']) if r['kind']=='income' else -Decimal(r['net_amount']) for r in records if r['accounting_date']==period),Decimal(0))
                    old=self.conn.execute('SELECT balance FROM customer_opening_balances WHERE chat_id=? AND period=?',(cid,period)).fetchone()
                    self.conn.execute('INSERT OR REPLACE INTO customer_opening_balances VALUES(?,?,?)',(cid,period,str(balance+(Decimal(old[0]) if old else 0))))
                for old in self.conn.execute('SELECT period FROM customer_opening_balances WHERE chat_id=?',(cid,)).fetchall():
                    if old[0] not in keep:
                        self.conn.execute('DELETE FROM customer_opening_balances WHERE chat_id=? AND period=?',(cid,old[0]))
            self.conn.execute("DELETE FROM entries WHERE date(created_at,'+8 hours')<?",(month,))
            tables={r[0] for r in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for table in ('customer_month_entries','customer_bill_archives'):
                if table in tables:
                    self.conn.execute("DELETE FROM "+table+" WHERE date(created_at,'+8 hours')<?",(month,))

    def opening_balance(self, chat_id, period):
        exists=self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='customer_opening_balances'").fetchone()
        row=self.conn.execute('SELECT balance FROM customer_opening_balances WHERE chat_id=? AND period=?',(chat_id,period)).fetchone() if exists else None
        return Decimal(row[0]) if row else Decimal(0)

    def mark_history(self):
        self.conn.execute('CREATE TABLE IF NOT EXISTS customer_ledger_history(id INTEGER PRIMARY KEY CHECK(id=1))')
        self.conn.execute('INSERT OR IGNORE INTO customer_ledger_history VALUES(1)')


    def summary(self, chat_id: int) -> LedgerSummary:
        current_rate, fee_percent = self.get_settings(chat_id)
        rows = self.conn.execute(
            """
            SELECT kind, net_amount, amount, currency, rate, fee_percent, fee_amount, payable_amount, payable_usdt
            FROM entries
            WHERE chat_id = ? AND voided_at IS NULL
            """,
            (chat_id,),
        ).fetchall()
        income = Decimal("0")
        payable_amount = Decimal("0")
        income_usdt = Decimal("0")
        payout_usdt = Decimal("0")
        fees = Decimal("0")
        for row in rows:
            if row["kind"] == "income":
                income += Decimal(row["payable_amount"] if row['currency'] == 'U' else row["amount"])
                fees += Decimal(row["fee_amount"])
                payable_amount += Decimal(row["payable_amount"])
                income_usdt += Decimal(row["payable_usdt"])
            else:
                payout_usdt += Decimal(row["net_amount"])
        return LedgerSummary(
            income=money(income),
            payout=money(payout_usdt),
            fees=money(fees),
            payable_amount=money(payable_amount),
            balance=money(income_usdt - payout_usdt),
            income_usdt=money(income_usdt),
            payout_usdt=money(payout_usdt),
            balance_usdt=money(income_usdt - payout_usdt),
            count=len(rows),
            rate=current_rate,
            fee_percent=fee_percent,
        )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    @staticmethod
    def _entry_from_row(row: sqlite3.Row) -> LedgerEntry:
        return LedgerEntry(
            id=row["id"],
            chat_id=row["chat_id"],
            kind=row["kind"],
            amount=money(row["amount"]),
            currency=row["currency"],
            rate=rate(row["rate"]),
            fee_percent=rate(row["fee_percent"]),
            fee_amount=money(row["fee_amount"]),
            payable_amount=money(row["payable_amount"]),
            payable_usdt=money(row["payable_usdt"]),
            net_amount=money(row["net_amount"]),
            note=row["note"],
            operator_id=row["operator_id"],
            operator_name=row["operator_name"],
            source_message_id=row["source_message_id"],
            accounting_date=row["accounting_date"] or "",
            created_at=row["created_at"],
            voided_at=row["voided_at"],
        )
