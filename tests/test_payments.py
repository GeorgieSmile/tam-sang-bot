"""Behavioral tests for v2 Ticket 1: payment status, ค้างจ่าย, refunds due, and summaries."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app.agent import HANDOVER_REPLY, handle_customer_text
from app.config import Settings
from app.db import Order, ShopDB
from app.main import create_app
from app.menu import Menu, load_menu
from app.owner import handle_owner_text

MENU_PATH = Path(__file__).resolve().parent.parent / "menu.yaml"
BKK = ZoneInfo("Asia/Bangkok")
KEY = "?key=secret-123"

# Exact v1 schema from git a516222:app/db.py (the shipped v1).
V1_SCHEMA_SQL = """
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

V1_TABLES = ("shop_state", "menu_overrides", "customers", "conversations", "orders", "line_events")

# Pickup cart prices from menu.yaml
CART_60 = [{"dish_id": "kaphrao", "qty": 1, "option_ids": ["crispy_pork"]}]
CART_70 = [{"dish_id": "kaphrao", "qty": 1, "option_ids": ["crispy_pork", "fried_egg_runny"]}]
CART_80 = [
    {"dish_id": "kaphrao", "qty": 1, "option_ids": ["crispy_pork", "fried_egg_runny", "special"]}
]
CART_130 = CART_70 + CART_60


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class SpyLineClient:
    def __init__(self) -> None:
        self.replies: list[tuple[str, list[dict[str, Any]]]] = []
        self.pushes: list[tuple[str, str]] = []

    def reply_messages(self, reply_token: str, messages: list[dict[str, Any]]) -> None:
        self.replies.append((reply_token, messages))

    def push_text(self, user_id: str, text: str) -> None:
        self.pushes.append((user_id, text))

    def show_loading(self, user_id: str, seconds: int = 20) -> None:
        return None

    def get_profile(self, user_id: str) -> str | None:
        return None

    def get_message_content(self, message_id: str) -> bytes:
        return b""


class StubLLM:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("StubLLM ran out of scripted responses")
        return self._responses.pop(0)


def _resp(content: str = "", tool_calls: list[Any] | None = None) -> Any:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=tool_calls))]
    )


def _tool_call(call_id: str, name: str, args: dict[str, Any]) -> Any:
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(args, ensure_ascii=False)),
    )


def _settings(db_path: Path) -> Settings:
    return Settings(
        db_path=db_path,
        menu_path=MENU_PATH,
        kitchen_secret="secret-123",
        public_base_url="http://localhost:8000",
        payment_info_text="",
        line_channel_secret="sec",
        line_channel_access_token="tok",
        owner_line_user_id="U_OWNER",
        llm_base_url="https://openrouter.ai/api/v1",
        llm_api_key="key",
        llm_model="anthropic/claude-haiku-5.5",
        llm_reasoning_effort="low",
    )


def _place(db: ShopDB, menu: Menu, user_id: str, card_id: str, items: list[Any]) -> Order:
    conv = db.get_or_create_conversation(user_id)
    rev = db.save_draft(conv.id, {"items": items, "fulfillment": "pickup"})
    total = menu.price_cart(items, "pickup", {}).total
    db.set_active_card(
        conv.id,
        card_id,
        {
            "card_id": card_id,
            "draft_revision": rev,
            "raw_items": items,
            "fulfillment": "pickup",
            "address": None,
            "location_url": None,
            "requested_time": None,
            "total": total,
        },
    )
    res = db.confirm_card(user_id, card_id, menu)
    assert res.ok and res.order is not None, res.error
    return res.order


def _force_payment(db: ShopDB, order_id: int, status: str, paid_at: str | None) -> None:
    conn = sqlite3.connect(db.db_path)
    try:
        conn.execute(
            "UPDATE orders SET payment_status = ?, paid_at = ? WHERE id = ?",
            (status, paid_at, order_id),
        )
        conn.commit()
    finally:
        conn.close()


def _counts(path: Path) -> dict[str, int]:
    conn = sqlite3.connect(path)
    try:
        return {t: int(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]) for t in V1_TABLES}
    finally:
        conn.close()


def test_v1_database_migrates_to_legacy_and_new_orders_are_unpaid(tmp_path: Path) -> None:
    path = tmp_path / "shop.db"
    conn = sqlite3.connect(path)
    conn.executescript(V1_SCHEMA_SQL)
    ts = "2026-11-23T12:00:00+07:00"
    conn.execute("INSERT INTO shop_state (id, updated_at) VALUES (1, ?)", (ts,))
    conn.execute(
        "INSERT INTO menu_overrides (kind, key, is_available, updated_at) VALUES (?, ?, 0, ?)",
        ("ingredient", "squid", ts),
    )
    conn.execute(
        "INSERT INTO customers (line_user_id, last_address, updated_at) VALUES (?, ?, ?)",
        ("U_OLD", "บ้าน 1", ts),
    )
    conn.execute(
        "INSERT INTO conversations (line_user_id, shop_date, messages_json, draft_json, "
        "updated_at) VALUES ('U_OLD', '2026-11-23', '[]', '{}', ?)",
        (ts,),
    )
    # A v1 hand-over still running on migration day (v1 set it to the end of the day)
    conn.execute(
        "INSERT INTO conversations (line_user_id, shop_date, messages_json, draft_json, "
        "handed_over_until, updated_at) "
        "VALUES ('U_HO', '2026-11-24', '[]', '{}', '2026-11-24T23:59:59+07:00', ?)",
        (ts,),
    )
    for q, status in [(1, "done"), (2, "cancelled"), (3, "queued")]:
        conn.execute(
            "INSERT INTO orders (shop_date, queue_number, line_user_id, card_id, status, "
            "fulfillment, items_json, food_total, delivery_fee, total, created_at, updated_at) "
            "VALUES ('2026-11-23', ?, 'U_OLD', ?, ?, 'pickup', '[]', 60, 0, 60, ?, ?)",
            (q, f"old-{q}", status, ts, ts),
        )
    conn.execute("INSERT INTO line_events (event_id, received_at) VALUES ('e1', ?)", (ts,))
    conn.commit()
    conn.close()
    before = _counts(path)

    menu = load_menu(MENU_PATH)
    clock = Clock(datetime(2026, 11, 24, 12, 0, tzinfo=BKK))
    db = ShopDB(path, clock=clock)
    assert _counts(path) == before

    conn = sqlite3.connect(path)
    statuses = [r[0] for r in conn.execute("SELECT payment_status FROM orders ORDER BY id")]
    conn.close()
    assert statuses == ["legacy", "legacy", "legacy"]
    # The v1 hand-over has no inbox card, so it is ended (the bot-came-back note is still owed)
    ho_conv = db.get_or_create_conversation("U_HO")
    assert db.is_handed_over(ho_conv) is False
    assert ho_conv.handed_over_until == "2026-11-24T11:59:59+07:00"

    new_order = _place(db, menu, "U_OLD", "new-1", CART_60)
    assert new_order.payment_status == "unpaid"
    assert new_order.paid_at is None

    # Re-opening (a restart) does not re-run the backfill on v2 orders
    db = ShopDB(path, clock=clock)
    conn = sqlite3.connect(path)
    statuses = [r[0] for r in conn.execute("SELECT payment_status FROM orders ORDER BY id")]
    conn.close()
    assert statuses == ["legacy", "legacy", "legacy", "unpaid"]
    # legacy orders never count toward the unpaid balance or the ค้างจ่าย list
    assert db.get_unpaid_balance("U_OLD").total == 60
    assert db.get_unpaid_done_orders() == []


def test_unpaid_balance_spans_days_and_skips_cancelled_and_legacy(tmp_path: Path) -> None:
    menu = load_menu(MENU_PATH)
    clock = Clock(datetime(2026, 11, 23, 12, 0, tzinfo=BKK))
    db = ShopDB(tmp_path / "shop.db", clock=clock)

    o3 = _place(db, menu, "U_A", "y-1", CART_60)  # yesterday 60
    db.mark_order_done(o3.id)
    legacy = _place(db, menu, "U_A", "y-2", CART_80)
    _force_payment(db, legacy.id, "legacy", None)

    clock.now = datetime(2026, 11, 24, 12, 0, tzinfo=BKK)
    _place(db, menu, "U_A", "t-1", CART_70)  # today 70
    cancelled = _place(db, menu, "U_A", "t-2", CART_80)
    assert db.cancel_order_by_owner(cancelled.id, "ของหมด") is not None

    balance = db.get_unpaid_balance("U_A")
    assert balance.total == 130
    assert [o.total for o in balance.orders] == [60, 70]


def test_owner_cash_undo_and_slip_undo_conflict(tmp_path: Path) -> None:
    menu = load_menu(MENU_PATH)
    clock = Clock(datetime(2026, 11, 24, 12, 0, tzinfo=BKK))
    db = ShopDB(tmp_path / "shop.db", clock=clock)
    spy = SpyLineClient()
    client = TestClient(
        create_app(settings=_settings(db.db_path), db=db, menu=menu, line_client=spy)
    )

    o = _place(db, menu, "U_A", "c-1", CART_70)
    r = client.post(f"/api/kitchen/orders/{o.id}/payment", json={"status": "cash"})
    assert r.status_code == 403
    r = client.post(f"/api/kitchen/orders/{o.id}/payment{KEY}", json={"status": "cash"})
    assert r.status_code == 200
    assert r.json()["order"]["payment_status"] == "cash"
    assert r.json()["order"]["paid_at"] == "2026-11-24T12:00:00+07:00"
    assert spy.pushes == [] and spy.replies == []

    # Already paid -> can't be marked paid again
    again = client.post(f"/api/kitchen/orders/{o.id}/payment{KEY}", json={"status": "cash"})
    assert again.status_code == 409

    undo = client.post(f"/api/kitchen/orders/{o.id}/payment{KEY}", json={"status": "unpaid"})
    assert undo.status_code == 200
    assert undo.json()["order"]["payment_status"] == "unpaid"
    assert undo.json()["order"]["paid_at"] is None

    slip = _place(db, menu, "U_B", "c-2", CART_60)
    _force_payment(db, slip.id, "slip", "2026-11-24T12:00:00+07:00")
    r_slip = client.post(f"/api/kitchen/orders/{slip.id}/payment{KEY}", json={"status": "unpaid"})
    assert r_slip.status_code == 409
    assert db.get_order(slip.id) is not None
    assert db.get_order(slip.id).payment_status == "slip"  # type: ignore[union-attr]

    missing = client.post(f"/api/kitchen/orders/999/payment{KEY}", json={"status": "cash"})
    assert missing.status_code == 404

    # Undo works at any time, even after the owner cancelled the order (no refund due then)
    c = _place(db, menu, "U_C", "c-3", CART_60)
    assert client.post(
        f"/api/kitchen/orders/{c.id}/payment{KEY}", json={"status": "cash"}
    ).is_success
    assert db.cancel_order_by_owner(c.id, "ของหมด") is not None
    spy.pushes.clear()
    late_undo = client.post(f"/api/kitchen/orders/{c.id}/payment{KEY}", json={"status": "unpaid"})
    assert late_undo.status_code == 200
    assert late_undo.json()["order"]["status"] == "cancelled"
    # ...but a cancelled order can't be marked paid
    re_pay = client.post(f"/api/kitchen/orders/{c.id}/payment{KEY}", json={"status": "cash"})
    assert re_pay.status_code == 409
    assert spy.pushes == []


def test_unpaid_section_lists_done_unpaid_orders_newest_first(tmp_path: Path) -> None:
    menu = load_menu(MENU_PATH)
    clock = Clock(datetime(2026, 11, 23, 12, 0, tzinfo=BKK))
    db = ShopDB(tmp_path / "shop.db", clock=clock)
    client = TestClient(
        create_app(settings=_settings(db.db_path), db=db, menu=menu, line_client=SpyLineClient())
    )

    # Yesterday: #1 done paid, #2 ... up to #9 done unpaid (only #9 matters)
    for i in range(1, 9):
        filler = _place(db, menu, f"U_F{i}", f"f-{i}", CART_60)
        db.mark_order_done(filler.id)
        _force_payment(db, filler.id, "cash", "2026-11-23T12:00:00+07:00")
    o9 = _place(db, menu, "U_Y", "y-9", CART_80)
    assert o9.queue_number == 9
    db.mark_order_done(o9.id)

    clock.now = datetime(2026, 11, 24, 12, 0, tzinfo=BKK)
    _place(db, menu, "U_Q", "t-1", CART_60)  # #1 still queued: not in ค้างจ่าย
    o2 = _place(db, menu, "U_T", "t-2", CART_70)
    db.mark_order_done(o2.id)

    state = client.get(f"/api/kitchen/state{KEY}").json()
    unpaid = state["orders"]["unpaid"]
    assert [(u["queue_number"], u["is_today"], u["day_label"]) for u in unpaid] == [
        (2, True, "วันนี้"),
        (9, False, "เมื่อวาน"),
    ]
    assert all(u["payment_status"] == "unpaid" for u in unpaid)
    # Queued orders carry their payment status for the badge
    assert state["orders"]["queued"][0]["payment_status"] == "unpaid"

    r = client.post(f"/api/kitchen/orders/{o2.id}/payment{KEY}", json={"status": "cash"})
    assert r.status_code == 200
    state2 = client.get(f"/api/kitchen/state{KEY}").json()
    assert [u["queue_number"] for u in state2["orders"]["unpaid"]] == [9]


def test_owner_cancel_of_paid_order_pushes_refund_and_summary_lists_refund_due(
    tmp_path: Path,
) -> None:
    menu = load_menu(MENU_PATH)
    clock = Clock(datetime(2026, 11, 24, 12, 0, tzinfo=BKK))
    db = ShopDB(tmp_path / "shop.db", clock=clock)
    spy = SpyLineClient()
    cfg = _settings(db.db_path)
    client = TestClient(create_app(settings=cfg, db=db, menu=menu, line_client=spy))

    for i in range(1, 5):
        _place(db, menu, f"U_{i}", f"c-{i}", CART_60)
    o5 = _place(db, menu, "U_5", "c-5", CART_70)
    assert o5.queue_number == 5
    _force_payment(db, o5.id, "slip", "2026-11-24T11:00:00+07:00")

    r = client.post(f"/api/kitchen/orders/{o5.id}/cancel{KEY}", json={"reason": "ของหมด"})
    assert r.status_code == 200
    assert spy.pushes == [("U_5", "ขออภัยค่ะ ออเดอร์ #5 ถูกยกเลิก (ของหมด) ร้านจะติดต่อคืนเงินค่ะ")]

    # An unpaid cancel gets no refund line
    o1 = db.get_customer_orders_today("U_1")[0]
    client.post(f"/api/kitchen/orders/{o1.id}/cancel{KEY}", json={"reason": "ของหมด"})
    assert spy.pushes[1] == ("U_1", "ขออภัยค่ะ ออเดอร์ #1 ถูกยกเลิก (ของหมด)")

    summary = handle_owner_text(db, menu, cfg, "สรุปวันนี้")
    assert "• ต้องคืนเงิน #5 70฿" in summary
    assert "ต้องคืนเงิน #1" not in summary


def test_bot_cancels_unpaid_but_hands_over_paid_cancel_request(tmp_path: Path) -> None:
    menu = load_menu(MENU_PATH)
    clock = Clock(datetime(2026, 11, 24, 12, 0, tzinfo=BKK))
    db = ShopDB(tmp_path / "shop.db", clock=clock)
    cfg = _settings(db.db_path)

    unpaid = _place(db, menu, "U_A", "a-1", CART_70)
    stub = StubLLM(
        [
            _resp(tool_calls=[_tool_call("x1", "cancel_order", {"order_id": unpaid.id})]),
            _resp(content="ยกเลิกออเดอร์ #1 แล้วค่ะ"),
        ]
    )
    t1 = handle_customer_text(db, menu, "U_A", "ยกเลิก #1", llm_client=stub, settings=cfg)
    assert t1.reply_text == "ยกเลิกออเดอร์ #1 แล้วค่ะ"
    assert db.get_order(unpaid.id).status == "cancelled"  # type: ignore[union-attr]

    paid = _place(db, menu, "U_B", "b-1", CART_70)
    assert db.set_payment_status(paid.id, "cash")[1] is None
    stub2 = StubLLM([_resp(tool_calls=[_tool_call("x2", "cancel_order", {"order_id": paid.id})])])
    t2 = handle_customer_text(db, menu, "U_B", "ขอยกเลิก #2", llm_client=stub2, settings=cfg)
    assert t2.reason == "hand_over"
    assert t2.reply_text == HANDOVER_REPLY
    assert len(stub2.calls) == 1
    still = db.get_order(paid.id)
    assert still is not None and still.status == "queued" and still.payment_status == "cash"
    conv = db.get_or_create_conversation("U_B")
    assert conv.handed_over_until is not None
    assert db.is_handed_over(conv) is True
    cards = db.get_open_handovers()
    assert [(c["line_user_id"], c["reasons"]) for c in cards] == [("U_B", ["paid_cancel_request"])]
    assert cards[0]["details"][0]["detail"] == f"#{paid.queue_number} 70฿"

    # A done order that's paid is also handed over rather than refused
    done_paid = _place(db, menu, "U_D", "d-1", CART_60)
    db.mark_order_done(done_paid.id)
    db.set_payment_status(done_paid.id, "transfer_manual")
    stub3 = StubLLM(
        [_resp(tool_calls=[_tool_call("x3", "cancel_order", {"order_id": done_paid.id})])]
    )
    t3 = handle_customer_text(db, menu, "U_D", "ยกเลิก", llm_client=stub3, settings=cfg)
    assert t3.reply_text == HANDOVER_REPLY
    assert db.get_order(done_paid.id).status == "done"  # type: ignore[union-attr]


def test_done_push_shows_unpaid_balance_only_when_owed(tmp_path: Path) -> None:
    menu = load_menu(MENU_PATH)
    clock = Clock(datetime(2026, 11, 24, 12, 0, tzinfo=BKK))
    db = ShopDB(tmp_path / "shop.db", clock=clock)
    spy = SpyLineClient()
    client = TestClient(
        create_app(settings=_settings(db.db_path), db=db, menu=menu, line_client=spy)
    )

    for i in range(1, 5):
        _place(db, menu, f"U_{i}", f"c-{i}", CART_60)
    o5 = _place(db, menu, "U_5", "c-5", CART_70)
    r = client.post(f"/api/kitchen/orders/{o5.id}/done{KEY}", json={"notify": True})
    assert r.status_code == 200
    assert spy.pushes[-1] == ("U_5", "ออเดอร์ #5 เสร็จแล้ว มารับได้เลยค่ะ · ยอดค้าง 70฿")

    o6 = _place(db, menu, "U_6", "c-6", CART_60)
    db.set_payment_status(o6.id, "transfer_manual")
    client.post(f"/api/kitchen/orders/{o6.id}/done{KEY}", json={"notify": True})
    assert spy.pushes[-1] == ("U_6", "ออเดอร์ #6 เสร็จแล้ว มารับได้เลยค่ะ")

    # The balance covers earlier shop days, not just this order
    clock.now = datetime(2026, 11, 25, 12, 0, tzinfo=BKK)
    o_next = _place(db, menu, "U_5", "c-next", CART_60)
    client.post(f"/api/kitchen/orders/{o_next.id}/done{KEY}", json={"notify": True})
    assert spy.pushes[-1] == ("U_5", "ออเดอร์ #1 เสร็จแล้ว มารับได้เลยค่ะ · ยอดค้าง 130฿")


def test_daily_summary_payment_lines(tmp_path: Path) -> None:
    menu = load_menu(MENU_PATH)
    clock = Clock(datetime(2026, 11, 23, 12, 0, tzinfo=BKK))
    db = ShopDB(tmp_path / "shop.db", clock=clock)
    cfg = _settings(db.db_path)

    yesterday = _place(db, menu, "U_Y", "y-1", CART_80)
    db.mark_order_done(yesterday.id)
    paid_late = _place(db, menu, "U_L", "y-2", CART_60)  # yesterday's order, paid in cash today
    db.mark_order_done(paid_late.id)

    clock.now = datetime(2026, 11, 24, 12, 0, tzinfo=BKK)
    slip = _place(db, menu, "U_S", "t-1", CART_130)
    _force_payment(db, slip.id, "slip", "2026-11-24T12:05:00+07:00")
    cash = _place(db, menu, "U_C", "t-2", CART_60)
    db.set_payment_status(cash.id, "cash")
    unpaid = _place(db, menu, "U_U", "t-3", CART_70)
    assert (slip.total, cash.total, unpaid.total) == (130, 60, 70)

    summary = handle_owner_text(db, menu, cfg, "สรุปวันนี้")
    assert "• รับโอน (สลิป): 130 บาท" in summary
    assert "• รับเงินสด/ร้านยืนยันโอน: 60 บาท" in summary

    # A late cash payment for yesterday's order counts as money received today
    db.set_payment_status(paid_late.id, "cash")
    summary = handle_owner_text(db, menu, cfg, "สรุปวันนี้")
    assert "• รับเงินสด/ร้านยืนยันโอน: 120 บาท" in summary
    assert f"• ยังไม่จ่าย: #{unpaid.queue_number} 70฿" in summary
    assert "• ค้างจ่ายจากวันก่อน 1 ออเดอร์ 80฿" in summary
    assert "ต้องคืนเงิน" not in summary


def test_done_with_cash_marks_paid_before_the_push(tmp_path: Path) -> None:
    """Ticket 7: ทำเสร็จแล้ว on an unpaid order asks รับเงินสดแล้ว / ยังไม่จ่าย."""
    menu = load_menu(MENU_PATH)
    db = ShopDB(tmp_path / "shop.db", clock=Clock(datetime(2026, 11, 24, 12, 0, tzinfo=BKK)))
    spy = SpyLineClient()
    client = TestClient(
        create_app(settings=_settings(db.db_path), db=db, menu=menu, line_client=spy)
    )
    o1 = _place(db, menu, "U_A", "c-1", CART_70)
    r = client.post(
        f"/api/kitchen/orders/{o1.id}/done{KEY}", json={"notify": True, "paid_cash": True}
    )
    assert r.status_code == 200
    assert r.json()["order"]["payment_status"] == "cash"
    assert spy.pushes == [("U_A", "ออเดอร์ #1 เสร็จแล้ว มารับได้เลยค่ะ")]

    # ยังไม่จ่าย: done stays unpaid and lands in ค้างจ่าย
    o2 = _place(db, menu, "U_B", "c-2", CART_60)
    client.post(f"/api/kitchen/orders/{o2.id}/done{KEY}", json={"notify": False})
    unpaid = client.get(f"/api/kitchen/state{KEY}").json()["orders"]["unpaid"]
    assert [u["queue_number"] for u in unpaid] == [2]

    # paid_cash never overrides a slip payment
    o3 = _place(db, menu, "U_C", "c-3", CART_60)
    _force_payment(db, o3.id, "slip", "2026-11-24T12:00:00+07:00")
    r3 = client.post(f"/api/kitchen/orders/{o3.id}/done{KEY}", json={"paid_cash": True})
    assert r3.json()["order"]["payment_status"] == "slip"


def test_cash_done_keeps_a_payment_recorded_meanwhile_and_reports_other_debt(
    tmp_path: Path,
) -> None:
    """Review fixes: done + cash is one transaction that never overwrites a payment, and the
    push still reports the customer's other unpaid orders (SPEC §3.1 item 10)."""
    menu = load_menu(MENU_PATH)
    db = ShopDB(tmp_path / "shop.db", clock=Clock(datetime(2026, 11, 24, 12, 0, tzinfo=BKK)))
    spy = SpyLineClient()
    cfg = _settings(db.db_path)
    client = TestClient(create_app(settings=cfg, db=db, menu=menu, line_client=spy))

    o1 = _place(db, menu, "U_A", "c-1", CART_60)
    o2 = _place(db, menu, "U_A", "c-2", CART_70)
    r = client.post(
        f"/api/kitchen/orders/{o2.id}/done{KEY}", json={"notify": True, "paid_cash": True}
    )
    assert r.json()["order"]["payment_status"] == "cash"
    assert spy.pushes[-1] == ("U_A", "ออเดอร์ #2 เสร็จแล้ว มารับได้เลยค่ะ · ยอดค้าง 60฿")

    # The bot's next turn sees the new balance (AGENTS.md out-of-band rule)
    stub = StubLLM([_resp(content="ยอดค้าง 60 บาทค่ะ")])
    handle_customer_text(db, menu, "U_A", "ค้างเท่าไหร่", llm_client=stub, settings=cfg)
    assert "| ยอดค้างชำระ=60 บาท" in stub.calls[0]["messages"][1]["content"]

    # A transfer recorded before the done tap is kept, and the response shows the real row
    db.set_payment_status(o1.id, "transfer_manual")
    r1 = client.post(f"/api/kitchen/orders/{o1.id}/done{KEY}", json={"paid_cash": True})
    assert r1.json()["order"]["payment_status"] == "transfer_manual"
    assert db.get_order(o1.id).payment_status == "transfer_manual"  # type: ignore[union-attr]


def test_done_and_cancel_pushes_are_in_the_bots_next_history(tmp_path: Path) -> None:
    """Full review #8: the customer saw these pushes, so the LLM must see them too."""
    menu = load_menu(MENU_PATH)
    db = ShopDB(tmp_path / "shop.db", clock=Clock(datetime(2026, 11, 24, 12, 0, tzinfo=BKK)))
    spy = SpyLineClient()
    cfg = _settings(db.db_path)
    client = TestClient(create_app(settings=cfg, db=db, menu=menu, line_client=spy))
    o1 = _place(db, menu, "U_A", "c-1", CART_60)
    o2 = _place(db, menu, "U_A", "c-2", CART_70)
    client.post(f"/api/kitchen/orders/{o1.id}/done{KEY}", json={"notify": True})
    client.post(f"/api/kitchen/orders/{o2.id}/cancel{KEY}", json={"reason": "ของหมด"})
    stub = StubLLM([_resp(content="ค่ะ")])
    handle_customer_text(db, menu, "U_A", "เสร็จยัง", llm_client=stub, settings=cfg)
    sent = stub.calls[0]["messages"]
    assert sent[-5:] == [
        {"role": "user", "content": "[ร้านแจ้งว่าออเดอร์ #1 เสร็จแล้ว]"},
        {"role": "assistant", "content": "ออเดอร์ #1 เสร็จแล้ว มารับได้เลยค่ะ · ยอดค้าง 130฿"},
        {"role": "user", "content": "[ร้านยกเลิกออเดอร์ #2]"},
        {"role": "assistant", "content": "ขออภัยค่ะ ออเดอร์ #2 ถูกยกเลิก (ของหมด)"},
        {"role": "user", "content": "เสร็จยัง"},
    ]
