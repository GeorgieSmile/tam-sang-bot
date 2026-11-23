"""Behavioral tests for SQLite schedule overrides, order confirmation, and /kitchen API."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import ShopDB
from app.main import create_app
from app.menu import load_menu

MENU_PATH = Path(__file__).resolve().parent.parent / "menu.yaml"
BKK = ZoneInfo("Asia/Bangkok")


class SpyLineClient:
    def __init__(self) -> None:
        self.replies: list[tuple[str, list[dict[str, object]]]] = []
        self.sent: list[tuple[str, str]] = []
        self.loadings: list[str] = []
        self.fail_push: bool = False

    def reply_messages(self, reply_token: str, messages: list[dict[str, object]]) -> None:
        self.replies.append((reply_token, messages))

    def push_text(self, user_id: str, text: str) -> None:
        if self.fail_push:
            raise RuntimeError("LINE 500")
        self.sent.append((user_id, text))

    def show_loading(self, user_id: str, seconds: int = 20) -> None:
        self.loadings.append(user_id)


def _make_settings(db_path: Path) -> Settings:
    return Settings(
        db_path=db_path,
        menu_path=MENU_PATH,
        kitchen_secret="secret-123",
        public_base_url="http://localhost:8000",
        payment_info_text="PromptPay 081-234-5678",
        line_channel_secret="line-secret",
        line_channel_access_token="line-token",
        owner_line_user_id="U_OWNER",
        llm_base_url="https://openrouter.ai/api/v1",
        llm_api_key="test-key",
        llm_model="anthropic/claude-haiku-5.5",
        llm_reasoning_effort="low",
    )


def _stage_card(
    db: ShopDB,
    user_id: str,
    card_id: str,
    raw_items: list[dict[str, object]],
    *,
    fulfillment: str = "delivery",
    address: str | None = "บ้าน 45/2",
    location_url: str | None = "https://maps.google.com/?q=13.75,100.5",
    total: int = 80,
) -> None:
    conv = db.get_or_create_conversation(user_id)
    rev = db.save_draft(
        conv.id,
        {
            "items": raw_items,
            "fulfillment": fulfillment,
            "address": address,
            "location_url": location_url,
        },
    )
    db.set_active_card(
        conv.id,
        card_id,
        {
            "card_id": card_id,
            "draft_revision": rev,
            "raw_items": raw_items,
            "fulfillment": fulfillment,
            "address": address,
            "location_url": location_url,
            "requested_time": None,
            "total": total,
        },
    )


def test_schedule_and_same_day_override_reset_at_midnight(tmp_path: Path) -> None:
    menu = load_menu(MENU_PATH)
    now = [datetime(2026, 11, 23, 21, 0, tzinfo=BKK)]
    db = ShopDB(tmp_path / "shop.db", clock=lambda: now[0])

    # 21:00 outside 09:00-20:00 -> closed
    assert db.get_shop_status(menu).is_open is False

    # Manual open override + busy mode apply for today
    db.set_open_override("open")
    db.set_busy_mode(True, 25)
    st = db.get_shop_status(menu)
    assert st.is_open is True
    assert st.busy_mode is True
    assert st.wait_minutes == 25

    # Next day 00:01 -> both open_override and busy_mode expire automatically
    now[0] = datetime(2026, 11, 24, 0, 1, tzinfo=BKK)
    st_next = db.get_shop_status(menu)
    assert st_next.is_open is False
    assert st_next.open_override is None
    assert st_next.busy_mode is False
    assert st_next.wait_minutes is None


def test_confirm_idempotency_queue_numbers_and_returning_address(tmp_path: Path) -> None:
    menu = load_menu(MENU_PATH)
    now = datetime(2026, 11, 23, 12, 0, tzinfo=BKK)
    db = ShopDB(tmp_path / "shop.db", clock=lambda: now)

    raw_items = [{"dish_id": "kaphrao", "qty": 1, "option_ids": ["crispy_pork", "fried_egg_runny"]}]
    _stage_card(db, "U_CUST_1", "card-1", raw_items, total=80)

    first = db.confirm_card("U_CUST_1", "card-1", menu)
    assert first.ok is True
    assert first.already_confirmed is False
    assert first.order is not None
    assert first.order.queue_number == 1
    assert first.order.total == 80
    assert first.order.status == "queued"

    # Draft and active card are cleared after confirm
    conv_after = db.get_or_create_conversation("U_CUST_1")
    assert conv_after.draft == {}
    assert conv_after.active_card_id is None

    # Returning customer address + location pin saved
    cust = db.get_customer("U_CUST_1")
    assert cust is not None
    assert cust.last_address == "บ้าน 45/2"
    assert cust.last_location_url == "https://maps.google.com/?q=13.75,100.5"

    # Double tap confirm -> idempotent, no duplicate order
    second = db.confirm_card("U_CUST_1", "card-1", menu)
    assert second.ok is True
    assert second.already_confirmed is True
    assert second.order is not None
    assert second.order.id == first.order.id
    assert second.order.queue_number == 1

    # Second order gets queue #2
    _stage_card(db, "U_CUST_2", "card-2", raw_items, fulfillment="pickup", address=None, total=70)
    third = db.confirm_card("U_CUST_2", "card-2", menu)
    assert third.ok is True
    assert third.order is not None
    assert third.order.queue_number == 2


def test_confirm_refuses_sold_out_and_stale_revision(tmp_path: Path) -> None:
    menu = load_menu(MENU_PATH)
    now = datetime(2026, 11, 23, 12, 0, tzinfo=BKK)
    db = ShopDB(tmp_path / "shop.db", clock=lambda: now)

    raw_items = [{"dish_id": "kaphrao", "qty": 1, "option_ids": ["crispy_pork"]}]
    _stage_card(db, "U_CUST", "card-sold", raw_items, fulfillment="pickup", address=None, total=60)
    db.set_override("ingredient", "crispy_pork", False)

    res = db.confirm_card("U_CUST", "card-sold", menu)
    assert res.ok is False
    assert res.error is not None and "หมูกรอบ" in res.error

    # Stale draft revision refused -> 0 orders created, clear Thai error
    db.clear_all_overrides()
    _stage_card(db, "U_CUST", "card-stale", raw_items, fulfillment="pickup", address=None, total=60)
    conv = db.get_or_create_conversation("U_CUST")
    db.save_draft(conv.id, {"items": []})
    stale_res = db.confirm_card("U_CUST", "card-stale", menu)
    assert stale_res.ok is False
    assert stale_res.error is not None and "หมดอายุ" in stale_res.error
    assert db.get_kitchen_orders_today()["stats"]["queued_count"] == 0

    # Closed shop refused on confirm
    _stage_card(
        db, "U_CUST", "card-closed", raw_items, fulfillment="pickup", address=None, total=60
    )
    db.set_open_override("closed")
    closed_res = db.confirm_card("U_CUST", "card-closed", menu)
    assert closed_res.ok is False
    assert closed_res.error is not None and "ปิด" in closed_res.error
    assert db.get_kitchen_orders_today()["stats"]["queued_count"] == 0


def test_kitchen_auth_and_order_actions(tmp_path: Path) -> None:
    menu = load_menu(MENU_PATH)
    now = datetime(2026, 11, 23, 12, 0, tzinfo=BKK)
    db = ShopDB(tmp_path / "shop.db", clock=lambda: now)
    spy = SpyLineClient()
    app = create_app(
        settings=_make_settings(tmp_path / "shop.db"),
        db=db,
        menu=menu,
        line_client=spy,
    )
    client = TestClient(app)

    assert client.get("/health").json() == {"ok": True}
    assert client.get("/kitchen").status_code == 403
    assert client.get("/api/kitchen/state").status_code == 403
    assert client.get("/kitchen?key=secret-123").status_code == 200

    # Create 3 queued orders
    raw_items = [{"dish_id": "kaphrao", "qty": 1, "option_ids": ["chicken"]}]
    for i in range(1, 4):
        _stage_card(
            db,
            f"U_{i}",
            f"c-{i}",
            raw_items,
            fulfillment="pickup" if i != 2 else "delivery",
            address=None if i != 2 else "บ้าน 12",
            total=50 if i != 2 else 60,
        )
        assert db.confirm_card(f"U_{i}", f"c-{i}", menu).ok

    state = client.get("/api/kitchen/state?key=secret-123").json()
    assert [o["queue_number"] for o in state["orders"]["queued"]] == [1, 2, 3]

    # Order 1: done without notify -> 0 pushes
    r1 = client.post("/api/kitchen/orders/1/done?key=secret-123", json={"notify": False})
    assert r1.status_code == 200
    assert r1.json()["order"]["status"] == "done"
    assert spy.sent == []

    # Order 2: done with notify=True -> 1 push
    r2 = client.post("/api/kitchen/orders/2/done?key=secret-123", json={"notify": True})
    assert r2.status_code == 200
    assert len(spy.sent) == 1
    assert spy.sent[0][0] == "U_2"
    assert "#2" in spy.sent[0][1]

    # Blank cancel reason rejected with 422
    blank = client.post("/api/kitchen/orders/3/cancel?key=secret-123", json={"reason": "   "})
    assert blank.status_code == 422

    # Order 3: cancel with reason -> 1 push containing reason
    r3 = client.post("/api/kitchen/orders/3/cancel?key=secret-123", json={"reason": "ของหมด"})
    assert r3.status_code == 200
    assert r3.json()["order"]["status"] == "cancelled"
    assert r3.json()["notified"] is True
    assert len(spy.sent) == 2
    assert spy.sent[1][0] == "U_3"
    assert "ของหมด" in spy.sent[1][1]

    # Push failure on cancel still returns 200 with notified=False (never 500 after DB commit)
    _stage_card(db, "U_4", "c-4", raw_items, fulfillment="pickup", address=None, total=50)
    assert db.confirm_card("U_4", "c-4", menu).ok

    spy.fail_push = True
    r4 = client.post("/api/kitchen/orders/4/cancel?key=secret-123", json={"reason": "ของหมด"})
    assert r4.status_code == 200
    assert r4.json()["order"]["status"] == "cancelled"
    assert r4.json()["notified"] is False

    # Stats check: 2 done (50 + 60 = 110), 2 cancelled
    final_state = client.get("/api/kitchen/state?key=secret-123").json()
    assert final_state["orders"]["stats"] == {
        "queued_count": 0,
        "done_count": 2,
        "cancelled_count": 2,
        "done_total": 110,
    }
