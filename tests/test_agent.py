"""Behavioral tests for customer ordering agent: defaults, returning address, hand-over, limits."""

from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

from app.agent import (
    HANDOVER_REPLY,
    LIMIT_REPLY,
    MAX_DAILY_TURNS,
    TURN_LIMIT_REPLY,
    handle_customer_text,
)
from app.config import Settings
from app.db import ShopDB
from app.menu import load_menu

MENU_PATH = Path(__file__).resolve().parent.parent / "menu.yaml"
BKK = ZoneInfo("Asia/Bangkok")


def _tool_call(call_id: str, name: str, args: dict[str, Any]) -> Any:
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(args, ensure_ascii=False)),
    )


def _resp(content: str = "", tool_calls: list[Any] | None = None) -> Any:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=tool_calls))]
    )


class StubLLM:
    """Deterministic stub for OpenAI chat.completions.create."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("StubLLM ran out of scripted responses")
        return self._responses.pop(0)


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


def test_agent_orders_with_default_spice_and_busy_wait_time_on_flex_card(tmp_path: Path) -> None:
    menu = load_menu(MENU_PATH)
    now = datetime(2026, 11, 23, 12, 0, tzinfo=BKK)
    db = ShopDB(tmp_path / "shop.db", clock=lambda: now)
    db.set_busy_mode(True, 25)

    stub = StubLLM(
        [
            _resp(
                tool_calls=[
                    _tool_call(
                        "c1",
                        "update_cart",
                        {
                            "action": "add",
                            "items": [
                                {"dish_id": "kaphrao", "qty": 1, "option_ids": ["crispy_pork"]}
                            ],
                        },
                    ),
                    _tool_call("c2", "set_fulfillment", {"fulfillment": "pickup"}),
                    _tool_call("c3", "build_order_card", {}),
                ]
            ),
            _resp(content="สรุปออเดอร์ให้แล้วค่ะ กดยืนยันบนการ์ดได้เลยนะคะ"),
        ]
    )

    turn = handle_customer_text(
        db,
        menu,
        "U_C1",
        "กะเพราหมูกรอบ 1 กล่อง รับเอง",
        llm_client=stub,
        settings=_settings(tmp_path / "shop.db"),
    )
    assert turn.reason == "ok"
    assert turn.flex_card is not None
    assert "60 บาท" in turn.flex_card["altText"]
    # Busy wait time banner appears on the Flex card
    body_texts = [
        c.get("text", "")
        for c in turn.flex_card["contents"]["body"]["contents"]
        if c.get("type") == "text"
    ]
    assert any("25 นาที" in t for t in body_texts)
    # Default spice (เผ็ดปกติ) was applied by code
    conv = db.get_or_create_conversation("U_C1")
    assert conv.active_card is not None
    assert "spice_normal" in conv.active_card["priced_lines"][0]["option_ids"]


def test_agent_reuses_returning_customer_saved_address(tmp_path: Path) -> None:
    menu = load_menu(MENU_PATH)
    now = datetime(2026, 11, 23, 12, 0, tzinfo=BKK)
    db = ShopDB(tmp_path / "shop.db", clock=lambda: now)
    db.upsert_customer(
        "U_RET",
        last_address="บ้าน 45/2",
        last_location_url="https://maps.google.com/?q=13.75,100.5",
        update_address=True,
    )

    stub = StubLLM(
        [
            _resp(
                tool_calls=[
                    _tool_call(
                        "c1",
                        "update_cart",
                        {
                            "action": "add",
                            "items": [{"dish_id": "kaphrao", "qty": 2, "option_ids": ["chicken"]}],
                        },
                    ),
                    _tool_call(
                        "c2",
                        "set_fulfillment",
                        {"fulfillment": "delivery", "use_saved_address": True},
                    ),
                    _tool_call("c3", "build_order_card", {}),
                ]
            ),
            _resp(content="ส่งที่เดิม (บ้าน 45/2) นะคะ ถ้าเปลี่ยนที่อยู่บอกได้เลยค่ะ"),
        ]
    )

    turn = handle_customer_text(
        db,
        menu,
        "U_RET",
        "กะเพราไก่ 2 กล่อง ส่งเหมือนเดิม",
        llm_client=stub,
        settings=_settings(tmp_path / "shop.db"),
    )
    assert turn.reason == "ok"
    assert turn.flex_card is not None
    conv = db.get_or_create_conversation("U_RET")
    assert conv.active_card is not None
    assert conv.active_card["address"] == "บ้าน 45/2"
    assert conv.active_card["location_url"] == "https://maps.google.com/?q=13.75,100.5"
    assert conv.active_card["total"] == 100  # 50*2 = 100 -> free delivery


def test_hand_over_silence_and_wake_up_on_order_status(tmp_path: Path) -> None:
    menu = load_menu(MENU_PATH)
    now = datetime(2026, 11, 23, 12, 0, tzinfo=BKK)
    db = ShopDB(tmp_path / "shop.db", clock=lambda: now)
    cfg = _settings(tmp_path / "shop.db")

    # 1. Bare greeting -> hand_over replies once
    stub1 = StubLLM([_resp(tool_calls=[_tool_call("h1", "hand_over", {})])])
    t1 = handle_customer_text(db, menu, "U_HO", "สวัสดีค่ะ", llm_client=stub1, settings=cfg)
    assert t1.reason == "hand_over"
    assert t1.reply_text == HANDOVER_REPLY

    # 2. Follow-up "ขอบคุณค่ะ" with no tool calls -> 100% silent
    stub2 = StubLLM([_resp(content="ยินดีค่ะ")])
    t2 = handle_customer_text(db, menu, "U_HO", "ขอบคุณค่ะ", llm_client=stub2, settings=cfg)
    assert t2.reason == "silent"
    assert t2.reply_text is None

    # 3. Clear status query calling get_my_orders -> wakes up and replies
    stub3 = StubLLM(
        [
            _resp(tool_calls=[_tool_call("s1", "get_my_orders", {})]),
            _resp(content="ยังไม่มีออเดอร์วันนี้ค่ะ"),
        ]
    )
    t3 = handle_customer_text(db, menu, "U_HO", "ถึงไหนแล้ว", llm_client=stub3, settings=cfg)
    assert t3.reason == "ok"
    assert t3.reply_text == "ยังไม่มีออเดอร์วันนี้ค่ะ"
    assert db.is_handed_over(db.get_or_create_conversation("U_HO")) is False


def test_bot_disabled_and_guardrails(tmp_path: Path) -> None:
    menu = load_menu(MENU_PATH)
    now = datetime(2026, 11, 23, 12, 0, tzinfo=BKK)
    db = ShopDB(tmp_path / "shop.db", clock=lambda: now)
    cfg = _settings(tmp_path / "shop.db")

    # bot_enabled = 0 -> silent, 0 LLM calls
    db.set_bot_enabled(False)
    empty_stub = StubLLM([])
    t_off = handle_customer_text(db, menu, "U_G", "กะเพราไก่", llm_client=empty_stub, settings=cfg)
    assert t_off.reason == "bot_disabled"
    assert t_off.reply_text is None
    assert empty_stub.calls == []
    db.set_bot_enabled(True)

    # Max 6 tool calls guardrail
    seven_calls = [_tool_call(f"t{i}", "get_shop_status", {}) for i in range(7)]
    stub_max = StubLLM([_resp(tool_calls=seven_calls)])
    t_max = handle_customer_text(db, menu, "U_MAX", "เปิดยัง", llm_client=stub_max, settings=cfg)
    assert t_max.reason == "max_tool_calls"
    assert t_max.reply_text == LIMIT_REPLY

    # 40s deadline guardrail
    t_dead = handle_customer_text(
        db,
        menu,
        "U_DEAD",
        "เปิดยัง",
        llm_client=StubLLM([]),
        settings=cfg,
        started_at=time.monotonic() - 45.0,
    )
    assert t_dead.reason == "deadline"
    assert t_dead.reply_text == LIMIT_REPLY

    # 30 customer turns/day -> hands over once, then silent
    conv = db.get_or_create_conversation("U_30")
    fake_history = [{"role": "user", "content": f"m{i}"} for i in range(MAX_DAILY_TURNS)]
    db.save_conversation_messages(conv.id, fake_history)
    t_lim1 = handle_customer_text(
        db, menu, "U_30", "อีกข้อความ", llm_client=StubLLM([]), settings=cfg
    )
    assert t_lim1.reason == "turn_limit"
    assert t_lim1.reply_text == TURN_LIMIT_REPLY
    t_lim2 = handle_customer_text(db, menu, "U_30", "อีกที", llm_client=StubLLM([]), settings=cfg)
    assert t_lim2.reason == "silent"
    assert t_lim2.reply_text is None


def test_agent_preserves_location_pin_and_handles_bad_tool_args_and_closed_shop(
    tmp_path: Path,
) -> None:
    menu = load_menu(MENU_PATH)
    now = datetime(2026, 11, 23, 12, 0, tzinfo=BKK)
    db = ShopDB(tmp_path / "shop.db", clock=lambda: now)
    cfg = _settings(tmp_path / "shop.db")

    # 1. Existing pinned draft address preserved when LLM echoes same address string
    conv = db.get_or_create_conversation("U_PIN")
    db.save_draft(
        conv.id,
        {
            "items": [{"dish_id": "kaphrao", "qty": 1, "option_ids": ["chicken"]}],
            "fulfillment": "delivery",
            "address": "45/2 หมู่ 3",
            "location_url": "https://maps.google.com/?q=13.7563,100.5018",
        },
    )
    stub_pin = StubLLM(
        [
            _resp(
                tool_calls=[
                    _tool_call(
                        "f1", "set_fulfillment", {"fulfillment": "delivery", "address": "45/2 หมู่ 3"}
                    ),
                    _tool_call("b1", "build_order_card", {}),
                ]
            ),
            _resp(content="สรุปให้แล้วค่ะ"),
        ]
    )
    t_pin = handle_customer_text(db, menu, "U_PIN", "ส่งค่ะ", llm_client=stub_pin, settings=cfg)
    assert t_pin.reason == "ok"
    conv_after = db.get_or_create_conversation("U_PIN")
    assert conv_after.active_card is not None
    assert conv_after.active_card["location_url"] == "https://maps.google.com/?q=13.7563,100.5018"

    # 2. Returning customer's saved pin restored when LLM passes saved address text directly
    db.upsert_customer(
        "U_SAVED",
        last_address="บ้าน 88",
        last_location_url="https://maps.google.com/?q=13.8,100.6",
        update_address=True,
    )
    stub_saved = StubLLM(
        [
            _resp(
                tool_calls=[
                    _tool_call(
                        "u1",
                        "update_cart",
                        {"action": "add", "items": [{"dish_id": "water", "qty": 1}]},
                    ),
                    _tool_call(
                        "f2", "set_fulfillment", {"fulfillment": "delivery", "address": "บ้าน 88"}
                    ),
                ]
            ),
            _resp(content="ส่งที่เดิม (บ้าน 88) นะคะ"),
        ]
    )
    handle_customer_text(
        db, menu, "U_SAVED", "น้ำ 1 ขวด ส่งบ้าน 88", llm_client=stub_saved, settings=cfg
    )
    assert db.get_or_create_conversation("U_SAVED").draft["location_url"] == (
        "https://maps.google.com/?q=13.8,100.6"
    )

    # 3. Malformed tool args (qty: null, option_ids: null, remove_index: "first") never crash turn
    stub_bad = StubLLM(
        [
            _resp(
                tool_calls=[
                    _tool_call(
                        "bad1",
                        "update_cart",
                        {
                            "action": "add",
                            "items": [{"dish_id": "kaphrao", "qty": None, "option_ids": None}],
                        },
                    ),
                    _tool_call(
                        "bad2", "update_cart", {"action": "remove", "remove_index": "first"}
                    ),
                ]
            ),
            _resp(content="ขอรายละเอียดเพิ่มหน่อยนะคะ"),
        ]
    )
    t_bad = handle_customer_text(db, menu, "U_BAD", "กะเพรา", llm_client=stub_bad, settings=cfg)
    assert t_bad.reason == "ok"
    assert t_bad.reply_text == "ขอรายละเอียดเพิ่มหน่อยนะคะ"

    # 4. Closed shop refuses update_cart(add)
    db.set_open_override("closed")
    stub_closed = StubLLM(
        [
            _resp(
                tool_calls=[
                    _tool_call(
                        "cl1",
                        "update_cart",
                        {
                            "action": "add",
                            "items": [{"dish_id": "water", "qty": 1, "option_ids": []}],
                        },
                    )
                ]
            ),
            _resp(content="ตอนนี้ร้านปิดอยู่ค่ะ เปิด 09:00–20:00 น."),
        ]
    )
    t_closed = handle_customer_text(
        db, menu, "U_CLOSED", "เอาน้ำ 1 ขวด", llm_client=stub_closed, settings=cfg
    )
    assert t_closed.reason == "ok"
    assert db.get_or_create_conversation("U_CLOSED").draft == {}

    # 5. Live state system note reflects restocked items and empty cart despite old chat text
    db.set_open_override("open")
    db.set_override("ingredient", "crispy_pork", True)
    conv_restock = db.get_or_create_conversation("U_RESTOCK")
    db.save_conversation_messages(
        conv_restock.id,
        [
            {"role": "user", "content": "กะเพราหมูกรอบ"},
            {"role": "assistant", "content": "ขออภัยค่ะ หมูกรอบหมดแล้ว"},
        ],
    )
    stub_restock = StubLLM([_resp(content="รับทราบค่ะ ไข่ดาวสุกหรือไม่สุกคะ")])
    handle_customer_text(
        db, menu, "U_RESTOCK", "กะเพราหมูกรอบ ไข่ดาว 1", llm_client=stub_restock, settings=cfg
    )
    sent_msgs = stub_restock.calls[0]["messages"]
    live_note = next(
        m["content"]
        for m in sent_msgs
        if m["role"] == "system"
        and isinstance(m["content"], str)
        and "สถานะระบบล่าสุดตอนนี้" in m["content"]
    )
    assert "ของหมดตอนนี้=ไม่มี" in live_note
    assert "ตะกร้าปัจจุบัน=ว่าง" in live_note
