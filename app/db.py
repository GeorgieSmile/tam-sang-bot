"""SQLite storage (WAL mode) for shop state, overrides, customers, conversations, and orders."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from app.menu import Fulfillment, Menu, MenuError, OverrideMap
from app.slips import match_slip

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
    updated_at TEXT NOT NULL,
    notify_owner_on_inbox INTEGER NOT NULL DEFAULT 1
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
    turn_offset INTEGER NOT NULL DEFAULT 0,
    quiet_reason TEXT,
    pay_cash INTEGER NOT NULL DEFAULT 0,
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
    payment_status TEXT NOT NULL DEFAULT 'unpaid',
    paid_at TEXT,
    pay_cash INTEGER NOT NULL DEFAULT 0,
    UNIQUE (shop_date, queue_number)
);

CREATE TABLE IF NOT EXISTS line_events (
    event_id TEXT PRIMARY KEY,
    received_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS handovers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    line_user_id TEXT NOT NULL,
    shop_date TEXT NOT NULL,
    reasons_json TEXT NOT NULL,
    details_json TEXT NOT NULL,
    recent_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_customer_message_at TEXT,
    unseen_count INTEGER NOT NULL DEFAULT 0,
    resolved_at TEXT
);

CREATE INDEX IF NOT EXISTS handovers_open ON handovers (line_user_id, resolved_at);

CREATE TABLE IF NOT EXISTS slip_checks (
    trans_ref TEXT PRIMARY KEY,
    line_user_id TEXT NOT NULL,
    amount_satang INTEGER,
    transfer_at TEXT,
    result TEXT NOT NULL,
    order_ids_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);
"""

QUIET_MINUTES = 15


# History events that survive compaction on Confirm (they describe orders, not the cart)
KEPT_EVENT_PREFIXES = ("[ลูกค้ากดยืนยันออเดอร์", "[ร้านแจ้งว่าออเดอร์", "[ร้านยกเลิกออเดอร์")


def customer_turn_count(messages: list[dict[str, Any]]) -> int:
    """Messages the customer actually typed. System notes saved as `user` entries (confirm,
    slip, pin, owner pushes) start with `[` and never count toward the daily limit."""
    return sum(
        1
        for m in messages
        if m.get("role") == "user" and not str(m.get("content") or "").startswith("[")
    )


# Payment status values (SPEC §3.1.2). Only owner-set statuses can be undone.
PAID_STATUSES = ("slip", "cash", "transfer_manual")
OWNER_PAID_STATUSES = ("cash", "transfer_manual")


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
    notify_owner_on_inbox: bool = True


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
    turn_offset: int = 0
    quiet_reason: str | None = None
    # The customer said they'll pay cash today: no QR in receipts or payment info
    pay_cash: bool = False


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
    payment_status: str = "unpaid"
    paid_at: str | None = None
    pay_cash: bool = False

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
            "payment_status": self.payment_status,
            "paid_at": self.paid_at,
            "pay_cash": self.pay_cash,
        }


@dataclass(frozen=True)
class UnpaidBalance:
    total: int
    orders: list[Order]


@dataclass(frozen=True)
class SlipPayResult:
    """`result` is `matched` or a `slip_*` hand-over reason."""

    result: str
    paid_orders: list[Order]
    confirmed_order: Order | None
    owed_satang: int


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
        payment_status=str(row["payment_status"]),
        paid_at=row["paid_at"],
        pay_cash=bool(row["pay_cash"]),
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
        self._migrate_v2_payment_columns()
        self._migrate_v2_1_columns()
        with self._tx(immediate=True) as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO shop_state (id, updated_at)
                VALUES (1, ?)
                """,
                (self.now_iso(),),
            )

    def _migrate_v2_payment_columns(self) -> None:
        """Additive v1 -> v2 migration: new columns, and mark every v1 order `legacy`."""
        with self._tx(immediate=True) as conn:
            conv_cols = {
                str(r["name"]) for r in conn.execute("PRAGMA table_info(conversations)").fetchall()
            }
            if "turn_offset" not in conv_cols:
                conn.execute(
                    "ALTER TABLE conversations ADD COLUMN turn_offset INTEGER NOT NULL DEFAULT 0"
                )
                # A v1 hand-over still running has no inbox card; end it so the customer isn't
                # left in silence with nothing for the owner to resolve. The bot-came-back note
                # is still given on the next LLM call.
                past = (self.now_bkk() - timedelta(seconds=1)).isoformat(timespec="seconds")
                conn.execute(
                    "UPDATE conversations SET handed_over_until = ? WHERE handed_over_until > ?",
                    (past, past),
                )
            if "quiet_reason" not in conv_cols:
                conn.execute("ALTER TABLE conversations ADD COLUMN quiet_reason TEXT")
            cols = {str(r["name"]) for r in conn.execute("PRAGMA table_info(orders)").fetchall()}
            if "payment_status" in cols:
                return
            conn.execute(
                "ALTER TABLE orders ADD COLUMN payment_status TEXT NOT NULL DEFAULT 'unpaid'"
            )
            conn.execute("ALTER TABLE orders ADD COLUMN paid_at TEXT")
            conn.execute("UPDATE orders SET payment_status = 'legacy'")

    def _migrate_v2_1_columns(self) -> None:
        """Additive (Ticket 7): cash intent and the separate inbox alert switch."""
        with self._tx(immediate=True) as conn:
            for table, column, ddl in (
                ("orders", "pay_cash", "INTEGER NOT NULL DEFAULT 0"),
                ("conversations", "pay_cash", "INTEGER NOT NULL DEFAULT 0"),
                ("shop_state", "notify_owner_on_inbox", "INTEGER NOT NULL DEFAULT 1"),
            ):
                cols = {
                    str(r["name"]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()
                }
                if column not in cols:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

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
            notify_owner_on_inbox=bool(row["notify_owner_on_inbox"]),
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

    def set_notify_owner_on_inbox(self, enabled: bool) -> None:
        with self._tx(immediate=True) as conn:
            conn.execute(
                "UPDATE shop_state SET notify_owner_on_inbox = ?, updated_at = ? WHERE id = 1",
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
            turn_offset=int(row["turn_offset"]),
            quiet_reason=row["quiet_reason"],
            pay_cash=bool(row["pay_cash"]),
        )

    def set_pay_cash(self, line_user_id: str, pay_cash: bool) -> list[Order]:
        """Cash intent for today's chat and every unpaid order of this customer.

        Returns the unpaid orders it now applies to. The QR stays the default (pay_cash = 0).
        """
        self.get_or_create_conversation(line_user_id)
        now_iso = self.now_iso()
        with self._tx(immediate=True) as conn:
            conn.execute(
                "UPDATE conversations SET pay_cash = ?, updated_at = ? "
                "WHERE line_user_id = ? AND shop_date = ?",
                (int(pay_cash), now_iso, line_user_id, self.today_str()),
            )
            conn.execute(
                "UPDATE orders SET pay_cash = ?, updated_at = ? WHERE line_user_id = ? "
                "AND payment_status = 'unpaid' AND status IN ('queued', 'done')",
                (int(pay_cash), now_iso, line_user_id),
            )
            rows = conn.execute(
                "SELECT * FROM orders WHERE line_user_id = ? AND payment_status = 'unpaid' "
                "AND status IN ('queued', 'done') ORDER BY created_at ASC, id ASC",
                (line_user_id,),
            ).fetchall()
        return [_row_to_order(r) for r in rows]

    def pays_cash(self, line_user_id: str) -> bool:
        """The one cash rule (SPEC §3.9): said cash today, or every unpaid order is a cash order.

        Receipts, payment info and the live state all use this, so they never disagree.
        """
        if self.get_or_create_conversation(line_user_id).pay_cash:
            return True
        unpaid = self.get_unpaid_balance(line_user_id).orders
        return bool(unpaid) and all(o.pay_cash for o in unpaid)

    def is_handed_over(self, conv: Conversation) -> bool:
        if not conv.handed_over_until:
            return False
        try:
            until = datetime.fromisoformat(conv.handed_over_until)
        except ValueError:
            return False
        return self.now_bkk() < until

    def save_conversation_messages(self, conv_id: int, messages: list[dict[str, Any]]) -> None:
        with self._tx(immediate=True) as conn:
            conn.execute(
                "UPDATE conversations SET messages_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(messages, ensure_ascii=False), self.now_iso(), conv_id),
            )

    def append_conversation_messages(self, conv_id: int, new: list[dict[str, Any]]) -> None:
        """Append to the stored history in one transaction (never overwrites concurrent
        events such as an owner push written while an LLM turn was running)."""
        with self._tx(immediate=True) as conn:
            row = conn.execute(
                "SELECT messages_json FROM conversations WHERE id = ?", (conv_id,)
            ).fetchone()
            messages = [*json.loads(str(row["messages_json"])), *new]
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
        """Keep conversation history in sync with non-LLM events (confirm, cancel, pin, image).

        Read-modify-write in one transaction, so two events can't overwrite each other.
        """
        conv_id = self.get_or_create_conversation(line_user_id).id
        with self._tx(immediate=True) as conn:
            row = conn.execute(
                "SELECT messages_json FROM conversations WHERE id = ?", (conv_id,)
            ).fetchone()
            msgs: list[dict[str, Any]] = json.loads(str(row["messages_json"]))
            if compact_for_confirmed_order:
                # Drop stale cart-building turns; keep confirmed-order summaries and owner order
                # updates (done/cancel pushes the customer saw)
                base: list[dict[str, Any]] = []
                i = 0
                while i < len(msgs):
                    m = msgs[i]
                    if (
                        m.get("role") == "user"
                        and isinstance(m.get("content"), str)
                        and m["content"].startswith(KEPT_EVENT_PREFIXES)
                    ):
                        base.append(m)
                        if i + 1 < len(msgs) and msgs[i + 1].get("role") == "assistant":
                            base.append(msgs[i + 1])
                            i += 2
                            continue
                    i += 1
            else:
                base = list(msgs)
            base.append({"role": "user", "content": user_note})
            base.append({"role": "assistant", "content": assistant_reply})
            # Compaction drops customer turns from the history; lower the offset by as many
            # (it may go below 0), so the daily count is unchanged by the compaction
            removed = customer_turn_count(msgs) - customer_turn_count(base)
            conn.execute(
                "UPDATE conversations SET messages_json = ?, turn_offset = turn_offset - ?, "
                "updated_at = ? WHERE id = ?",
                (
                    json.dumps(base, ensure_ascii=False),
                    removed if compact_for_confirmed_order and removed > 0 else 0,
                    self.now_iso(),
                    conv_id,
                ),
            )

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

    # --- Hand-over inbox & bot quiet (SPEC §3.2) ---
    #
    # `conversations.handed_over_until` drives bot quiet:
    #   in the future -> quiet; in the past -> quiet ended, the "bot came back" note is still
    #   owed to the LLM; NULL -> not handed over (or the note was already given).

    def _quiet_until_after_activity(self, current: str | None, reason: str | None = None) -> str:
        now = self.now_bkk()
        if reason == "turn_limit":
            # Exclusive end at the next local midnight (the new shop day brings the bot back)
            end = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        else:
            end = now + timedelta(minutes=QUIET_MINUTES)
            if end.microsecond:  # round up so quiet never ends early
                end = end.replace(microsecond=0) + timedelta(seconds=1)
        candidate = end.isoformat(timespec="seconds")
        if current:
            try:
                if datetime.fromisoformat(current) > datetime.fromisoformat(candidate):
                    return current
            except ValueError:
                pass
        return candidate

    def open_handover(
        self,
        line_user_id: str,
        reason: str,
        detail: str | None = None,
        customer_text: str | None = None,
    ) -> tuple[int, bool]:
        """Make the bot quiet for this customer and open (or extend) their one inbox card.

        Returns (card_id, is_new_card).
        """
        conv = self.get_or_create_conversation(line_user_id)
        now_iso = self.now_iso()
        with self._tx(immediate=True) as conn:
            row = conn.execute(
                "SELECT handed_over_until, quiet_reason FROM conversations WHERE id = ?",
                (conv.id,),
            ).fetchone()
            still_quiet = self._is_future(row["handed_over_until"])
            in_turn_limit = still_quiet and row["quiet_reason"] == "turn_limit"
            quiet_reason: str | None
            if in_turn_limit:
                until, quiet_reason = str(row["handed_over_until"]), "turn_limit"
            else:
                current = row["handed_over_until"] if still_quiet else None
                until = self._quiet_until_after_activity(current, reason)
                quiet_reason = "turn_limit" if reason == "turn_limit" else None
            conn.execute(
                "UPDATE conversations SET handed_over_until = ?, quiet_reason = ?, updated_at = ? "
                "WHERE id = ?",
                (until, quiet_reason, now_iso, conv.id),
            )
            entry = {"kind": "reason", "reason": reason, "detail": detail, "at": now_iso}
            card = conn.execute(
                "SELECT * FROM handovers WHERE line_user_id = ? AND resolved_at IS NULL "
                "ORDER BY id DESC LIMIT 1",
                (line_user_id,),
            ).fetchone()
            recent_add = [{"text": customer_text, "at": now_iso}] if customer_text else []
            if card is None:
                cur = conn.execute(
                    """
                    INSERT INTO handovers (
                        line_user_id, shop_date, reasons_json, details_json, recent_json,
                        created_at, updated_at, last_customer_message_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        line_user_id,
                        self.today_str(),
                        json.dumps([reason]),
                        json.dumps([entry], ensure_ascii=False),
                        json.dumps(recent_add, ensure_ascii=False),
                        now_iso,
                        now_iso,
                        now_iso if customer_text else None,
                    ),
                )
                return int(cur.lastrowid or 0), True
            reasons: list[str] = json.loads(str(card["reasons_json"]))
            if reason not in reasons:
                reasons.append(reason)
            details: list[dict[str, Any]] = json.loads(str(card["details_json"]))
            details.append(entry)
            recent = (json.loads(str(card["recent_json"])) + recent_add)[-2:]
            conn.execute(
                """
                UPDATE handovers
                SET reasons_json = ?, details_json = ?, recent_json = ?, updated_at = ?,
                    last_customer_message_at = COALESCE(?, last_customer_message_at)
                WHERE id = ?
                """,
                (
                    json.dumps(reasons),
                    json.dumps(details, ensure_ascii=False),
                    json.dumps(recent, ensure_ascii=False),
                    now_iso,
                    now_iso if customer_text else None,
                    int(card["id"]),
                ),
            )
            return int(card["id"]), False

    def record_quiet_activity(
        self,
        line_user_id: str,
        preview: str,
        *,
        pin: dict[str, str] | None = None,
        reason: str | None = None,
        detail: str | None = None,
    ) -> None:
        """A customer message while the bot is quiet: restart the 15-minute timer and update
        the open inbox card (new-message count, last 2 messages, pins, extra reasons)."""
        conv = self.get_or_create_conversation(line_user_id)
        now_iso = self.now_iso()
        with self._tx(immediate=True) as conn:
            row = conn.execute(
                "SELECT handed_over_until, quiet_reason FROM conversations WHERE id = ?",
                (conv.id,),
            ).fetchone()
            current = row["handed_over_until"]
            # Re-checked inside the transaction: if the owner just tapped เรียบร้อย, don't
            # restore quiet.
            if not self._is_future(current):
                return
            # A turn_limit hand-over keeps its midnight end; others restart the 15 minutes.
            until = (
                str(current)
                if row["quiet_reason"] == "turn_limit"
                else self._quiet_until_after_activity(str(current))
            )
            conn.execute(
                "UPDATE conversations SET handed_over_until = ?, updated_at = ? WHERE id = ?",
                (until, now_iso, conv.id),
            )
            card = conn.execute(
                "SELECT * FROM handovers WHERE line_user_id = ? AND resolved_at IS NULL "
                "ORDER BY id DESC LIMIT 1",
                (line_user_id,),
            ).fetchone()
            if card is None:
                return
            recent = (json.loads(str(card["recent_json"])) + [{"text": preview, "at": now_iso}])[
                -2:
            ]
            details: list[dict[str, Any]] = json.loads(str(card["details_json"]))
            reasons: list[str] = json.loads(str(card["reasons_json"]))
            if pin is not None:
                details.append({"kind": "pin", **pin, "at": now_iso})
            if reason is not None:
                details.append(
                    {"kind": "reason", "reason": reason, "detail": detail, "at": now_iso}
                )
                if reason not in reasons:
                    reasons.append(reason)
            conn.execute(
                """
                UPDATE handovers
                SET recent_json = ?, details_json = ?, reasons_json = ?,
                    unseen_count = unseen_count + 1, last_customer_message_at = ?,
                    updated_at = CASE WHEN ? THEN ? ELSE updated_at END
                WHERE id = ?
                """,
                (
                    json.dumps(recent, ensure_ascii=False),
                    json.dumps(details, ensure_ascii=False),
                    json.dumps(reasons),
                    now_iso,
                    int(reason is not None),
                    now_iso,
                    int(card["id"]),
                ),
            )

    def _is_future(self, iso: object) -> bool:
        if not iso:
            return False
        try:
            return self.now_bkk() < datetime.fromisoformat(str(iso))
        except ValueError:
            return False

    def _end_quiet_in_tx(self, conn: sqlite3.Connection, conv_id: int) -> None:
        past = (self.now_bkk() - timedelta(seconds=1)).isoformat(timespec="seconds")
        conn.execute(
            """
            UPDATE conversations SET handed_over_until = ?, quiet_reason = NULL, updated_at = ?
            WHERE id = ? AND handed_over_until IS NOT NULL AND handed_over_until > ?
            """,
            (past, self.now_iso(), conv_id, past),
        )

    def add_reason_to_open_card(self, line_user_id: str, reason: str, detail: str | None) -> bool:
        """Attach an outcome (e.g. a slip problem) to the open card. Never reopens a card.

        False when no card is open (e.g. the owner resolved it meanwhile): the caller must
        then hand over normally so the outcome isn't lost.
        """
        now_iso = self.now_iso()
        with self._tx(immediate=True) as conn:
            card = conn.execute(
                "SELECT * FROM handovers WHERE line_user_id = ? AND resolved_at IS NULL "
                "ORDER BY id DESC LIMIT 1",
                (line_user_id,),
            ).fetchone()
            if card is None:
                return False
            reasons: list[str] = json.loads(str(card["reasons_json"]))
            if reason not in reasons:
                reasons.append(reason)
            details: list[dict[str, Any]] = json.loads(str(card["details_json"]))
            details.append({"kind": "reason", "reason": reason, "detail": detail, "at": now_iso})
            conn.execute(
                "UPDATE handovers SET reasons_json = ?, details_json = ?, updated_at = ? "
                "WHERE id = ?",
                (json.dumps(reasons), json.dumps(details, ensure_ascii=False), now_iso, card["id"]),
            )
            return True

    def mark_transfer_manual(
        self, card_id: int, order_ids: list[int]
    ) -> tuple[list[Order], list[int]] | None:
        """จ่ายแล้ว ✓ picker, in one transaction with the card check.

        None if the card isn't an open slip card; else (marked orders, ids not eligible).
        """
        now_iso = self.now_iso()
        marked: list[Order] = []
        skipped: list[int] = []
        with self._tx(immediate=True) as conn:
            card = conn.execute(
                "SELECT * FROM handovers WHERE id = ? AND resolved_at IS NULL", (card_id,)
            ).fetchone()
            reasons: list[str] = json.loads(str(card["reasons_json"])) if card else []
            if card is None or not any(r.startswith("slip_") for r in reasons):
                return None
            line_user_id = str(card["line_user_id"])
            for oid in order_ids:
                cur = conn.execute(
                    """
                    UPDATE orders
                    SET payment_status = 'transfer_manual', paid_at = ?, updated_at = ?
                    WHERE id = ? AND line_user_id = ? AND payment_status = 'unpaid'
                      AND status IN ('queued', 'done')
                    """,
                    (now_iso, now_iso, oid, line_user_id),
                )
                if cur.rowcount == 0:
                    skipped.append(oid)
                    continue
                row = conn.execute("SELECT * FROM orders WHERE id = ?", (oid,)).fetchone()
                marked.append(_row_to_order(row))
        return marked, skipped

    def end_quiet_on_button_tap(self, line_user_id: str) -> None:
        """Confirm/Cancel tap ends bot quiet (the next LLM call still gets the "bot came back"
        note), except during a `turn_limit` hand-over, which only เรียบร้อย or a new day ends."""
        conv = self.get_or_create_conversation(line_user_id)
        with self._tx(immediate=True) as conn:
            row = conn.execute(
                "SELECT quiet_reason FROM conversations WHERE id = ?", (conv.id,)
            ).fetchone()
            if row["quiet_reason"] != "turn_limit":
                self._end_quiet_in_tx(conn, conv.id)

    def consume_resume_note(self, conv: Conversation) -> bool:
        """True once after a hand-over ended (any way); clears the marker."""
        if not conv.handed_over_until or self.is_handed_over(conv):
            return False
        now_iso = self.now_iso()
        with self._tx(immediate=True) as conn:
            # Only clear a quiet window that is still over: an owner takeover (คุยเอง) started
            # meanwhile must survive
            cur = conn.execute(
                "UPDATE conversations SET handed_over_until = NULL, updated_at = ? "
                "WHERE id = ? AND handed_over_until IS NOT NULL AND handed_over_until <= ?",
                (now_iso, conv.id, now_iso),
            )
        return cur.rowcount > 0

    def resolve_handover(self, card_id: int) -> bool:
        """Owner taps เรียบร้อย: hide the card, end bot quiet, and after a `turn_limit`
        hand-over reset that customer's message count for the shop day."""
        today = self.today_str()
        with self._tx(immediate=True) as conn:
            card = conn.execute(
                "SELECT * FROM handovers WHERE id = ? AND resolved_at IS NULL", (card_id,)
            ).fetchone()
            if card is None:
                return False
            conn.execute(
                "UPDATE handovers SET resolved_at = ? WHERE id = ?", (self.now_iso(), card_id)
            )
            conv = conn.execute(
                "SELECT * FROM conversations WHERE line_user_id = ? AND shop_date = ?",
                (str(card["line_user_id"]), today),
            ).fetchone()
            if conv is None:
                return True
            self._end_quiet_in_tx(conn, int(conv["id"]))
            # Reset today's message count after today's turn_limit hand-over
            if conv["quiet_reason"] == "turn_limit":
                messages: list[dict[str, Any]] = json.loads(str(conv["messages_json"]))
                user_turns = customer_turn_count(messages)
                conn.execute(
                    "UPDATE conversations SET turn_offset = ?, updated_at = ? WHERE id = ?",
                    (user_turns, self.now_iso(), int(conv["id"])),
                )
        return True

    def get_open_handovers(self) -> list[dict[str, Any]]:
        """Open inbox cards, most recently updated first, with the bot state for each."""
        today = self.today_str()
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT h.*, c.display_name, v.handed_over_until
                FROM handovers h
                LEFT JOIN customers c ON c.line_user_id = h.line_user_id
                LEFT JOIN conversations v
                    ON v.line_user_id = h.line_user_id AND v.shop_date = ?
                WHERE h.resolved_at IS NULL
                ORDER BY h.updated_at DESC, h.id DESC
                """,
                (today,),
            ).fetchall()
        finally:
            conn.close()
        now = self.now_bkk()
        out: list[dict[str, Any]] = []
        for r in rows:
            until_raw = r["handed_over_until"]
            minutes_left: int | None = None
            if until_raw:
                try:
                    delta = (datetime.fromisoformat(str(until_raw)) - now).total_seconds()
                    if delta > 0:
                        minutes_left = int((delta + 59) // 60)
                except ValueError:
                    pass
            user_id = str(r["line_user_id"])
            out.append(
                {
                    "id": int(r["id"]),
                    "line_user_id": user_id,
                    "display_name": r["display_name"] or f"ลูกค้า …{user_id[-4:]}",
                    "reasons": json.loads(str(r["reasons_json"])),
                    "details": json.loads(str(r["details_json"])),
                    "recent": json.loads(str(r["recent_json"])),
                    "unseen_count": int(r["unseen_count"]),
                    "created_at": str(r["created_at"]),
                    "updated_at": str(r["updated_at"]),
                    "bot_quiet": minutes_left is not None,
                    "quiet_minutes_left": minutes_left,
                }
            )
        return out

    # --- Orders ---

    def confirm_card(self, line_user_id: str, card_id: str, menu: Menu) -> ConfirmResult:
        """Atomically verify card snapshot, re-check availability/price, and queue order."""
        with self._tx(immediate=True) as conn:
            return self._confirm_card_in_tx(conn, line_user_id, card_id, menu)

    def _confirm_card_in_tx(
        self, conn: sqlite3.Connection, line_user_id: str, card_id: str, menu: Menu
    ) -> ConfirmResult:
        """The Confirm-postback path; slip payment reuses it inside its own transaction."""
        now = self.now_bkk()
        today = now.strftime("%Y-%m-%d")
        now_iso = now.isoformat(timespec="seconds")

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
            "SELECT COALESCE(MAX(queue_number), 0) + 1 AS next_q FROM orders WHERE shop_date = ?",
            (today,),
        ).fetchone()
        queue_number = int(q_row["next_q"])

        items_payload = [line.to_dict() for line in priced.lines]
        cur = conn.execute(
            """
            INSERT INTO orders (
                shop_date, queue_number, line_user_id, card_id, status,
                fulfillment, address, location_url, requested_time,
                items_json, food_total, delivery_fee, total, created_at, updated_at, pay_cash
            ) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                int(conv_row["pay_cash"]),
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

    def cancel_customer_order(
        self, line_user_id: str, order_id: int
    ) -> tuple[Order | None, Order | None]:
        """Bot cancel. Returns (cancelled_order, None), or (None, paid_order) when the customer's
        own non-cancelled order isn't `unpaid` (the bot must hand over), or (None, None)."""
        now_iso = self.now_iso()
        with self._tx(immediate=True) as conn:
            row = conn.execute(
                "SELECT * FROM orders WHERE id = ? AND line_user_id = ?", (order_id, line_user_id)
            ).fetchone()
            if row is None or str(row["status"]) == "cancelled":
                return None, None
            if str(row["payment_status"]) != "unpaid":
                return None, _row_to_order(row)
            if str(row["status"]) != "queued":
                return None, None
            conn.execute(
                """
                UPDATE orders
                SET status = 'cancelled', cancel_reason = 'ลูกค้ายกเลิกเอง', updated_at = ?
                WHERE id = ?
                """,
                (now_iso, order_id),
            )
            row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            return _row_to_order(row), None

    def get_queued_orders(self) -> list[Order]:
        """The queue: every v2 order still waiting to be cooked, from any shop day, oldest first.

        Pre-v2 (`legacy`) orders never count: they are shown on /kitchen so the owner can clear
        them, but they must not inflate queue positions or wait estimates.
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM orders WHERE status = 'queued' AND payment_status != 'legacy' "
                "ORDER BY created_at ASC, id ASC"
            ).fetchall()
            return [_row_to_order(r) for r in rows]
        finally:
            conn.close()

    def get_customer_queued_before_today(self, line_user_id: str) -> list[Order]:
        """This customer's orders from earlier shop days still waiting, `legacy` included."""
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM orders WHERE line_user_id = ? AND status = 'queued' "
                "AND shop_date < ? ORDER BY created_at ASC, id ASC",
                (line_user_id, self.today_str()),
            ).fetchall()
            return [_row_to_order(r) for r in rows]
        finally:
            conn.close()

    def get_order(self, order_id: int) -> Order | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            return _row_to_order(row) if row is not None else None
        finally:
            conn.close()

    def get_unpaid_balance(self, line_user_id: str) -> UnpaidBalance:
        """Unpaid, non-cancelled orders across all shop days (never `legacy`), oldest first."""
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT * FROM orders
                WHERE line_user_id = ? AND payment_status = 'unpaid'
                  AND status IN ('queued', 'done')
                ORDER BY created_at ASC, id ASC
                """,
                (line_user_id,),
            ).fetchall()
        finally:
            conn.close()
        orders = [_row_to_order(r) for r in rows]
        return UnpaidBalance(total=sum(o.total for o in orders), orders=orders)

    def set_payment_status(
        self, order_id: int, new_status: Literal["cash", "transfer_manual", "unpaid"]
    ) -> tuple[Order | None, str | None]:
        """Owner payment action. Returns (order, None) or (None, 'not_found' | 'conflict').

        `unpaid` -> `cash`/`transfer_manual` (queued or done orders only), or undo
        `cash`/`transfer_manual` -> `unpaid` at any time. `slip` and `legacy` never change here.
        """
        with self._tx(immediate=True) as conn:
            row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            if row is None:
                return None, "not_found"
            current = str(row["payment_status"])
            if new_status == "unpaid":
                if current not in OWNER_PAID_STATUSES:
                    return None, "conflict"
                paid_at: str | None = None
            else:
                if current != "unpaid" or str(row["status"]) not in ("queued", "done"):
                    return None, "conflict"
                paid_at = self.now_iso()
            conn.execute(
                "UPDATE orders SET payment_status = ?, paid_at = ?, updated_at = ? WHERE id = ?",
                (new_status, paid_at, self.now_iso(), order_id),
            )
            updated = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            return _row_to_order(updated), None

    def get_unpaid_done_orders(self) -> list[dict[str, Any]]:
        """The ค้างจ่าย list: unpaid orders no longer queued (done), any shop day, newest first."""
        today = self.now_bkk().date()
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT * FROM orders
                WHERE payment_status = 'unpaid' AND status = 'done'
                ORDER BY created_at DESC, id DESC
                """
            ).fetchall()
        finally:
            conn.close()
        return [self._with_day_label(_row_to_order(r), today) for r in rows]

    @staticmethod
    def _label_for(shop_date: str, today: date) -> str:
        days_ago = (today - datetime.strptime(shop_date, "%Y-%m-%d").date()).days
        if days_ago <= 0:
            return "วันนี้"
        if days_ago == 1:
            return "เมื่อวาน"
        d = datetime.strptime(shop_date, "%Y-%m-%d")
        return f"{d.day}/{d.month}"

    def day_label(self, shop_date: str) -> str:
        return self._label_for(shop_date, self.now_bkk().date())

    @staticmethod
    def _with_day_label(o: Order, today: date) -> dict[str, Any]:
        label = ShopDB._label_for(o.shop_date, today)
        return {**o.to_dict(), "is_today": label == "วันนี้", "day_label": label}

    def get_payment_summary_today(self) -> dict[str, Any]:
        """Payment lines for สรุปวันนี้ (SPEC §3.1.11).

        Received totals count money paid today (`paid_at` today, cancelled orders included,
        since refunds are listed separately). Refunds due are paid orders cancelled today.
        """
        today = self.today_str()
        paid_statuses = ",".join(f"'{p}'" for p in PAID_STATUSES)
        conn = self._connect()
        try:
            received = conn.execute(
                "SELECT payment_status, COALESCE(SUM(total), 0) AS amount FROM orders "
                "WHERE substr(paid_at, 1, 10) = ? GROUP BY payment_status",
                (today,),
            ).fetchall()
            unpaid_rows = conn.execute(
                "SELECT * FROM orders WHERE payment_status = 'unpaid' "
                "AND status IN ('queued', 'done') ORDER BY shop_date, queue_number"
            ).fetchall()
            refund_rows = conn.execute(
                f"SELECT * FROM orders WHERE status = 'cancelled' "
                f"AND payment_status IN ({paid_statuses}) AND substr(updated_at, 1, 10) = ? "
                f"ORDER BY queue_number",
                (today,),
            ).fetchall()
        finally:
            conn.close()
        by_status = {str(r["payment_status"]): int(r["amount"]) for r in received}
        unpaid = [_row_to_order(r) for r in unpaid_rows]
        unpaid_earlier = [o for o in unpaid if o.shop_date < today]
        return {
            "slip_total": by_status.get("slip", 0),
            "cash_total": sum(by_status.get(p, 0) for p in OWNER_PAID_STATUSES),
            "unpaid_today": [o for o in unpaid if o.shop_date == today],
            "unpaid_earlier_count": len(unpaid_earlier),
            "unpaid_earlier_total": sum(o.total for o in unpaid_earlier),
            "refunds_due": [_row_to_order(r) for r in refund_rows],
        }

    def get_kitchen_orders_today(self) -> dict[str, Any]:
        today = self.today_str()
        today_date = self.now_bkk().date()
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM orders WHERE shop_date = ? ORDER BY queue_number ASC",
                (today,),
            ).fetchall()
            earlier_rows = conn.execute(
                "SELECT * FROM orders WHERE shop_date < ? AND status = 'queued' "
                "ORDER BY created_at ASC, id ASC",
                (today,),
            ).fetchall()
            # Earlier-day orders finished or paid today stay reachable (e.g. ยกเลิกจ่าย)
            earlier_finished_rows = conn.execute(
                "SELECT * FROM orders WHERE shop_date < ? AND status IN ('done', 'cancelled') "
                "AND substr(updated_at, 1, 10) = ? ORDER BY updated_at DESC, id DESC",
                (today, today),
            ).fetchall()
        finally:
            conn.close()

        orders = [_row_to_order(r) for r in rows]
        earlier_queued = [_row_to_order(r) for r in earlier_rows]
        queued = [o for o in orders if o.status == "queued"]
        finished = [o for o in orders if o.status in ("done", "cancelled")]
        finished.reverse()
        finished += [_row_to_order(r) for r in earlier_finished_rows]
        done_orders = [o for o in orders if o.status == "done"]
        cancelled_orders = [o for o in orders if o.status == "cancelled"]
        return {
            "shop_date": today,
            "queued": [self._with_day_label(o, today_date) for o in [*earlier_queued, *queued]],
            "finished": [self._with_day_label(o, today_date) for o in finished],
            "stats": {
                "queued_count": len(queued),
                "done_count": len(done_orders),
                "cancelled_count": len(cancelled_orders),
                "done_total": sum(o.total for o in done_orders),
            },
        }

    def mark_order_done(self, order_id: int, *, paid_cash: bool = False) -> Order | None:
        """Queued -> done. `paid_cash` also records cash in the same transaction, but only if
        the order is still `unpaid` (a payment recorded meanwhile is never overwritten)."""
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
            if paid_cash:
                conn.execute(
                    "UPDATE orders SET payment_status = 'cash', paid_at = ? "
                    "WHERE id = ? AND payment_status = 'unpaid'",
                    (now_iso, order_id),
                )
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

    # --- Slips (SPEC §3.1.5–6) ---

    def _record_slip_in_tx(
        self,
        conn: sqlite3.Connection,
        trans_ref: str,
        line_user_id: str,
        amount_satang: int | None,
        transfer_at: datetime | None,
        result: str,
        order_ids: list[int] | None = None,
    ) -> None:
        """Set the final result on this customer's pending claim (see `claim_slip`)."""
        conn.execute(
            """
            UPDATE slip_checks
            SET amount_satang = ?, transfer_at = ?, result = ?, order_ids_json = ?
            WHERE trans_ref = ? AND line_user_id = ? AND result = 'pending'
            """,
            (
                amount_satang,
                transfer_at.isoformat(timespec="seconds") if transfer_at else None,
                result,
                json.dumps(order_ids or []),
                trans_ref,
                line_user_id,
            ),
        )

    def claim_slip(self, trans_ref: str, line_user_id: str) -> bool:
        """Atomically claim a transaction reference before any SlipOK call.

        False means it was seen before (by anyone), so it must never be checked or paid again.
        """
        with self._tx(immediate=True) as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO slip_checks (trans_ref, line_user_id, result, created_at)
                VALUES (?, ?, 'pending', ?)
                """,
                (trans_ref, line_user_id, self.now_iso()),
            )
            return cur.rowcount > 0

    def get_slip_check(self, trans_ref: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM slip_checks WHERE trans_ref = ?", (trans_ref,)
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            conn.close()

    def record_slip_check(
        self,
        trans_ref: str,
        line_user_id: str,
        result: str,
        amount_satang: int | None = None,
        transfer_at: datetime | None = None,
    ) -> None:
        with self._tx(immediate=True) as conn:
            self._record_slip_in_tx(
                conn, trans_ref, line_user_id, amount_satang, transfer_at, result
            )

    def amount_due_satang(self, line_user_id: str) -> int:
        """Unpaid balance plus today's open order card, in satang."""
        conv = self.get_or_create_conversation(line_user_id)
        card_total = int(conv.active_card["total"]) if conv.active_card else 0
        return (self.get_unpaid_balance(line_user_id).total + card_total) * 100

    def slip_snapshot(self, line_user_id: str) -> tuple[tuple[int, ...], str | None]:
        """What a slip can pay now (unpaid order IDs, open card), taken before the SlipOK call."""
        conv = self.get_or_create_conversation(line_user_id)
        ids = tuple(o.id for o in self.get_unpaid_balance(line_user_id).orders)
        return ids, conv.active_card_id

    def pay_with_slip(
        self,
        line_user_id: str,
        trans_ref: str,
        amount_satang: int,
        transfer_at: datetime,
        menu: Menu,
        expected: tuple[tuple[int, ...], str | None] | None = None,
    ) -> SlipPayResult:
        """Slip match, date rule, card confirmation and payment, recorded in one transaction.

        `expected` is `slip_snapshot` from before the SlipOK call. If what the customer owes
        changed meanwhile (e.g. the owner recorded this same transfer by hand), nothing is paid
        and the slip goes to the owner as `slip_balance_changed`: one transfer, one credit.
        """
        today = self.today_str()
        transfer_day = transfer_at.astimezone(BKK).strftime("%Y-%m-%d")
        with self._tx(immediate=True) as conn:
            claim = conn.execute(
                "SELECT line_user_id, result FROM slip_checks WHERE trans_ref = ?", (trans_ref,)
            ).fetchone()
            if (
                claim is None
                or claim["result"] != "pending"
                or claim["line_user_id"] != line_user_id
            ):
                return SlipPayResult("slip_duplicate", [], None, 0)
            rows = conn.execute(
                """
                SELECT * FROM orders
                WHERE line_user_id = ? AND payment_status = 'unpaid'
                  AND status IN ('queued', 'done')
                ORDER BY created_at ASC, id ASC
                """,
                (line_user_id,),
            ).fetchall()
            unpaid = [_row_to_order(r) for r in rows]
            conv_row = conn.execute(
                "SELECT * FROM conversations WHERE line_user_id = ? AND shop_date = ?",
                (line_user_id, today),
            ).fetchone()
            card: dict[str, Any] | None = (
                json.loads(str(conv_row["active_card_json"]))
                if conv_row is not None and conv_row["active_card_json"]
                else None
            )
            items: list[tuple[int | None, int]] = [(o.id, o.total * 100) for o in unpaid]
            if card is not None:
                items.append((None, int(card["total"]) * 100))  # None = the open order card
            owed = sum(total for _, total in items)

            def done(result: str, paid: list[Order], confirmed: Order | None) -> SlipPayResult:
                self._record_slip_in_tx(
                    conn,
                    trans_ref,
                    line_user_id,
                    amount_satang,
                    transfer_at,
                    result,
                    [o.id for o in paid],
                )
                return SlipPayResult(result, paid, confirmed, owed)

            current = (tuple(o.id for o in unpaid), card["card_id"] if card is not None else None)
            if expected is not None and current != expected:
                return done("slip_balance_changed", [], None)
            matched = match_slip(items, amount_satang)
            if matched is None:
                return done("slip_amount_mismatch", [], None)
            by_id = {o.id: o for o in unpaid}
            if any(by_id[k].shop_date > transfer_day for k in matched if k is not None):
                return done("slip_too_old", [], None)
            confirmed: Order | None = None
            if None in matched:
                if transfer_day != today or card is None:
                    return done("slip_too_old", [], None)
                res = self._confirm_card_in_tx(conn, line_user_id, str(card["card_id"]), menu)
                if not res.ok or res.order is None or res.already_confirmed:
                    return done("slip_card_confirm_failed", [], None)
                confirmed = res.order
            ids = [k for k in matched if k is not None]
            if confirmed is not None:
                ids.append(confirmed.id)
            now_iso = self.now_iso()
            conn.executemany(
                "UPDATE orders SET payment_status = 'slip', paid_at = ?, updated_at = ? "
                "WHERE id = ?",
                [(now_iso, now_iso, oid) for oid in ids],
            )
            paid_rows = conn.execute(
                f"SELECT * FROM orders WHERE id IN ({','.join('?' * len(ids))}) "
                "ORDER BY created_at, id",
                ids,
            ).fetchall()
            paid = [_row_to_order(r) for r in paid_rows]
            if confirmed is not None:
                confirmed = next(o for o in paid if o.id == confirmed.id)
            return done("matched", paid, confirmed)

    # --- LINE webhook deduplication ---

    def record_line_event(self, event_id: str) -> bool:
        """Record a webhookEventId; returns True if new, False if duplicate."""
        with self._tx(immediate=True) as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO line_events (event_id, received_at) VALUES (?, ?)",
                (event_id, self.now_iso()),
            )
            return cur.rowcount > 0
