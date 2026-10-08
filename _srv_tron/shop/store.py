from __future__ import annotations

import json
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path


SERVICES = {"energy": "⚡ 充能量", "smart": "🔥 智能笔数", "real": "✅ 真实笔数",
            "premium": "🎁 开通会员", "usdt_trx": "USDT → TRX", "trx_usdt": "TRX → USDT"}
ACTIONS = {"home": "🏠 返回首页", **SERVICES, "orders": "📊 我的记录", "remain": "🔢 剩余笔数",
           "query": "💰 余额查询", "watch": "🔔 地址监听", "unwatch": "取消监听",
           "contact": "☎️ 联系客服"}
MENU = [["energy", "orders"], ["smart", "real"], ["remain", "query"],
        ["usdt_trx", "trx_usdt"], ["premium", "watch"], ["contact"]]
STATES = {"pending": "待付款", "paid": "已收款", "sending": "交付核对中", "processing": "上游处理中",
          "done": "已完成", "review": "待人工核对", "expired": "已过期", "cancelled": "已取消", "refunded": "商户已确认退款"}


def units(value: str) -> int:
    try:
        number = Decimal(value)
        if not number.is_finite() or number <= 0 or number > Decimal("1000000"):
            raise ValueError
        raw = number * 1000000
        if raw != raw.to_integral_value():
            raise ValueError
        return int(raw)
    except (InvalidOperation, ValueError):
        raise ValueError("金额必须大于 0，最多 6 位小数，不超过 100 万。") from None


def amount(value: int) -> str:
    return format(Decimal(value) / 1000000, "f").rstrip("0").rstrip(".") if value % 1000000 else str(value // 1000000)


def parse_prices(service: str, text: str) -> list[dict]:
    result = []
    for line in text.splitlines():
        fields = line.split()
        try:
            if service == "energy":
                qty, hours, price, coin = fields
                hours = 0.25 if hours.lower() == "15m" else int(hours)
                if hours not in (0.25, 1, 24, 72, 168, 336, 720):
                    raise ValueError
            else:
                qty, price, coin = fields
                hours = 0
            qty = int(qty)
            if not 1 <= qty <= 10000000 or coin.upper() not in ("TRX", "USDT"):
                raise ValueError
            if service == "energy" and qty < 32000:
                raise ValueError
            if service in ("smart", "real") and qty < 2:
                raise ValueError
            if service == "premium" and qty not in (3, 6, 12):
                raise ValueError
            if service in ("usdt_trx", "trx_usdt") and coin.upper() != ("USDT" if service == "usdt_trx" else "TRX"):
                raise ValueError
            result.append({"quantity": qty, "hours": hours, "amount": units(price), "coin": coin.upper()})
        except (ValueError, TypeError):
            raise ValueError("套餐格式不正确，请按提示填写；价格最多 6 位小数。") from None
    if not 1 <= len(result) <= 12:
        raise ValueError("每项服务设置 1～12 个套餐。")
    return result


class ShopStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS orders(
                    id TEXT PRIMARY KEY, buyer INTEGER NOT NULL, service TEXT NOT NULL,
                    details TEXT NOT NULL, coin TEXT NOT NULL, amount INTEGER NOT NULL,
                    destination TEXT NOT NULL, receiver TEXT NOT NULL,
                    created INTEGER NOT NULL, expires INTEGER NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending', txid TEXT UNIQUE,
                    provider_id TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '');
                CREATE UNIQUE INDEX IF NOT EXISTS pending_amount ON orders(receiver,coin,amount) WHERE state='pending';
                CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY, payload TEXT NOT NULL, order_id TEXT);
                CREATE TABLE IF NOT EXISTS cursors(address TEXT, coin TEXT, timestamp INTEGER, PRIMARY KEY(address,coin));
                CREATE TABLE IF NOT EXISTS watches(buyer INTEGER, address TEXT, since INTEGER, PRIMARY KEY(buyer,address));
                CREATE TABLE IF NOT EXISTS alerts(buyer INTEGER, txid TEXT, PRIMARY KEY(buyer,txid));
                CREATE TABLE IF NOT EXISTS watch_events(
                    buyer INTEGER, address TEXT, txid TEXT, timestamp INTEGER, coin TEXT,
                    direction TEXT, amount INTEGER, notified INTEGER DEFAULT 0,
                    PRIMARY KEY(buyer,address,txid));
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(watches)")}
            for name, definition in {
                "note": "TEXT NOT NULL DEFAULT ''", "coins": "TEXT NOT NULL DEFAULT 'USDT'",
                "direction": "TEXT NOT NULL DEFAULT 'both'", "minimum": "INTEGER NOT NULL DEFAULT 0",
                "detailed": "INTEGER NOT NULL DEFAULT 0", "added": "INTEGER NOT NULL DEFAULT 0",
                "synced": "INTEGER NOT NULL DEFAULT 0", "error": "TEXT NOT NULL DEFAULT ''",
                "revision": "TEXT NOT NULL DEFAULT ''",
            }.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE watches ADD COLUMN {name} {definition}")
            db.execute("UPDATE watches SET added=since,revision=lower(hex(randomblob(8))) WHERE added=0")
        os.chmod(path, 0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, key: str, default=None):
        with self.db() as db:
            row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key: str, value):
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, json.dumps(value, ensure_ascii=False)))

    def payment_address(self, address: str):
        now = int(time.time() * 1000)
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO settings VALUES('address',?)", (json.dumps(address),))
            db.execute("INSERT OR IGNORE INTO settings VALUES(?,?)", ("scan_start:" + address, json.dumps(now)))
            for coin in ("TRX", "USDT"):
                db.execute("INSERT OR IGNORE INTO cursors VALUES(?,?,?)", (address, coin, now))

    def create(self, buyer: int, service: str, tier: dict, destination: str) -> dict:
        now = int(time.time() * 1000)
        receiver = self.get("address", "")
        if not receiver:
            raise ValueError("商户尚未设置收款地址。")
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            
            busy = {row[0] for row in db.execute("SELECT amount FROM orders WHERE receiver=? AND coin=?",
                                                (receiver, tier["coin"]))}
            payable = next((tier["amount"] + i for i in range(1, 10001) if tier["amount"] + i not in busy), None)
            if payable is None:
                raise ValueError("当前价格的收款识别金额已用完，请联系商户调整价格。")
            if db.execute("SELECT COUNT(*) FROM orders WHERE buyer=? AND state='pending' AND expires>?", (buyer, now)).fetchone()[0] >= 3:
                raise ValueError("最多同时保留 3 个待付款订单，请先处理已有订单。")
            oid = secrets.token_hex(10)
            details = {**tier, "provider": self.get("provider:" + service, "apitrx")}
            db.execute("INSERT INTO orders(id,buyer,service,details,coin,amount,destination,receiver,created,expires) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (oid, buyer, service, json.dumps(details), tier["coin"], payable, destination, receiver, now, now + 1800000))
        return self.order(oid)

    def order(self, oid: str):
        with self.db() as db:
            row = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        return dict(row) if row else None

    def orders(self, buyer: int | None = None, states: tuple = ()) -> list[dict]:
        where, args = [], []
        if buyer is not None:
            where.append("buyer=?")
            args.append(buyer)
        if states:
            where.append("state IN (" + ",".join("?" for _ in states) + ")")
            args.extend(states)
        with self.db() as db:
            rows = db.execute("SELECT * FROM orders" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY created DESC LIMIT 50", args)
            return [dict(row) for row in rows]

    def transition(self, oid: str, previous: str, state: str, *, note="", provider_id="") -> bool:
        with self.db() as db:
            return bool(db.execute("UPDATE orders SET state=?,note=?,provider_id=? WHERE id=? AND state=?",
                                   (state, note, provider_id, oid, previous)).rowcount)

    def receive(self, tx: dict) -> str | None:
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM receipts WHERE id=?", (tx["id"],)).fetchone():
                return None
            row = db.execute("SELECT * FROM orders WHERE receiver=? AND coin=? AND amount=? AND created<=? ORDER BY created DESC LIMIT 1",
                             (tx["to"], tx["coin"], tx["amount"], tx["timestamp"])).fetchone()
            oid = row["id"] if row else None
            db.execute("INSERT INTO receipts VALUES(?,?,?)", (tx["id"], json.dumps(tx), oid))
            if row:
                state = "paid" if row["state"] in ("pending", "expired") and tx["timestamp"] <= row["expires"] else "review"
                if row["txid"]:
                    return None
                db.execute("UPDATE orders SET state=?,txid=?,note=? WHERE id=?", (state, tx["id"], "" if state == "paid" else "超时或取消后付款，请核对", oid))
            return oid

    def watch(self, buyer: int, address: str, enabled: bool):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if enabled:
                if db.execute("SELECT 1 FROM watches WHERE buyer=? AND address=?", (buyer, address)).fetchone():
                    raise ValueError("此地址已在你的地址簿中，不会重复添加。")
                if db.execute("SELECT COUNT(*) FROM watches WHERE buyer=?", (buyer,)).fetchone()[0] >= 5:
                    raise ValueError("每人最多监听 5 个地址。")
                now = int(time.time() * 1000)
                db.execute("INSERT INTO watches(buyer,address,since,added,coins,revision) VALUES(?,?,?,?,?,?)",
                           (buyer, address, now, now, "USDT,TRX", secrets.token_hex(8)))
            else:
                db.execute("DELETE FROM watches WHERE buyer=? AND address=?", (buyer, address))
                db.execute("DELETE FROM watch_events WHERE buyer=? AND address=?", (buyer, address))

    def watches(self, buyer: int | None = None):
        with self.db() as db:
            return [dict(row) for row in db.execute("SELECT * FROM watches" +
                    (" WHERE buyer=?" if buyer is not None else "") + " ORDER BY added DESC,address",
                    (buyer,) if buyer is not None else ())]

    def watched(self, buyer, address):
        return next((row for row in self.watches(buyer) if row["address"] == address), None)
