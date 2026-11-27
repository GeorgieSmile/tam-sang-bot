"""Behavioral tests for v2 Ticket 5: wait estimate, queue position, batch view."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.agent import build_card_snapshot_for_conv, handle_customer_text
from app.config import Settings
from app.db import Order, ShopDB
from app.main import create_app
from app.menu import MenuError, WaitConfig, load_menu
from app.owner import handle_owner_text
from app.wait import card_wait_line, range_text, wait_range

MENU_PATH = Path(__file__).resolve().parent.parent / "menu.yaml"
BKK = ZoneInfo("Asia/Bangkok")
SECRET = "test-channel-secret"
KEY = "?key=secret-123"
T0 = datetime(2026, 11, 23, 12, 0, tzinfo=BKK)
KAPHRAO = {"dish_id": "kaphrao", "option_ids": ["crispy_pork", "fried_egg_runny"]}


def kaphrao(qty: int, note: str = "") -> dict[str, Any]:
    return {**KAPHRAO, "qty": qty, "note": note}


class SpyLineClient:
    def __init__(self) -> None:
        self.replies: list[tuple[str, list[dict[str, Any]]]] = []

    def reply_messages(self, reply_token: str, messages: list[dict[str, Any]]) -> None:
        self.replies.append((reply_token, messages))

    def push_text(self, user_id: str, text: str) -> None:
        return None

    def show_loading(self, user_id: str, seconds: int = 20) -> None:
        return None

    def get_profile(self, user_id: str) -> str | None:
        return None

    def get_message_content(self, message_id: str) -> bytes:
        return b""


class StubLLM:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.responses.pop(0)


def _resp(content: str = "", tool_calls: list[Any] | None = None) -> Any:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=tool_calls))]
    )


def _settings(db_path: Path) -> Settings:
    return Settings(
        db_path=db_path,
        menu_path=MENU_PATH,
        kitchen_secret="secret-123",
        public_base_url="https://shop.example",
        payment_info_text="",
        line_channel_secret=SECRET,
        line_channel_access_token="token",
        owner_line_user_id="U_OWNER",
        llm_base_url="https://openrouter.ai/api/v1",
        llm_api_key="test-key",
        llm_model="anthropic/claude-haiku-5.5",
        llm_reasoning_effort="low",
    )


def _stage(db: ShopDB, user_id: str, items: list[Any], fulfillment: str = "pickup") -> str:
    conv = db.get_or_create_conversation(user_id)
    draft: dict[str, Any] = {"items": items, "fulfillment": fulfillment}
    if fulfillment == "delivery":
        draft["address"] = "บ้าน 1"
    db.save_draft(conv.id, draft)
    snap, err = build_card_snapshot_for_conv(
        db, db.get_or_create_conversation(user_id), load_menu(MENU_PATH)
    )
    assert snap is not None, err
    return str(snap["card_id"])


def _place(db: ShopDB, user_id: str, items: list[Any]) -> Order:
    res = db.confirm_card(user_id, _stage(db, user_id, items), load_menu(MENU_PATH))
    assert res.ok and res.order is not None
    return res.order


def _card_line(db: ShopDB, user_id: str, items: list[Any]) -> str:
    _stage(db, user_id, items)
    snap = db.get_or_create_conversation(user_id).active_card
    assert snap is not None
    return card_wait_line(db, load_menu(MENU_PATH), snap)


def test_wait_settings_must_be_whole_numbers(tmp_path: Path) -> None:
    raw = MENU_PATH.read_text(encoding="utf-8")
    assert "  minutes_per_box: 3\n" in raw
    bad = tmp_path / "menu.yaml"
    bad.write_text(
        raw.replace("  minutes_per_box: 3\n", "  minutes_per_box: 3.5\n"), encoding="utf-8"
    )
    with pytest.raises(MenuError):
        load_menu(bad)
    assert load_menu(MENU_PATH).wait == WaitConfig(minutes_per_box=3, min_wait=10)


def test_range_rounds_down_to_5_minutes() -> None:
    assert [wait_range(m) for m in (10, 24, 39)] == [(10, 15), (20, 25), (35, 40)]
    assert range_text(10, delivery=True) == "ประมาณ 10–15 นาที (ไม่รวมเวลาส่ง)"


def test_card_estimate_from_meal_boxes_queued_and_in_the_card(tmp_path: Path) -> None:
    db = ShopDB(tmp_path / "shop.db", clock=lambda: T0)
    assert _card_line(db, "U_C", [kaphrao(1)]) == "ตอนนี้รอประมาณ 10–15 นาที"

    _place(db, "U_Q1", [kaphrao(6)])  # 6 meal boxes queued
    drink = {"dish_id": "water", "qty": 1, "option_ids": []}
    # (6 + 2) x 3 = 24 -> 20–25; the drink counts 0
    assert _card_line(db, "U_C", [kaphrao(2), drink]) == "ตอนนี้รอประมาณ 20–25 นาที"

    _place(db, "U_Q2", [kaphrao(6)])  # 12 queued
    # (12 + 1) x 3 = 39 -> 35–40
    assert _card_line(db, "U_C", [kaphrao(1)]) == "ตอนนี้รอประมาณ 35–40 นาที"


def test_busy_mode_sets_a_minimum_wait(tmp_path: Path) -> None:
    menu = load_menu(MENU_PATH)
    db = ShopDB(tmp_path / "shop.db", clock=lambda: T0)
    cfg = _settings(db.db_path)
    handle_owner_text(db, menu, cfg, "ยุ่ง 25 นาที")
    assert _card_line(db, "U_C", [kaphrao(1)]) == "ตอนนี้รอประมาณ 25–30 นาที"
    _place(db, "U_Q1", [kaphrao(12)])
    assert _card_line(db, "U_C", [kaphrao(1)]) == "ตอนนี้รอประมาณ 35–40 นาที"

    db2 = ShopDB(tmp_path / "b.db", clock=lambda: T0)
    handle_owner_text(db2, menu, cfg, "ยุ่ง 25 นาที")
    handle_owner_text(db2, menu, cfg, "โหมดปกติ")
    assert _card_line(db2, "U_C", [kaphrao(1)]) == "ตอนนี้รอประมาณ 10–15 นาที"


def test_receipt_shows_queue_position_and_delivery_note(tmp_path: Path) -> None:
    menu = load_menu(MENU_PATH)
    db = ShopDB(tmp_path / "shop.db", clock=lambda: T0)
    spy = SpyLineClient()
    client = TestClient(
        create_app(settings=_settings(db.db_path), db=db, menu=menu, line_client=spy)
    )
    for i in range(3):
        _place(db, f"U_Q{i}", [kaphrao(1)])

    def confirm(user_id: str, fulfillment: str) -> str:
        card_id = _stage(db, user_id, [kaphrao(1)], fulfillment)
        event = {
            "webhookEventId": f"evt-{user_id}",
            "type": "postback",
            "replyToken": f"rt-{user_id}",
            "source": {"type": "user", "userId": user_id},
            "postback": {"data": f"action=confirm&card_id={card_id}"},
        }
        raw = json.dumps({"events": [event]}).encode()
        sig = base64.b64encode(hmac.new(SECRET.encode(), raw, hashlib.sha256).digest()).decode()
        client.post(
            "/line/webhook",
            content=raw,
            headers={"Content-Type": "application/json", "x-line-signature": sig},
        )
        text: str = spy.replies[-1][1][0]["text"]
        return text

    # 3 queued (3 boxes) + this 1 box = 12 min -> 10–15
    assert "\nคิวที่ 4 · ประมาณ 10–15 นาที" in confirm("U_A", "pickup")
    assert "\nคิวที่ 5 · ประมาณ 15–20 นาที (ไม่รวมเวลาส่ง)" in confirm("U_B", "delivery")


def test_get_my_orders_returns_queue_position_and_range(tmp_path: Path) -> None:
    menu = load_menu(MENU_PATH)
    db = ShopDB(tmp_path / "shop.db", clock=lambda: T0)
    _place(db, "U_Q1", [kaphrao(2)])
    _place(db, "U_Q2", [kaphrao(2)])
    mine = _place(db, "U_A", [kaphrao(1)])
    call = SimpleNamespace(id="g1", function=SimpleNamespace(name="get_my_orders", arguments="{}"))
    llm = StubLLM(
        [_resp(tool_calls=[call]), _resp(content="ยังไม่เสร็จค่ะ ก่อนหน้าอีก 2 คิว ประมาณ 15–20 นาที")]
    )
    handle_customer_text(db, menu, "U_A", "เสร็จยัง", llm_client=llm, settings=_settings(db.db_path))
    result = json.loads(llm.calls[1]["messages"][-1]["content"])
    entry = result["orders"][0]
    assert entry["order_id"] == mine.id
    # (2 + 2 + 1) x 3 = 15 -> 15–20
    assert (entry["queue_position"], entry["orders_ahead"], entry["wait_range_minutes"]) == (
        3,
        2,
        "15–20",
    )
    # The live-state message carries the estimate instead of the busy-only text
    live = llm.calls[0]["messages"][1]["content"]
    assert "| สั่ง 1 กล่องตอนนี้รอประมาณ 15–20 นาที |" in live


def test_batch_view_groups_identical_dishes_with_notes(tmp_path: Path) -> None:
    menu = load_menu(MENU_PATH)
    db = ShopDB(tmp_path / "shop.db", clock=lambda: T0)
    client = TestClient(
        create_app(settings=_settings(db.db_path), db=db, menu=menu, line_client=SpyLineClient())
    )
    other = {"dish_id": "omelette_rice", "qty": 1, "option_ids": []}
    orders = {}
    for q in range(1, 8):
        items = [kaphrao(1, "ไม่ใส่ผัก" if q == 4 else "")] if q in (2, 4, 7) else [other]
        orders[q] = _place(db, f"U_{q}", items)
    assert db.mark_order_done(orders[1].id) is not None
    assert db.cancel_order_by_owner(orders[3].id, "ของหมด") is not None

    batch = client.get(f"/api/kitchen/state{KEY}").json()["orders"]["batch"]
    group = next(g for g in batch if g["dish_id"] == "kaphrao")
    assert group["qty"] == 3
    assert group["orders"] == [2, 4, 7]
    assert group["notes"] == [{"queue_number": 4, "ref": "#4", "note": "ไม่ใส่ผัก"}]
    assert group["refs"] == ["#2", "#4", "#7"]
    assert group["option_names_th"] == ["หมูกรอบ", "ไข่ดาวไม่สุก", "เผ็ดปกติ"]
    omelette = next(g for g in batch if g["dish_id"] == "omelette_rice")
    assert omelette["orders"] == [5, 6]  # done #1 and cancelled #3 excluded


def test_normal_mode_flex_card_and_full_live_state(tmp_path: Path) -> None:
    menu = load_menu(MENU_PATH)
    db = ShopDB(tmp_path / "shop.db", clock=lambda: T0)
    _place(db, "U_Q1", [kaphrao(3)])  # 3 boxes queued

    def call(name: str, args: dict[str, Any], cid: str) -> Any:
        return SimpleNamespace(
            id=cid,
            function=SimpleNamespace(name=name, arguments=json.dumps(args, ensure_ascii=False)),
        )

    llm = StubLLM(
        [
            _resp(
                tool_calls=[
                    call("update_cart", {"action": "add", "items": [kaphrao(1)]}, "a"),
                    call("set_fulfillment", {"fulfillment": "pickup"}, "b"),
                    call("build_order_card", {}, "c"),
                ]
            ),
            _resp(content="กดยืนยันบนการ์ดได้เลยค่ะ"),
        ]
    )
    turn = handle_customer_text(
        db, menu, "U_A", "กะเพรา 1", llm_client=llm, settings=_settings(db.db_path)
    )
    assert turn.flex_card is not None
    body = [
        c.get("text")
        for c in turn.flex_card["contents"]["body"]["contents"]
        if c.get("type") == "text"
    ]
    # (3 + 1) x 3 = 12 -> 10–15, shown in normal (not busy) mode
    assert body[1] == "ตอนนี้รอประมาณ 10–15 นาที"
    assert llm.calls[0]["messages"][1] == {
        "role": "system",
        "content": (
            "สถานะระบบล่าสุดตอนนี้ (ยึดข้อมูลนี้เหนือข้อความเก่าในแชท): "
            "ร้าน=เปิดรับออเดอร์ | สั่ง 1 กล่องตอนนี้รอประมาณ 10–15 นาที "
            "| ของหมดตอนนี้=ไม่มี (ทุกเมนูและวัตถุดิบพร้อมขาย) | ตะกร้าปัจจุบัน=ว่าง (0 รายการ) "
            "| ยอดค้างชำระ=0 บาท"
        ),
    }


def test_the_queue_includes_earlier_days_still_queued(tmp_path: Path) -> None:
    clock = {"now": T0}
    db = ShopDB(tmp_path / "shop.db", clock=lambda: clock["now"])
    _place(db, "U_OLD", [kaphrao(2)])  # yesterday, still not cooked
    clock["now"] = T0.replace(day=24)
    mine = _place(db, "U_A", [kaphrao(1)])
    assert mine.queue_number == 1
    from app.wait import queue_info

    info = queue_info(db, load_menu(MENU_PATH), mine)
    assert info is not None
    # (2 + 1) x 3 = 9 -> min 10 -> 10–15
    assert (info.position, info.orders_ahead, info.range_str()) == (2, 1, "10–15")


def test_wait_estimate_is_never_pushed(tmp_path: Path) -> None:
    menu = load_menu(MENU_PATH)
    db = ShopDB(tmp_path / "shop.db", clock=lambda: T0)
    pushes: list[tuple[str, str]] = []
    spy = SpyLineClient()
    spy.push_text = lambda user_id, text: pushes.append((user_id, text))  # type: ignore[method-assign]
    client = TestClient(
        create_app(settings=_settings(db.db_path), db=db, menu=menu, line_client=spy)
    )
    o = _place(db, "U_A", [kaphrao(1)])
    _place(db, "U_B", [kaphrao(1)])
    client.post(f"/api/kitchen/orders/{o.id}/done{KEY}", json={"notify": True})
    assert pushes == [("U_A", "ออเดอร์ #1 เสร็จแล้ว มารับได้เลยค่ะ · ยอดค้าง 70฿")]
    assert all("นาที" not in text for _, text in pushes)


def test_pre_v2_orders_never_count_but_are_listed_for_the_owner(tmp_path: Path) -> None:
    """Ticket 7: v1 orders left `queued` inflated every queue number in the Ticket 6 test."""
    import sqlite3

    from app.wait import queue_info

    menu = load_menu(MENU_PATH)
    clock = {"now": T0}
    db = ShopDB(tmp_path / "shop.db", clock=lambda: clock["now"])
    client = TestClient(
        create_app(settings=_settings(db.db_path), db=db, menu=menu, line_client=SpyLineClient())
    )
    old = [_place(db, f"U_OLD{i}", [kaphrao(2)]) for i in range(4)]  # 8 boxes, 23 Nov
    leftover = _place(db, "U_LATE", [kaphrao(1)])  # a real v2 order nobody marked done
    conn = sqlite3.connect(db.db_path)
    conn.executemany(
        "UPDATE orders SET payment_status = 'legacy' WHERE id = ?", [(o.id,) for o in old]
    )
    conn.commit()
    conn.close()

    clock["now"] = T0.replace(day=24)
    mine = _place(db, "U_A", [kaphrao(1)])
    info = queue_info(db, menu, mine)
    assert info is not None
    # Only the v2 leftover is ahead: (1 + 1) x 3 = 6 -> min 10 -> 10–15
    assert (info.position, info.orders_ahead, info.range_str()) == (2, 1, "10–15")
    assert _card_line(db, "U_B", [kaphrao(1)]) == "ตอนนี้รอประมาณ 10–15 นาที"

    orders = client.get(f"/api/kitchen/state{KEY}").json()["orders"]
    # Earlier-day orders are listed first with a day label, so the owner can clear them
    assert [(o["queue_number"], o["day_label"], o["is_today"]) for o in orders["queued"]] == [
        (1, "เมื่อวาน", False),
        (2, "เมื่อวาน", False),
        (3, "เมื่อวาน", False),
        (4, "เมื่อวาน", False),
        (5, "เมื่อวาน", False),
        (1, "วันนี้", True),
    ]
    assert orders["stats"]["queued_count"] == 1  # สรุปวันนี้ still counts today only
    # รวมจาน never includes the pre-v2 leftovers
    # Oldest first, and queue numbers that repeat across days are told apart
    assert [(g["orders"], g["refs"]) for g in orders["batch"]] == [([5, 1], ["#5 (เมื่อวาน)", "#1"])]
    # The owner can clear a leftover from /kitchen
    res = client.post(f"/api/kitchen/orders/{old[0].id}/done{KEY}", json={"notify": False})
    assert res.status_code == 200
    assert leftover.queue_number == 5


def test_get_my_orders_includes_earlier_day_orders_still_queued(tmp_path: Path) -> None:
    """Full review #13: a customer asking about yesterday's still-queued order gets an answer."""
    menu = load_menu(MENU_PATH)
    clock = {"now": T0}
    db = ShopDB(tmp_path / "shop.db", clock=lambda: clock["now"])
    old = _place(db, "U_A", [kaphrao(1)])
    clock["now"] = T0.replace(day=24)
    call = SimpleNamespace(id="g1", function=SimpleNamespace(name="get_my_orders", arguments="{}"))
    llm = StubLLM([_resp(tool_calls=[call]), _resp(content="ยังไม่เสร็จค่ะ")])
    handle_customer_text(db, menu, "U_A", "เสร็จยัง", llm_client=llm, settings=_settings(db.db_path))
    orders = json.loads(llm.calls[1]["messages"][-1]["content"])["orders"]
    assert [(o["order_id"], o["shop_date"], o["queue_position"]) for o in orders] == [
        (old.id, "2026-11-23", 1)
    ]


def test_earlier_day_orders_stay_visible_for_status_and_undo(tmp_path: Path) -> None:
    """Tier 3 review A: a pre-v2 order still waiting gets a status (no estimate), and an
    earlier-day order paid today stays on /kitchen so ยกเลิกจ่าย is reachable."""
    import sqlite3

    menu = load_menu(MENU_PATH)
    clock = {"now": T0}
    db = ShopDB(tmp_path / "shop.db", clock=lambda: clock["now"])
    client = TestClient(
        create_app(settings=_settings(db.db_path), db=db, menu=menu, line_client=SpyLineClient())
    )
    legacy = _place(db, "U_A", [kaphrao(1)])
    paid_later = _place(db, "U_B", [kaphrao(1)])
    conn = sqlite3.connect(db.db_path)
    conn.execute("UPDATE orders SET payment_status = 'legacy' WHERE id = ?", (legacy.id,))
    conn.commit()
    conn.close()

    clock["now"] = T0.replace(day=24)
    call = SimpleNamespace(id="g1", function=SimpleNamespace(name="get_my_orders", arguments="{}"))
    llm = StubLLM([_resp(tool_calls=[call]), _resp(content="ค่ะ")])
    handle_customer_text(db, menu, "U_A", "เสร็จยัง", llm_client=llm, settings=_settings(db.db_path))
    orders = json.loads(llm.calls[1]["messages"][-1]["content"])["orders"]
    assert [(o["order_id"], o["status"], "queue_position" in o) for o in orders] == [
        (legacy.id, "queued", False)
    ]

    assert db.mark_order_done(paid_later.id, paid_cash=True) is not None
    finished = client.get(f"/api/kitchen/state{KEY}").json()["orders"]["finished"]
    assert [(o["id"], o["payment_status"], o["day_label"]) for o in finished] == [
        (paid_later.id, "cash", "เมื่อวาน")
    ]
