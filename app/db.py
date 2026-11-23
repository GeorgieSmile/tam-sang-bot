"""SQLite storage (WAL mode) for shop state, overrides, customers, conversations, and orders."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from app.menu import Fulfillment, Menu, MenuError, OverrideMap

BKK = ZoneInfo("Asia/Bangkok")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS shop_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    override_date TEXT,
    open_override TEXT,
    bot_enabled INTEGER NOT NULL DEFAULT 1,
    busy_date TEXT,
    busy_mode INTEGER NOT NULL DEFAULT 0,
    wait_minutes INTEGER,
    notify_owner_on_order INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS menu_overrides (
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    is_available INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (kind, key)
);

CREATE TABLE IF NOT EXISTS customers (
    line_user_id TEXT PRIMARY KEY,
    display_name TEXT,
    last_address TEXT,
    last_location_url TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    line_user_id TEXT NOT NULL,
    shop_date TEXT NOT NULL,
    messages_json TEXT NOT NULL,
    draft_json TEXT NOT NULL,
    draft_revision INTEGER NOT NULL DEFAULT 0,
    active_card_id TEXT,
    active_card_json TEXT,
    handed_over_until TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE (line_user_id, shop_date)
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shop_date TEXT NOT NULL,
    queue_number INTEGER NOT NULL,
    line_user_id TEXT NOT NULL,
    card_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    cancel_reason TEXT,
    fulfillment TEXT NOT NULL,
    address TEXT,
    location_url TEXT,
    requested_time TEXT,
    items_json TEXT NOT NULL,
    food_total INTEGER NOT NULL,
    delivery_fee INTEGER NOT NULL,
    total INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (shop_date, queue_number)
);

CREATE TABLE IF NOT EXISTS line_events (
    event_id TEXT PRIMARY KEY,
    received_at TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class ShopStatus:
    shop_date: str
    is_open: bool
    scheduled_open: bool
    open_override: str | None
    bot_enabled: bool
    busy_mode: bool
    wait_minutes: int | None
    notify_owner_on_order: bool
    hours_text: str
    delivery_rule_text: str
    sold_out_ingredients: list[str]
    sold_out_dishes: list[str]


@dataclass(frozen=True)
class Customer:
    line_user_id: str
    display_name: str | None
    last_address: str | None
    last_location_url: str | None


@dataclass(frozen=True)
class Conversation:
    id: int
    line_user_id: str
    shop_date: str
    messages: list[dict[str, Any]]
    draft: dict[str, Any]
    draft_revision: int
    active_card_id: str | None
    active_card: dict[str, Any] | None
    handed_over_until: str | None


@dataclass(frozen=True)
class Order:
    id: int
    shop_date: str
    queue_number: int
    line_user_id: str
    card_id: str
    status: str
    cancel_reason: str | None
    fulfillment: Fulfillment
    address: str | None
    location_url: str | None
    requested_time: str | None
    items: list[dict[str, Any]]
    food_total: int
    delivery_fee: int
    total: int
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "shop_date": self.shop_date,
            "queue_number": self.queue_number,
            "line_user_id": self.line_user_id,
            "card_id": self.card_id,
            "status": self.status,
            "cancel_reason": self.cancel_reason,
            "fulfillment": self.fulfillment,
            "address": self.address,
            "location_url": self.location_url,
            "requested_time": self.requested_time,
            "items": self.items,
            "food_total": self.food_total,
            "delivery_fee": self.delivery_fee,
            "total": self.total,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class ConfirmResult:
    ok: bool
    order: Order | None = None
    already_confirmed: bool = False
    error: str | None = None


def _default_clock() -> datetime:
    return datetime.now(BKK)


def _row_to_order(row: sqlite3.Row) -> Order:
    fulfillment_raw = str(row["fulfillment"])
    fulfillment: Fulfillment = "delivery" if fulfillment_raw == "delivery" else "pickup"
    return Order(
        id=int(row["id"]),
        shop_date=str(row["shop_date"]),
        queue_number=int(row["queue_number"]),
        line_user_id=str(row["line_user_id"]),
        card_id=str(row["card_id"]),
        status=str(row["status"]),
        cancel_reason=row["cancel_reason"],
        fulfillment=fulfillment,
        address=row["address"],
        location_url=row["location_url"],
        requested_time=row["requested_time"],
        items=json.loads(str(row["items_json"])),
        food_total=int(row["food_total"]),
        delivery_fee=int(row["delivery_fee"]),
        total=int(row["total"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _resolve_open_status(
    state_row: sqlite3.Row, today: str, now: datetime, menu: Menu
) -> tuple[bool, bool, str | None]:
    scheduled_open = menu.is_scheduled_open(now)
    open_override = (
        str(state_row["open_override"])
        if state_row["override_date"] == today and state_row["open_override"] in ("open", "closed")
        else None
    )
    if open_override == "open":
        return True, scheduled_open, open_override
    if open_override == "closed":
        return False, scheduled_open, open_override
    return scheduled_open, scheduled_open, None


class ShopDB:
    def __init__(
        self,
        db_path: Path,
        clock: Callable[[], datetime] = _default_clock,
    ) -> None:
        self.db_path = db_path
        self.clock = clock
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @contextmanager
    def _tx(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield conn
            if conn.in_transaction:
                conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def now_bkk(self) -> datetime:
        return self.clock().astimezone(BKK)

    def today_str(self) -> str:
        return self.now_bkk().strftime("%Y-%m-%d")

    def now_iso(self) -> str:
        return self.now_bkk().isoformat(timespec="seconds")

    def end_of_today_iso(self) -> str:
        local = self.now_bkk().replace(hour=23, minute=59, second=59, microsecond=0)
        return local.isoformat(timespec="seconds")

    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(SCHEMA_SQL)
        finally:
            conn.close()
        with self._tx(immediate=True) as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO shop_state (id, updated_at)
                VALUES (1, ?)
                """,
                (self.now_iso(),),
            )

    # --- Shop state & overrides ---

    def get_overrides(self) -> OverrideMap:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT kind, key, is_available FROM menu_overrides").fetchall()
            return {(str(r["kind"]), str(r["key"])): bool(r["is_available"]) for r in rows}
        finally:
            conn.close()

    def set_override(
        self, kind: Literal["dish", "ingredient"], key: str, is_available: bool
    ) -> None:
        with self._tx(immediate=True) as conn:
            conn.execute(
                """
                INSERT INTO menu_overrides (kind, key, is_available, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(kind, key) DO UPDATE SET
                    is_available = excluded.is_available,
                    updated_at = excluded.updated_at
                """,
                (kind, key, int(is_available), self.now_iso()),
            )

    def clear_all_overrides(self) -> None:
        with self._tx(immediate=True) as conn:
            conn.execute("DELETE FROM menu_overrides")

    def get_shop_status(self, menu: Menu) -> ShopStatus:
        now = self.now_bkk()
        today = now.strftime("%Y-%m-%d")
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM shop_state WHERE id = 1").fetchone()
            overrides_rows = conn.execute(
                "SELECT kind, key, is_available FROM menu_overrides"
            ).fetchall()
        finally:
            conn.close()

        overrides: OverrideMap = {
            (str(r["kind"]), str(r["key"])): bool(r["is_available"]) for r in overrides_rows
        }
        is_open, scheduled_open, open_override = _resolve_open_status(row, today, now, menu)

        busy_active = row["busy_date"] == today and bool(row["busy_mode"])
        wait_minutes = int(row["wait_minutes"]) if busy_active and row["wait_minutes"] else None
        sold = menu.sold_out_summary(overrides)
        return ShopStatus(
            shop_date=today,
            is_open=is_open,
            scheduled_open=scheduled_open,
            open_override=open_override,
            bot_enabled=bool(row["bot_enabled"]),
            busy_mode=busy_active,
            wait_minutes=wait_minutes,
            notify_owner_on_order=bool(row["notify_owner_on_order"]),
            hours_text=menu.shop.hours_text_th(),
            delivery_rule_text=menu.shop.delivery.rule_text_th(),
            sold_out_ingredients=sold["ingredients"],
            sold_out_dishes=sold["dishes"],
        )

    def set_open_override(self, override: Literal["open", "closed"] | None) -> None:
        today = self.today_str() if override is not None else None
        with self._tx(immediate=True) as conn:
            conn.execute(
                """
                UPDATE shop_state
                SET override_date = ?, open_override = ?, updated_at = ?
                WHERE id = 1
                """,
                (today, override, self.now_iso()),
            )

    def set_busy_mode(self, busy: bool, wait_minutes: int | None = None) -> None:
        today = self.today_str() if busy else None
        mins = (wait_minutes if wait_minutes and wait_minutes > 0 else 20) if busy else None
        with self._tx(immediate=True) as conn:
            conn.execute(
                """
                UPDATE shop_state
                SET busy_date = ?, busy_mode = ?, wait_minutes = ?, updated_at = ?
                WHERE id = 1
                """,
                (today, int(busy), mins, self.now_iso()),
            )

    def set_bot_enabled(self, enabled: bool) -> None:
        with self._tx(immediate=True) as conn:
            conn.execute(
                "UPDATE shop_state SET bot_enabled = ?, updated_at = ? WHERE id = 1",
                (int(enabled), self.now_iso()),
            )

    def set_notify_owner(self, enabled: bool) -> None:
        with self._tx(immediate=True) as conn:
            conn.execute(
                "UPDATE shop_state SET notify_owner_on_order = ?, updated_at = ? WHERE id = 1",
                (int(enabled), self.now_iso()),
            )

    # --- Customers & Conversations ---

    def get_customer(self, line_user_id: str) -> Customer | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM customers WHERE line_user_id = ?", (line_user_id,)
            ).fetchone()
            if row is None:
                return None
            return Customer(
                line_user_id=str(row["line_user_id"]),
                display_name=row["display_name"],
                last_address=row["last_address"],
                last_location_url=row["last_location_url"],
            )
        finally:
            conn.close()

    def upsert_customer(
        self,
        line_user_id: str,
        *,
        display_name: str | None = None,
        last_address: str | None = None,
        last_location_url: str | None = None,
        update_address: bool = False,
    ) -> None:
        now = self.now_iso()
        with self._tx(immediate=True) as conn:
            existing = conn.execute(
                "SELECT * FROM customers WHERE line_user_id = ?", (line_user_id,)
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO customers (
                        line_user_id, display_name, last_address, last_location_url, updated_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (line_user_id, display_name, last_address, last_location_url, now),
                )
            else:
                new_name = display_name if display_name is not None else existing["display_name"]
                new_addr = last_address if update_address else existing["last_address"]
                new_loc = last_location_url if update_address else existing["last_location_url"]
                conn.execute(
                    """
                    UPDATE customers
                    SET display_name = ?, last_address = ?, last_location_url = ?, updated_at = ?
                    WHERE line_user_id = ?
                    """,
                    (new_name, new_addr, new_loc, now, line_user_id),
                )

    def get_or_create_conversation(self, line_user_id: str) -> Conversation:
        today = self.today_str()
        now = self.now_iso()
        with self._tx(immediate=True) as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO conversations (
                    line_user_id, shop_date, messages_json, draft_json, draft_revision, updated_at
                ) VALUES (?, ?, '[]', '{}', 0, ?)
                """,
                (line_user_id, today, now),
            )
            row = conn.execute(
                "SELECT * FROM conversations WHERE line_user_id = ? AND shop_date = ?",
                (line_user_id, today),
            ).fetchone()
        return Conversation(
            id=int(row["id"]),
            line_user_id=str(row["line_user_id"]),
            shop_date=str(row["shop_date"]),
            messages=json.loads(str(row["messages_json"])),
            draft=json.loads(str(row["draft_json"])),
            draft_revision=int(row["draft_revision"]),
            active_card_id=row["active_card_id"],
            active_card=json.loads(str(row["active_card_json"]))
            if row["active_card_json"]
            else None,
            handed_over_until=row["handed_over_until"],
        )

    def is_handed_over(self, conv: Conversation) -> bool:
        if not conv.handed_over_until:
            return False
        try:
            until = datetime.fromisoformat(conv.handed_over_until)
        except ValueError:
            return False
        return self.now_bkk() <= until

    def save_conversation_messages(self, conv_id: int, messages: list[dict[str, Any]]) -> None:
        with self._tx(immediate=True) as conn:
            conn.execute(
                "UPDATE conversations SET messages_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(messages, ensure_ascii=False), self.now_iso(), conv_id),
            )

    def append_conversation_event(
        self,
        line_user_id: str,
        user_note: str,
        assistant_reply: str,
        *,
        compact_for_confirmed_order: bool = False,
    ) -> None:
        """Keep conversation history in sync with non-LLM events (confirm, cancel, pin, image)."""
        conv = self.get_or_create_conversation(line_user_id)
        if compact_for_confirmed_order:
            # Drop stale cart-building turns; keep only earlier confirmed-order summaries today
            kept: list[dict[str, Any]] = []
            msgs = conv.messages
            i = 0
            while i < len(msgs):
                m = msgs[i]
                if (
                    m.get("role") == "user"
                    and isinstance(m.get("content"), str)
                    and m["content"].startswith("[ลูกค้ากดยืนยันออเดอร์")
                ):
                    kept.append(m)
                    if i + 1 < len(msgs) and msgs[i + 1].get("role") == "assistant":
                        kept.append(msgs[i + 1])
                        i += 2
                        continue
                i += 1
            base = kept
        else:
            base = list(conv.messages)
        base.append({"role": "user", "content": user_note})
        base.append({"role": "assistant", "content": assistant_reply})
        self.save_conversation_messages(conv.id, base)

    def save_draft(self, conv_id: int, draft: dict[str, Any]) -> int:
        with self._tx(immediate=True) as conn:
            conn.execute(
                """
                UPDATE conversations
                SET draft_json = ?,
                    draft_revision = draft_revision + 1,
                    active_card_id = NULL,
                    active_card_json = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (json.dumps(draft, ensure_ascii=False), self.now_iso(), conv_id),
            )
            row = conn.execute(
                "SELECT draft_revision FROM conversations WHERE id = ?", (conv_id,)
            ).fetchone()
            return int(row["draft_revision"])

    def set_active_card(self, conv_id: int, card_id: str, card_snapshot: dict[str, Any]) -> None:
        with self._tx(immediate=True) as conn:
            conn.execute(
                """
                UPDATE conversations
                SET active_card_id = ?, active_card_json = ?, updated_at = ?
                WHERE id = ?
                """,
                (card_id, json.dumps(card_snapshot, ensure_ascii=False), self.now_iso(), conv_id),
            )

    def cancel_active_card(self, line_user_id: str, card_id: str) -> bool:
        today = self.today_str()
        with self._tx(immediate=True) as conn:
            cur = conn.execute(
                """
                UPDATE conversations
                SET active_card_id = NULL, active_card_json = NULL, updated_at = ?
                WHERE line_user_id = ? AND shop_date = ? AND active_card_id = ?
                """,
                (self.now_iso(), line_user_id, today, card_id),
            )
            return cur.rowcount > 0

    def set_handed_over(self, conv_id: int, until_iso: str | None = None) -> None:
        until = until_iso or self.end_of_today_iso()
        with self._tx(immediate=True) as conn:
            conn.execute(
                "UPDATE conversations SET handed_over_until = ?, updated_at = ? WHERE id = ?",
                (until, self.now_iso(), conv_id),
            )

    def clear_handed_over(self, conv_id: int) -> None:
        with self._tx(immediate=True) as conn:
            conn.execute(
                "UPDATE conversations SET handed_over_until = NULL, updated_at = ? WHERE id = ?",
                (self.now_iso(), conv_id),
            )

    # --- Orders ---

    def confirm_card(self, line_user_id: str, card_id: str, menu: Menu) -> ConfirmResult:
        """Atomically verify card snapshot, re-check availability/price, and queue order."""
        now = self.now_bkk()
        today = now.strftime("%Y-%m-%d")
        now_iso = now.isoformat(timespec="seconds")

        with self._tx(immediate=True) as conn:
            existing = conn.execute(
                "SELECT * FROM orders WHERE card_id = ? AND line_user_id = ?",
                (card_id, line_user_id),
            ).fetchone()
            if existing is not None:
                return ConfirmResult(ok=True, order=_row_to_order(existing), already_confirmed=True)

            conv_row = conn.execute(
                "SELECT * FROM conversations WHERE line_user_id = ? AND shop_date = ?",
                (line_user_id, today),
            ).fetchone()
            if conv_row is None or conv_row["active_card_id"] != card_id:
                return ConfirmResult(
                    ok=False,
                    error="การ์ดออเดอร์นี้หมดอายุหรือถูกเปลี่ยนไปแล้วค่ะ กรุณาสั่งใหม่นะคะ",
                )

            snapshot: dict[str, Any] = json.loads(str(conv_row["active_card_json"]))
            if int(conv_row["draft_revision"]) != int(snapshot["draft_revision"]):
                return ConfirmResult(
                    ok=False,
                    error="รายการในตะกร้ามีการเปลี่ยนแปลงค่ะ กรุณาดูสรุปออเดอร์ใหม่นะคะ",
                )

            # Re-check shop open status inside the same transaction
            state_row = conn.execute("SELECT * FROM shop_state WHERE id = 1").fetchone()
            is_open, _, _ = _resolve_open_status(state_row, today, now, menu)
            if not is_open:
                return ConfirmResult(
                    ok=False,
                    error=f"ตอนนี้ร้านปิดรับออเดอร์แล้วค่ะ (ปกติเปิด {menu.shop.hours_text_th()})",
                )

            ov_rows = conn.execute("SELECT kind, key, is_available FROM menu_overrides").fetchall()
            overrides: OverrideMap = {
                (str(r["kind"]), str(r["key"])): bool(r["is_available"]) for r in ov_rows
            }

            fulfillment_raw = str(snapshot["fulfillment"])
            fulfillment: Fulfillment = "delivery" if fulfillment_raw == "delivery" else "pickup"
            address = snapshot.get("address")
            location_url = snapshot.get("location_url")
            if fulfillment == "delivery" and not (address and str(address).strip()):
                return ConfirmResult(ok=False, error="ยังไม่ได้ระบุที่อยู่จัดส่งค่ะ")

            try:
                priced = menu.price_cart(snapshot["raw_items"], fulfillment, overrides)
            except MenuError as exc:
                return ConfirmResult(ok=False, error=str(exc))

            if priced.total != int(snapshot["total"]):
                return ConfirmResult(
                    ok=False,
                    error="ราคาเมนูมีการเปลี่ยนแปลงค่ะ กรุณาขอสรุปออเดอร์ใหม่นะคะ",
                )

            q_row = conn.execute(
                "SELECT COALESCE(MAX(queue_number), 0) + 1 AS next_q "
                "FROM orders WHERE shop_date = ?",
                (today,),
            ).fetchone()
            queue_number = int(q_row["next_q"])

            items_payload = [line.to_dict() for line in priced.lines]
            cur = conn.execute(
                """
                INSERT INTO orders (
                    shop_date, queue_number, line_user_id, card_id, status,
                    fulfillment, address, location_url, requested_time,
                    items_json, food_total, delivery_fee, total, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    today,
                    queue_number,
                    line_user_id,
                    card_id,
                    fulfillment,
                    str(address).strip() if address else None,
                    str(location_url).strip() if location_url else None,
                    snapshot.get("requested_time"),
                    json.dumps(items_payload, ensure_ascii=False),
                    priced.food_total,
                    priced.delivery_fee,
                    priced.total,
                    now_iso,
                    now_iso,
                ),
            )
            order_id = int(cur.lastrowid or 0)

            # Clear draft and active card
            conn.execute(
                """
                UPDATE conversations
                SET draft_json = '{}',
                    draft_revision = draft_revision + 1,
                    active_card_id = NULL,
                    active_card_json = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (now_iso, int(conv_row["id"])),
            )

            # Save returning customer's delivery address / location pin
            if fulfillment == "delivery" and address:
                clean_addr = str(address).strip()
                clean_loc = str(location_url).strip() if location_url else None
                conn.execute(
                    """
                    INSERT INTO customers (
                        line_user_id, last_address, last_location_url, updated_at
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(line_user_id) DO UPDATE SET
                        last_address = excluded.last_address,
                        last_location_url = excluded.last_location_url,
                        updated_at = excluded.updated_at
                    """,
                    (line_user_id, clean_addr, clean_loc, now_iso),
                )

            order_row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            return ConfirmResult(ok=True, order=_row_to_order(order_row), already_confirmed=False)

    def get_customer_orders_today(self, line_user_id: str) -> list[Order]:
        today = self.today_str()
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT * FROM orders
                WHERE line_user_id = ? AND shop_date = ?
                ORDER BY id DESC
                """,
                (line_user_id, today),
            ).fetchall()
            return [_row_to_order(r) for r in rows]
        finally:
            conn.close()

    def cancel_customer_order(self, line_user_id: str, order_id: int) -> Order | None:
        now_iso = self.now_iso()
        with self._tx(immediate=True) as conn:
            cur = conn.execute(
                """
                UPDATE orders
                SET status = 'cancelled', cancel_reason = 'ลูกค้ายกเลิกเอง', updated_at = ?
                WHERE id = ? AND line_user_id = ? AND status = 'queued'
                """,
                (now_iso, order_id, line_user_id),
            )
            if cur.rowcount == 0:
                return None
            row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            return _row_to_order(row)

    def get_kitchen_orders_today(self) -> dict[str, Any]:
        today = self.today_str()
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM orders WHERE shop_date = ? ORDER BY queue_number ASC",
                (today,),
            ).fetchall()
        finally:
            conn.close()

        orders = [_row_to_order(r) for r in rows]
        queued = [o for o in orders if o.status == "queued"]
        finished = [o for o in orders if o.status in ("done", "cancelled")]
        finished.reverse()
        done_orders = [o for o in orders if o.status == "done"]
        cancelled_orders = [o for o in orders if o.status == "cancelled"]
        return {
            "shop_date": today,
            "queued": [o.to_dict() for o in queued],
            "finished": [o.to_dict() for o in finished],
            "stats": {
                "queued_count": len(queued),
                "done_count": len(done_orders),
                "cancelled_count": len(cancelled_orders),
                "done_total": sum(o.total for o in done_orders),
            },
        }

    def mark_order_done(self, order_id: int) -> Order | None:
        now_iso = self.now_iso()
        with self._tx(immediate=True) as conn:
            cur = conn.execute(
                """
                UPDATE orders
                SET status = 'done', updated_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (now_iso, order_id),
            )
            if cur.rowcount == 0:
                return None
            row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            return _row_to_order(row)

    def cancel_order_by_owner(self, order_id: int, reason: str) -> Order | None:
        clean_reason = reason.strip() or "ร้านยกเลิกออเดอร์"
        now_iso = self.now_iso()
        with self._tx(immediate=True) as conn:
            cur = conn.execute(
                """
                UPDATE orders
                SET status = 'cancelled', cancel_reason = ?, updated_at = ?
                WHERE id = ? AND status = 'queued'
                """,
                (clean_reason, now_iso, order_id),
            )
            if cur.rowcount == 0:
                return None
            row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            return _row_to_order(row)

    # --- LINE webhook deduplication ---

    def record_line_event(self, event_id: str) -> bool:
        """Record a webhookEventId; returns True if new, False if duplicate."""
        with self._tx(immediate=True) as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO line_events (event_id, received_at) VALUES (?, ?)",
                (event_id, self.now_iso()),
            )
            return cur.rowcount > 0
