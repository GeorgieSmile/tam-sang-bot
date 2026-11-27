"""Behavioral tests for v2 Ticket 2: hand-over inbox, strict bot quiet, voice reply."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.agent import (
    BAD_ARGS_ERROR,
    CARD_AFTER_FAILED_EDIT,
    ERROR_REPLY,
    HANDOVER_REPLY,
    IMAGE_HANDOVER_REPLY,
    MAX_DAILY_TURNS,
    RESUME_NOTE,
    TURN_LIMIT_REPLY,
    VOICE_REPLY,
    _system_prompt,
    build_card_snapshot_for_conv,
)
from app.config import Settings
from app.db import ShopDB
from app.main import create_app
from app.menu import load_menu

MENU_PATH = Path(__file__).resolve().parent.parent / "menu.yaml"
BKK = ZoneInfo("Asia/Bangkok")
SECRET = "test-channel-secret"
KEY = "?key=secret-123"
OFF_MENU_TEXT = "แพ้กุ้งค่ะ กะเพราใส่กุ้งไหมคะ"
OFF_MENU_LABEL = "ถามส่วนผสม/แพ้อาหาร"
T0 = datetime(2026, 11, 23, 12, 0, tzinfo=BKK)
LIVE_STATE = (
    "สถานะระบบล่าสุดตอนนี้ (ยึดข้อมูลนี้เหนือข้อความเก่าในแชท): "
    "ร้าน=เปิดรับออเดอร์ | สั่ง 1 กล่องตอนนี้รอประมาณ 10–15 นาที "
    "| ของหมดตอนนี้=ไม่มี (ทุกเมนูและวัตถุดิบพร้อมขาย) "
    "| ตะกร้าปัจจุบัน=ว่าง (0 รายการ) | ยอดค้างชำระ=0 บาท"
)


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class SpyLineClient:
    def __init__(self, profile_name: str | None = "สมชาย") -> None:
        self.replies: list[tuple[str, list[dict[str, Any]]]] = []
        self.pushes: list[tuple[str, str]] = []
        self.profile_calls: list[str] = []
        self.profile_name = profile_name

    def reply_messages(self, reply_token: str, messages: list[dict[str, Any]]) -> None:
        self.replies.append((reply_token, messages))

    def push_text(self, user_id: str, text: str) -> None:
        self.pushes.append((user_id, text))

    def show_loading(self, user_id: str, seconds: int = 20) -> None:
        return None

    def get_profile(self, user_id: str) -> str | None:
        self.profile_calls.append(user_id)
        return self.profile_name

    def get_message_content(self, message_id: str) -> bytes:
        return b""


class StubLLM:
    def __init__(self) -> None:
        self.responses: list[Any] = []
        self.calls: list[dict[str, Any]] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError("StubLLM ran out of scripted responses")
        return self.responses.pop(0)


def _resp(content: str = "", tool_calls: list[Any] | None = None) -> Any:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=tool_calls))]
    )


def _tool_call(call_id: str, name: str, args: dict[str, Any]) -> Any:
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(args, ensure_ascii=False)),
    )


class Harness:
    def __init__(self, tmp_path: Path, *, profile_name: str | None = "สมชาย") -> None:
        self.menu = load_menu(MENU_PATH)
        self.clock = Clock(T0)
        self.db = ShopDB(tmp_path / "shop.db", clock=self.clock)
        self.line = SpyLineClient(profile_name)
        self.llm = StubLLM()
        self.settings = Settings(
            db_path=tmp_path / "shop.db",
            menu_path=MENU_PATH,
            kitchen_secret="secret-123",
            public_base_url="http://localhost:8000",
            payment_info_text="",
            line_channel_secret=SECRET,
            line_channel_access_token="token",
            owner_line_user_id="U_OWNER",
            llm_base_url="https://openrouter.ai/api/v1",
            llm_api_key="test-key",
            llm_model="anthropic/claude-haiku-5.5",
            llm_reasoning_effort="low",
        )
        self.client = TestClient(
            create_app(
                settings=self.settings,
                db=self.db,
                menu=self.menu,
                line_client=self.line,
                llm_client=self.llm,
            )
        )
        self._n = 0

    def send(self, user_id: str, message: dict[str, Any]) -> None:
        self._n += 1
        self._post(
            {
                "webhookEventId": f"evt-{self._n}",
                "type": "message",
                "replyToken": f"rt-{self._n}",
                "source": {"type": "user", "userId": user_id},
                "message": {"id": f"m-{self._n}", **message},
            }
        )

    def text(self, user_id: str, text: str) -> None:
        self.send(user_id, {"type": "text", "text": text})

    def postback(self, user_id: str, data: str) -> None:
        self._n += 1
        self._post(
            {
                "webhookEventId": f"evt-{self._n}",
                "type": "postback",
                "replyToken": f"rt-{self._n}",
                "source": {"type": "user", "userId": user_id},
                "postback": {"data": data},
            }
        )

    def _post(self, event: dict[str, Any]) -> None:
        raw = json.dumps({"events": [event]}, ensure_ascii=False).encode("utf-8")
        sig = base64.b64encode(hmac.new(SECRET.encode(), raw, hashlib.sha256).digest()).decode()
        res = self.client.post(
            "/line/webhook",
            content=raw,
            headers={"Content-Type": "application/json", "x-line-signature": sig},
        )
        assert res.status_code == 200

    def state(self) -> dict[str, Any]:
        res = self.client.get(f"/api/kitchen/state{KEY}")
        assert res.status_code == 200
        data: dict[str, Any] = res.json()
        return data

    def cards(self, user_id: str) -> list[dict[str, Any]]:
        return [c for c in self.state()["inbox"] if c["line_user_id"] == user_id]

    def off_menu_hand_over(self, user_id: str) -> None:
        # Greetings and unknown dishes no longer hand over (Ticket 7); an allergy question does
        self.llm.responses.append(
            _resp(tool_calls=[_tool_call("h1", "hand_over", {"reason": "off_menu"})])
        )
        self.text(user_id, OFF_MENU_TEXT)

    def until(self, user_id: str) -> str | None:
        return self.db.get_or_create_conversation(user_id).handed_over_until


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def test_off_menu_hand_over_opens_card_and_quiets_for_15_minutes(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.off_menu_hand_over("U_A")

    assert h.line.replies == [("rt-1", [{"type": "text", "text": HANDOVER_REPLY}])]
    cards = h.cards("U_A")
    assert len(cards) == 1
    assert cards[0]["reasons"] == ["off_menu"]
    assert cards[0]["reason_lines"] == [OFF_MENU_LABEL]
    assert cards[0]["recent"] == [{"text": OFF_MENU_TEXT, "at": _iso(T0)}]
    assert cards[0]["bot_quiet"] is True and cards[0]["quiet_minutes_left"] == 15
    assert h.until("U_A") == _iso(T0 + timedelta(minutes=15))
    # The LLM tool requires a reason limited to the LLM codes (no greeting since Ticket 7)
    tool = next(t for t in h.llm.calls[0]["tools"] if t["function"]["name"] == "hand_over")
    params = tool["function"]["parameters"]
    assert params["required"] == ["reason"]
    assert params["properties"]["reason"]["enum"] == ["edit_confirmed", "off_menu"]


def test_quiet_text_is_saved_silently_and_bot_returns_with_note_after_15_minutes(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    h.off_menu_hand_over("U_A")
    h.line.replies.clear()
    llm_calls_before = len(h.llm.calls)

    t1 = T0 + timedelta(minutes=5)
    h.clock.now = t1
    h.text("U_A", "กะเพราหมูกรอบ 1 กล่อง")
    assert h.line.replies == []
    assert len(h.llm.calls) == llm_calls_before
    conv = h.db.get_or_create_conversation("U_A")
    assert conv.messages[-1] == {"role": "user", "content": "กะเพราหมูกรอบ 1 กล่อง"}
    card = h.cards("U_A")[0]
    assert card["unseen_count"] == 1
    assert card["recent"][-1]["text"] == "กะเพราหมูกรอบ 1 กล่อง"
    assert h.until("U_A") == _iso(t1 + timedelta(minutes=15))

    # Exactly 15 minutes after the last customer message the bot is back
    h.clock.now = t1 + timedelta(minutes=15)
    h.llm.responses.append(_resp(content="รับกะเพราหมูกรอบ 1 กล่อง รับเองหรือให้ส่งคะ"))
    h.text("U_A", "กะเพราหมูกรอบ 1 กล่อง")
    assert len(h.llm.calls) == llm_calls_before + 1
    assert h.llm.calls[-1]["messages"] == [
        _system_prompt(h.menu, {}),
        {"role": "system", "content": LIVE_STATE},
        {"role": "system", "content": RESUME_NOTE},
        {"role": "user", "content": OFF_MENU_TEXT},
        {"role": "assistant", "content": HANDOVER_REPLY},
        {"role": "user", "content": "กะเพราหมูกรอบ 1 กล่อง"},
        {"role": "user", "content": "กะเพราหมูกรอบ 1 กล่อง"},
    ]
    assert RESUME_NOTE == (
        "ร้านคุยกับลูกค้าเรื่องก่อนหน้านี้แล้ว บอทไม่เห็นข้อความของร้าน อย่าตอบคำถามเดิมซ้ำ ให้ช่วยเฉพาะเรื่องใหม่"
    )
    assert h.line.replies[-1][1][0]["text"] == "รับกะเพราหมูกรอบ 1 กล่อง รับเองหรือให้ส่งคะ"
    card = h.cards("U_A")[0]
    assert card["bot_quiet"] is False  # shown as บอทกลับมาแล้ว until เรียบร้อย

    # The note is given only once
    h.llm.responses.append(_resp(content="ได้ค่ะ"))
    h.text("U_A", "รับเอง")
    assert {"role": "system", "content": RESUME_NOTE} not in h.llm.calls[-1]["messages"]


def test_resolve_hides_card_ends_quiet_and_next_text_gets_note(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.off_menu_hand_over("U_A")
    card_id = h.cards("U_A")[0]["id"]

    h.clock.now = T0 + timedelta(minutes=2)
    assert h.client.post(f"/api/kitchen/handovers/{card_id}/resolve").status_code == 403
    assert h.client.post(f"/api/kitchen/handovers/{card_id}/resolve{KEY}").status_code == 200
    assert h.cards("U_A") == []
    assert h.db.is_handed_over(h.db.get_or_create_conversation("U_A")) is False
    assert h.client.post(f"/api/kitchen/handovers/{card_id}/resolve{KEY}").status_code == 404

    h.llm.responses.append(_resp(content="สั่งอะไรดีคะ"))
    h.text("U_A", "ขอสั่งอาหารค่ะ")
    msgs = h.llm.calls[-1]["messages"]
    assert msgs[1:3] == [
        {"role": "system", "content": LIVE_STATE},
        {"role": "system", "content": RESUME_NOTE},
    ]
    assert msgs[-1] == {"role": "user", "content": "ขอสั่งอาหารค่ะ"}


def test_turn_limit_hand_over_lasts_until_resolve_then_count_resets(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    conv = h.db.get_or_create_conversation("U_30")
    h.db.save_conversation_messages(
        conv.id, [{"role": "user", "content": f"m{i}"} for i in range(MAX_DAILY_TURNS)]
    )
    h.text("U_30", "อีกข้อความ")
    assert h.line.replies[-1][1] == [{"type": "text", "text": TURN_LIMIT_REPLY}]
    assert h.llm.calls == []
    assert h.cards("U_30")[0]["reasons"] == ["turn_limit"]
    h.line.replies.clear()

    h.clock.now = T0 + timedelta(minutes=60)
    h.text("U_30", "ยังอยู่ไหม")
    assert h.line.replies == [] and h.llm.calls == []

    card_id = h.cards("U_30")[0]["id"]
    assert h.client.post(f"/api/kitchen/handovers/{card_id}/resolve{KEY}").status_code == 200
    h.llm.responses.append(_resp(content="ได้ค่ะ"))
    h.text("U_30", "กะเพราไก่ 1")
    assert len(h.llm.calls) == 1
    assert {"role": "system", "content": RESUME_NOTE} in h.llm.calls[0]["messages"]
    assert h.line.replies[-1][1][0]["text"] == "ได้ค่ะ"


def test_location_pin_while_quiet_is_saved_silently_and_shown_on_card(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.off_menu_hand_over("U_A")
    h.line.replies.clear()
    h.send(
        "U_A",
        {"type": "location", "address": "45/2 หมู่ 3", "latitude": 13.75, "longitude": 100.5},
    )
    assert h.line.replies == []
    conv = h.db.get_or_create_conversation("U_A")
    assert conv.draft["address"] == "45/2 หมู่ 3"
    assert conv.draft["location_url"] == "https://maps.google.com/?q=13.75,100.5"
    assert h.db.is_handed_over(conv) is True
    pins = [d for d in h.cards("U_A")[0]["details"] if d["kind"] == "pin"]
    assert [(p["address"], p["url"]) for p in pins] == [
        ("45/2 หมู่ 3", "https://maps.google.com/?q=13.75,100.5")
    ]


def test_confirm_tap_while_quiet_creates_order_and_ends_quiet(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    conv = h.db.get_or_create_conversation("U_A")
    h.db.save_draft(
        conv.id,
        {
            "items": [{"dish_id": "kaphrao", "qty": 1, "option_ids": ["crispy_pork"]}],
            "fulfillment": "pickup",
        },
    )
    snap, err = build_card_snapshot_for_conv(h.db, h.db.get_or_create_conversation("U_A"), h.menu)
    assert snap is not None, err
    h.db.open_handover("U_A", "greeting")
    assert h.db.is_handed_over(h.db.get_or_create_conversation("U_A")) is True

    h.postback("U_A", f"action=confirm&card_id={snap['card_id']}")
    assert h.state()["orders"]["queued"][0]["line_user_id"] == "U_A"
    assert h.db.is_handed_over(h.db.get_or_create_conversation("U_A")) is False

    # The next text reaches the LLM with the bot-came-back note
    h.llm.responses.append(_resp(content="ขอบคุณค่ะ"))
    h.text("U_A", "ขอบคุณค่ะ")
    assert h.llm.calls[-1]["messages"][2] == {"role": "system", "content": RESUME_NOTE}


def test_button_taps_do_not_end_a_turn_limit_hand_over(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    conv = h.db.get_or_create_conversation("U_30")
    h.db.save_conversation_messages(
        conv.id, [{"role": "user", "content": f"m{i}"} for i in range(MAX_DAILY_TURNS)]
    )
    h.text("U_30", "อีกข้อความ")
    # turn_limit quiet ends exactly at the next midnight
    assert h.until("U_30") == "2026-11-24T00:00:00+07:00"
    h.postback("U_30", "action=cancel&card_id=whatever")
    h.postback("U_30", "action=confirm&card_id=whatever")
    assert h.db.is_handed_over(h.db.get_or_create_conversation("U_30")) is True
    # An unknown postback never ends quiet either
    h.db.open_handover("U_B", "greeting")
    h.postback("U_B", "action=other")
    assert h.db.is_handed_over(h.db.get_or_create_conversation("U_B")) is True


def test_turn_limit_is_tracked_per_day_and_ends_at_midnight(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    # Yesterday's off-menu card is still open when today's turn limit is hit
    h.off_menu_hand_over("U_30")
    h.clock.now = T0.replace(hour=23, minute=50) + timedelta(days=1)
    conv = h.db.get_or_create_conversation("U_30")
    h.db.save_conversation_messages(
        conv.id, [{"role": "user", "content": f"m{i}"} for i in range(MAX_DAILY_TURNS)]
    )
    h.text("U_30", "อีกข้อความ")
    assert len(h.cards("U_30")) == 1
    assert h.cards("U_30")[0]["reasons"] == ["off_menu", "turn_limit"]
    h.postback("U_30", "action=confirm&card_id=whatever")
    assert h.db.is_handed_over(h.db.get_or_create_conversation("U_30")) is True
    # Quiet activity at 23:56 does not push the turn-limit end past midnight
    h.clock.now = T0.replace(hour=23, minute=56) + timedelta(days=1)
    h.text("U_30", "ยังอยู่ไหม")
    assert h.until("U_30") == "2026-11-25T00:00:00+07:00"
    # เรียบร้อย resets today's count
    card_id = h.cards("U_30")[0]["id"]
    assert h.db.resolve_handover(card_id) is True
    assert h.db.get_or_create_conversation("U_30").turn_offset == MAX_DAILY_TURNS + 2


def test_quiet_activity_after_resolve_does_not_restore_quiet(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.off_menu_hand_over("U_A")
    card_id = h.cards("U_A")[0]["id"]
    assert h.db.resolve_handover(card_id) is True
    # A message that was already in flight as "quiet" when the owner tapped เรียบร้อย
    h.db.record_quiet_activity("U_A", "late message")
    assert h.db.is_handed_over(h.db.get_or_create_conversation("U_A")) is False


def test_resolving_yesterdays_turn_limit_card_keeps_todays_count(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    conv = h.db.get_or_create_conversation("U_30")
    h.db.save_conversation_messages(
        conv.id, [{"role": "user", "content": f"m{i}"} for i in range(MAX_DAILY_TURNS)]
    )
    h.text("U_30", "อีกข้อความ")
    card_id = h.cards("U_30")[0]["id"]

    h.clock.now = T0 + timedelta(days=1)
    today = h.db.get_or_create_conversation("U_30")
    h.db.save_conversation_messages(today.id, [{"role": "user", "content": "x"}])
    assert h.db.resolve_handover(card_id) is True
    assert h.db.get_or_create_conversation("U_30").turn_offset == 0


def test_owner_takeover_sends_nothing_and_silences_the_bot(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    for i in range(1, 6):
        conv = h.db.get_or_create_conversation(f"U_{i}")
        h.db.save_draft(
            conv.id,
            {
                "items": [{"dish_id": "kaphrao", "qty": 1, "option_ids": ["chicken"]}],
                "fulfillment": "pickup",
            },
        )
        snap, _ = build_card_snapshot_for_conv(
            h.db, h.db.get_or_create_conversation(f"U_{i}"), h.menu
        )
        assert snap is not None
        assert h.db.confirm_card(f"U_{i}", snap["card_id"], h.menu).ok
    order5 = h.state()["orders"]["queued"][4]
    assert order5["queue_number"] == 5

    h.db.set_notify_owner(True)
    res = h.client.post(f"/api/kitchen/orders/{order5['id']}/takeover{KEY}")
    assert res.status_code == 200
    assert h.line.replies == []
    # Owner alerts are on, so the new card pushes the owner once (never the customer)
    assert h.line.pushes == [("U_OWNER", "มีลูกค้ารอร้านตอบ: สมชาย (ร้านคุยเอง #5)")]
    card = h.cards("U_5")[0]
    assert card["reasons"] == ["owner_takeover"]
    assert card["reason_lines"] == ["ร้านคุยเอง #5"]

    h.text("U_5", "ได้ค่ะ")
    assert h.line.replies == [] and h.llm.calls == []


def test_second_reason_joins_the_open_card(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.off_menu_hand_over("U_A")
    h.clock.now = T0 + timedelta(minutes=1)
    h.send("U_A", {"type": "image"})
    cards = h.cards("U_A")
    assert len(cards) == 1
    assert cards[0]["reasons"] == ["off_menu", "image"]


def test_owner_push_once_per_new_card_only_when_inbox_alerts_on(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    # Inbox alerts have their own switch, on by default, separate from new-order alerts
    assert h.state()["shop"]["notify_owner_on_inbox"] is True
    assert h.state()["shop"]["notify_owner_on_order"] is False
    h.off_menu_hand_over("U_A")
    assert h.line.pushes == [("U_OWNER", f"มีลูกค้ารอร้านตอบ: สมชาย ({OFF_MENU_LABEL})")]
    h.text("U_A", "อยู่ไหมคะ")
    h.send("U_A", {"type": "image"})
    assert len(h.line.pushes) == 1

    res = h.client.post(f"/api/kitchen/shop{KEY}", json={"notify_owner_on_inbox": False})
    assert res.status_code == 200
    h.db.set_notify_owner(True)  # new-order alerts on: still no inbox push
    h.off_menu_hand_over("U_B")
    assert len(h.line.pushes) == 1


def test_display_name_comes_from_line_profile_once(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.off_menu_hand_over("U_A")
    assert h.line.profile_calls == ["U_A"]
    assert h.cards("U_A")[0]["display_name"] == "สมชาย"
    customer = h.db.get_customer("U_A")
    assert customer is not None and customer.display_name == "สมชาย"

    # A later card for the same customer reuses the stored name
    card_id = h.cards("U_A")[0]["id"]
    h.client.post(f"/api/kitchen/handovers/{card_id}/resolve{KEY}")
    h.send("U_A", {"type": "image"})
    assert h.line.profile_calls == ["U_A"]

    h2 = Harness(tmp_path / "noname", profile_name=None)
    h2.off_menu_hand_over("U_ABCD1234")
    assert h2.line.profile_calls == ["U_ABCD1234"]
    assert h2.cards("U_ABCD1234")[0]["display_name"] == "ลูกค้า …1234"


def test_image_opens_card_with_image_reason(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.send("U_A", {"type": "image"})
    assert h.line.replies == [("rt-1", [{"type": "text", "text": IMAGE_HANDOVER_REPLY}])]
    assert h.llm.calls == []
    card = h.cards("U_A")[0]
    assert card["reasons"] == ["image"]
    # A second image while quiet: no reply, added to the card
    h.send("U_A", {"type": "image"})
    assert len(h.line.replies) == 1
    assert h.cards("U_A")[0]["unseen_count"] == 1


def test_voice_reply_and_silence_while_quiet_or_paused(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.send("U_A", {"type": "audio"})
    assert h.line.replies == [("rt-1", [{"type": "text", "text": VOICE_REPLY}])]
    assert VOICE_REPLY == "รบกวนพิมพ์เป็นข้อความนะคะ 🙏"
    assert h.llm.calls == []

    h.off_menu_hand_over("U_B")
    h.line.replies.clear()
    h.send("U_B", {"type": "audio"})
    assert h.line.replies == []

    h.db.set_bot_enabled(False)
    h.send("U_C", {"type": "audio"})
    h.send("U_C", {"type": "image"})
    assert h.line.replies == []
    assert h.cards("U_C") == []


def test_greeting_gets_a_bot_reply_and_never_opens_a_card(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.llm.responses.append(_resp(content="สวัสดีค่ะ วันนี้รับอะไรดีคะ"))
    h.text("U_A", "สวัสดีครับ")
    assert h.line.replies == [("rt-1", [{"type": "text", "text": "สวัสดีค่ะ วันนี้รับอะไรดีคะ"}])]
    assert h.cards("U_A") == []
    assert h.until("U_A") is None
    rules = h.llm.calls[0]["messages"][0]["content"][0]["text"]
    assert "ถ้าลูกค้าทักทาย ('สวัสดีครับ') หรือคุยเล่น ให้ทักกลับสั้นๆ" in rules
    # A dish we don't sell: the bot says so itself; allergy/ingredient questions still hand over
    assert "ให้บอกตรงๆ ว่าไม่มี แล้วแนะนำเมนูใกล้เคียงที่มีในเมนู JSON (ไม่ต้องเรียก hand_over)" in rules
    assert "ถามเรื่องส่วนผสม/แพ้อาหาร/ผงชูรส" in rules

    # A model that still calls hand_over(greeting) is told to reply itself: no card, no quiet
    h.llm.responses += [
        _resp(tool_calls=[_tool_call("g1", "hand_over", {"reason": "greeting"})]),
        _resp(content="สวัสดีค่ะ รับอะไรดีคะ"),
    ]
    h.text("U_B", "หวัดดี")
    assert h.line.replies[-1][1] == [{"type": "text", "text": "สวัสดีค่ะ รับอะไรดีคะ"}]
    assert h.cards("U_B") == []
    assert h.db.is_handed_over(h.db.get_or_create_conversation("U_B")) is False


def test_menu_list_is_sent_whole_by_code_not_written_by_the_llm(tmp_path: Path) -> None:
    """Ticket 7 phone retest: the LLM-written menu was cut off mid-list at its token limit."""
    h = Harness(tmp_path)
    h.db.set_override("dish", "cola", False)
    h.llm.responses += [
        _resp(tool_calls=[_tool_call("m1", "show_menu", {})]),
        _resp(content="สนใจเมนูไหนบอกได้เลยค่ะ"),
    ]
    h.text("U_A", "มีเมนูอะไรบ้าง")
    menu_msg, reply = h.line.replies[-1][1]
    assert reply == {"type": "text", "text": "สนใจเมนูไหนบอกได้เลยค่ะ"}
    text = menu_msg["text"]
    assert text.startswith(
        "วันนี้มีเมนูดังนี้ค่ะ\n\nอาหารจานหลัก\n• ข้าวกะเพรา 50฿ (หมูสับ/หมูชิ้น/ไก่/หมูกรอบ +10/กุ้ง +10/ปลาหมึก +10)\n"
    )
    # Only choices the customer must make are listed; optional add-ons (เพิ่มข้าว) aren't
    assert "\n• ข้าวไข่เจียว 40฿\n" in text
    assert "\n• ไข่ดาว 10฿ (ไข่ดาวไม่สุก/ไข่ดาวสุก)\n" in text
    assert "เพิ่มข้าว" not in text
    assert text.endswith("\n\nเครื่องดื่ม\n• น้ำเปล่า (ขวด) 10฿\n• ชาไทยเย็น 35฿\n• โกโก้เย็น 35฿")
    assert "โคล่า" not in text  # sold out today
    # All 24 menu items except the sold-out one: nothing cut off
    assert sum(line.startswith("• ") for line in text.splitlines()) == 23

    # A sold-out meat drops off every line that offered it
    h.db.set_override("ingredient", "crispy_pork", False)
    sold = h.menu.menu_text(h.db.get_overrides())
    assert "หมูกรอบ" not in sold
    assert "• ข้าวกะเพรา 50฿ (หมูสับ/หมูชิ้น/ไก่/กุ้ง +10/ปลาหมึก +10)" in sold
    # The LLM is told the list went out, and its history records the tool call
    note = json.loads(h.llm.calls[-1]["messages"][-1]["content"])["note"]
    assert note.startswith("ระบบส่งรายการเมนูให้ลูกค้าแล้ว")
    saved = h.db.get_or_create_conversation("U_A").messages
    assert [m["role"] for m in saved] == ["user", "assistant", "tool", "assistant"]


def test_owner_takeover_while_the_llm_is_thinking_silences_that_reply(tmp_path: Path) -> None:
    """Full review #2: คุยเอง during an in-flight LLM call -> the bot never talks over the owner."""
    h = Harness(tmp_path)

    def create(**kwargs: Any) -> Any:
        h.llm.calls.append(kwargs)
        h.db.open_handover("U_A", "owner_takeover", "#1")  # the owner taps คุยเอง meanwhile
        return _resp(content="รับกะเพราหมูกรอบ 1 กล่องค่ะ")

    h.llm.chat = SimpleNamespace(completions=SimpleNamespace(create=create))
    h.text("U_A", "กะเพราหมูกรอบ 1")
    assert h.line.replies == []
    card = h.cards("U_A")[0]
    assert card["reasons"] == ["owner_takeover"]
    assert card["recent"][-1]["text"] == "กะเพราหมูกรอบ 1"
    # History holds the customer's message but no reply they never received
    assert h.db.get_or_create_conversation("U_A").messages == [
        {"role": "user", "content": "กะเพราหมูกรอบ 1"}
    ]


def test_turn_limit_still_counts_after_resolve_then_confirm(tmp_path: Path) -> None:
    """Full review #9: compaction on Confirm must not disable the daily message limit."""
    h = Harness(tmp_path)
    conv = h.db.get_or_create_conversation("U_30")
    h.db.save_conversation_messages(
        conv.id, [{"role": "user", "content": f"m{i}"} for i in range(MAX_DAILY_TURNS)]
    )
    h.text("U_30", "อีกข้อความ")
    assert h.db.resolve_handover(h.cards("U_30")[0]["id"]) is True
    h.db.save_draft(
        h.db.get_or_create_conversation("U_30").id,
        {
            "items": [{"dish_id": "kaphrao", "qty": 1, "option_ids": ["chicken"]}],
            "fulfillment": "pickup",
        },
    )
    snap, _ = build_card_snapshot_for_conv(h.db, h.db.get_or_create_conversation("U_30"), h.menu)
    assert snap is not None
    h.postback("U_30", f"action=confirm&card_id={snap['card_id']}")
    conv = h.db.get_or_create_conversation("U_30")
    # 30 more customer messages after the confirm hit the limit again, with no LLM call
    h.db.save_conversation_messages(
        conv.id,
        [*conv.messages, *({"role": "user", "content": f"n{i}"} for i in range(MAX_DAILY_TURNS))],
    )
    h.line.replies.clear()
    h.text("U_30", "อีกครั้ง")
    assert h.line.replies[-1][1] == [{"type": "text", "text": TURN_LIMIT_REPLY}]
    assert h.llm.calls == []


def test_kitchen_key_never_reaches_the_access_log() -> None:
    """Full review #11: uvicorn logs the full path; the ?key= login is redacted."""
    import logging

    from app.main import _RedactKitchenKey

    rec = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:5", "GET", "/kitchen?key=secret-123&x=1", "1.1", 200),
        None,
    )
    assert _RedactKitchenKey().filter(rec) is True
    assert rec.getMessage() == '1.2.3.4:5 - "GET /kitchen?key=***&x=1 HTTP/1.1" 200'


def test_takeover_during_a_failing_llm_call_sends_nothing_and_keeps_the_message(
    tmp_path: Path,
) -> None:
    """Tier 3 review A: every reply path (here: the LLM error) respects คุยเอง."""
    h = Harness(tmp_path)

    def create(**kwargs: Any) -> Any:
        h.db.open_handover("U_A", "owner_takeover", "#1")
        raise TimeoutError("LLM timed out")

    h.llm.chat = SimpleNamespace(completions=SimpleNamespace(create=create))
    h.text("U_A", "ขอเพิ่มไข่ดาว")
    assert h.line.replies == []
    assert h.cards("U_A")[0]["recent"][-1]["text"] == "ขอเพิ่มไข่ดาว"
    assert h.db.get_or_create_conversation("U_A").messages == [
        {"role": "user", "content": "ขอเพิ่มไข่ดาว"}
    ]


def test_owner_push_during_an_llm_turn_survives_in_history(tmp_path: Path) -> None:
    """Tier 3 review A: the turn's save must not overwrite an event written meanwhile."""
    h = Harness(tmp_path)
    conv = h.db.get_or_create_conversation("U_A")
    h.db.save_draft(
        conv.id,
        {
            "items": [{"dish_id": "kaphrao", "qty": 1, "option_ids": ["chicken"]}],
            "fulfillment": "pickup",
        },
    )
    snap, _ = build_card_snapshot_for_conv(h.db, h.db.get_or_create_conversation("U_A"), h.menu)
    assert snap is not None
    order = h.db.confirm_card("U_A", snap["card_id"], h.menu).order
    assert order is not None

    def create(**kwargs: Any) -> Any:
        h.llm.calls.append(kwargs)
        h.client.post(f"/api/kitchen/orders/{order.id}/done{KEY}", json={"notify": True})
        return _resp(content="ได้ค่ะ")

    h.llm.chat = SimpleNamespace(completions=SimpleNamespace(create=create))
    h.text("U_A", "เสร็จยัง")
    push = "ออเดอร์ #1 เสร็จแล้ว มารับได้เลยค่ะ · ยอดค้าง 50฿"
    assert h.db.get_or_create_conversation("U_A").messages == [
        {"role": "user", "content": "[ร้านแจ้งว่าออเดอร์ #1 เสร็จแล้ว]"},
        {"role": "assistant", "content": push},
        {"role": "user", "content": "เสร็จยัง"},
        {"role": "assistant", "content": "ได้ค่ะ"},
    ]
    # ...and Confirm compaction keeps that owner update too
    h.db.append_conversation_event(
        "U_A", "[ลูกค้ากดยืนยันออเดอร์ #2 บนการ์ดแล้ว]", "รับออเดอร์ #2", compact_for_confirmed_order=True
    )
    assert {"role": "assistant", "content": push} in h.db.get_or_create_conversation("U_A").messages


def test_daily_limit_keeps_messages_sent_between_reset_and_confirm(tmp_path: Path) -> None:
    """Tier 3 review A: 29 texts after the owner's reset, then Confirm, then 1 more = 30."""
    h = Harness(tmp_path)
    conv = h.db.get_or_create_conversation("U_30")
    h.db.save_conversation_messages(
        conv.id, [{"role": "user", "content": f"m{i}"} for i in range(MAX_DAILY_TURNS)]
    )
    h.text("U_30", "อีกข้อความ")
    assert h.db.resolve_handover(h.cards("U_30")[0]["id"]) is True
    conv = h.db.get_or_create_conversation("U_30")
    h.db.save_conversation_messages(
        conv.id, [*conv.messages, *({"role": "user", "content": f"a{i}"} for i in range(29))]
    )
    h.db.save_draft(
        conv.id,
        {
            "items": [{"dish_id": "kaphrao", "qty": 1, "option_ids": ["chicken"]}],
            "fulfillment": "pickup",
        },
    )
    snap, _ = build_card_snapshot_for_conv(h.db, h.db.get_or_create_conversation("U_30"), h.menu)
    assert snap is not None
    h.postback("U_30", f"action=confirm&card_id={snap['card_id']}")  # compacts the history
    conv = h.db.get_or_create_conversation("U_30")
    h.db.save_conversation_messages(conv.id, [*conv.messages, {"role": "user", "content": "a29"}])
    h.line.replies.clear()
    h.text("U_30", "ข้อความที่ 31")
    assert h.line.replies[-1][1] == [{"type": "text", "text": TURN_LIMIT_REPLY}]
    assert h.llm.calls == []


def test_owner_payment_and_quiet_pin_reach_the_next_llm_call(tmp_path: Path) -> None:
    """AGENTS.md: exact next-LLM payload after out-of-band changes (picker, quiet pin)."""
    h = Harness(tmp_path)
    conv = h.db.get_or_create_conversation("U_A")
    h.db.save_draft(
        conv.id,
        {
            "items": [{"dish_id": "kaphrao", "qty": 1, "option_ids": ["chicken"]}],
            "fulfillment": "pickup",
        },
    )
    snap, _ = build_card_snapshot_for_conv(h.db, h.db.get_or_create_conversation("U_A"), h.menu)
    assert snap is not None
    order = h.db.confirm_card("U_A", snap["card_id"], h.menu).order
    assert order is not None
    card_id, _ = h.db.open_handover("U_A", "slip_amount_mismatch", "สลิป 40฿ · ยอดค้าง 50฿")
    h.send(
        "U_A", {"type": "location", "address": "45/2 หมู่ 3", "latitude": 13.75, "longitude": 100.5}
    )
    res = h.client.post(
        f"/api/kitchen/handovers/{card_id}/mark-paid{KEY}", json={"order_ids": [order.id]}
    )
    assert res.status_code == 200
    h.client.post(f"/api/kitchen/handovers/{card_id}/resolve{KEY}")
    h.llm.responses.append(_resp(content="ได้ค่ะ"))
    h.text("U_A", "ขอบคุณค่ะ")
    sent = h.llm.calls[-1]["messages"]
    assert sent[1]["content"].endswith(  # the picker's payment (+ the quiet pin, polish §3.5)
        "| ยอดค้างชำระ=0 บาท | วิธีรับ=ส่ง (มีที่อยู่+พิกัดแล้ว)"
    )
    assert sent[-2:] == [
        {"role": "user", "content": "[ลูกค้าส่งพิกัดจัดส่ง: 45/2 หมู่ 3]"},
        {"role": "user", "content": "ขอบคุณค่ะ"},
    ]


def _draft_ready(h: Harness, user_id: str) -> None:
    conv = h.db.get_or_create_conversation(user_id)
    h.db.save_draft(
        conv.id,
        {
            "items": [{"dish_id": "kaphrao", "qty": 1, "option_ids": ["chicken"]}],
            "fulfillment": "pickup",
        },
    )


def test_an_order_card_the_customer_never_saw_is_cancelled(tmp_path: Path) -> None:
    """Tier 3 review B #1: an unsent card must not stay confirmable (e.g. by a slip)."""
    build = _resp(tool_calls=[_tool_call("b1", "build_order_card", {})])

    # (a) the LLM fails after building the card
    h = Harness(tmp_path / "a")
    _draft_ready(h, "U_A")
    calls = iter([build])

    def failing(**kwargs: Any) -> Any:
        try:
            return next(calls)
        except StopIteration:
            raise TimeoutError("LLM timed out") from None

    h.llm.chat = SimpleNamespace(completions=SimpleNamespace(create=failing))
    h.text("U_A", "สั่งเลย")
    assert h.line.replies[-1][1] == [{"type": "text", "text": ERROR_REPLY}]
    assert h.db.get_or_create_conversation("U_A").active_card is None

    # (b) the owner taps คุยเอง while the LLM writes the reply after building the card
    h = Harness(tmp_path / "b")
    _draft_ready(h, "U_A")
    replies = iter([build, _resp(content="ตรวจสอบการ์ดได้เลยค่ะ")])

    def takeover(**kwargs: Any) -> Any:
        resp = next(replies)
        if resp is not build:
            h.db.open_handover("U_A", "owner_takeover", "#1")
        return resp

    h.llm.chat = SimpleNamespace(completions=SimpleNamespace(create=takeover))
    h.text("U_A", "สั่งเลย")
    assert h.line.replies == []
    assert h.db.get_or_create_conversation("U_A").active_card is None

    # (c) LINE fails to deliver the reply that carried the card
    h = Harness(tmp_path / "c")
    _draft_ready(h, "U_A")
    h.llm.responses += [build, _resp(content="ตรวจสอบการ์ดได้เลยค่ะ")]

    def broken_reply(reply_token: str, messages: list[dict[str, Any]]) -> None:
        raise ConnectionError("LINE down")

    h.line.reply_messages = broken_reply  # type: ignore[method-assign]
    h.text("U_A", "สั่งเลย")
    assert h.db.get_or_create_conversation("U_A").active_card is None


def test_takeover_before_tools_run_skips_them(tmp_path: Path) -> None:
    """Tier 3 review B #3: tools the LLM asked for after คุยเอง never run."""
    h = Harness(tmp_path)
    _draft_ready(h, "U_A")
    snap, _ = build_card_snapshot_for_conv(h.db, h.db.get_or_create_conversation("U_A"), h.menu)
    assert snap is not None
    order = h.db.confirm_card("U_A", snap["card_id"], h.menu).order
    assert order is not None

    def create(**kwargs: Any) -> Any:
        h.db.open_handover("U_A", "owner_takeover", "#1")
        return _resp(tool_calls=[_tool_call("c1", "cancel_order", {"order_id": order.id})])

    h.llm.chat = SimpleNamespace(completions=SimpleNamespace(create=create))
    h.text("U_A", "ยกเลิก #1")
    assert h.line.replies == []
    assert h.db.get_order(order.id).status == "queued"  # type: ignore[union-attr]


def test_resume_note_never_clears_a_takeover_started_meanwhile(tmp_path: Path) -> None:
    """Tier 3 review B #2: consuming the note only clears a quiet window that is over."""
    h = Harness(tmp_path)
    h.off_menu_hand_over("U_A")
    h.clock.now = T0 + timedelta(minutes=20)  # quiet is over, the note is owed
    stale = h.db.get_or_create_conversation("U_A")
    h.db.open_handover("U_A", "owner_takeover", "#1")  # the owner taps คุยเอง meanwhile
    assert h.db.consume_resume_note(stale) is False
    assert h.db.is_handed_over(h.db.get_or_create_conversation("U_A")) is True


# --- Agent polish Ticket 1 (docs/specs/2026-11-26-agent-polish.md §4) ---

KAPHRAO = {"dish_id": "kaphrao", "qty": 1, "option_ids": ["crispy_pork"]}
WATER = {"dish_id": "water", "qty": 1, "option_ids": []}
BUILD_PICKUP_CARD = [
    _tool_call("c1", "update_cart", {"action": "add", "items": [KAPHRAO]}),
    _tool_call("c2", "set_fulfillment", {"fulfillment": "pickup"}),
    _tool_call("c3", "build_order_card", {}),
]
# A question word keeps the second LLM call that these tests are about (card fast path §3.1)
ORDER_ASKING = "กะเพราหมูกรอบ 1 รับเองได้ไหมคะ"


def _raw_call(call_id: str, name: str, arguments: str) -> Any:
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=arguments))


def _tool_result(call: dict[str, Any], call_id: str) -> dict[str, Any]:
    msg = next(m for m in reversed(call["messages"]) if m.get("tool_call_id") == call_id)
    result: dict[str, Any] = json.loads(msg["content"])
    return result


def test_llm_response_without_a_message_cancels_the_card_built_earlier(tmp_path: Path) -> None:
    for i, broken in enumerate(
        [SimpleNamespace(choices=[]), SimpleNamespace(choices=[SimpleNamespace(message=None)])]
    ):
        h = Harness(tmp_path / str(i))
        h.llm.responses += [_resp(tool_calls=BUILD_PICKUP_CARD), broken]
        h.text("U_A", ORDER_ASKING)
        assert h.line.replies == [("rt-1", [{"type": "text", "text": ERROR_REPLY}])]
        conv = h.db.get_or_create_conversation("U_A")
        assert conv.active_card_id is None
        assert conv.messages[-1] == {"role": "assistant", "content": ERROR_REPLY}


def test_malformed_hand_over_arguments_never_hand_over(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.llm.responses += [
        _resp(tool_calls=[_raw_call("h1", "hand_over", "{bad")]),
        _resp(content="ขอโทษค่ะ ช่วยพิมพ์อีกครั้งได้ไหมคะ"),
    ]
    h.text("U_A", "แพ้ถั่วไหมคะ")
    assert _tool_result(h.llm.calls[1], "h1") == {"ok": False, "error": BAD_ARGS_ERROR}
    assert h.cards("U_A") == []
    assert h.until("U_A") is None
    assert h.line.replies[-1][1] == [{"type": "text", "text": "ขอโทษค่ะ ช่วยพิมพ์อีกครั้งได้ไหมคะ"}]


def test_card_edited_after_it_was_built_in_the_same_turn_is_never_sent(tmp_path: Path) -> None:
    edits = [
        _tool_call("e1", "update_cart", {"action": "add", "items": [WATER]}),
        _tool_call("e1", "set_fulfillment", {"fulfillment": "delivery", "address": "บ้าน 1"}),
    ]
    for i, edit in enumerate(edits):
        h = Harness(tmp_path / str(i))
        h.llm.responses += [
            _resp(tool_calls=BUILD_PICKUP_CARD),
            _resp(tool_calls=[edit]),
            _resp(content="แก้ไขแล้วค่ะ"),
        ]
        h.text("U_A", ORDER_ASKING)
        assert _tool_result(h.llm.calls[2], "e1")["ok"] is True
        sent = h.line.replies[-1][1]
        assert sent == [{"type": "text", "text": "แก้ไขแล้วค่ะ"}]  # no stale Flex card
        assert h.db.get_or_create_conversation("U_A").active_card_id is None


def test_update_cart_checks_the_whole_cart_before_saving(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    conv = h.db.get_or_create_conversation("U_A")
    h.db.save_draft(conv.id, {"items": [KAPHRAO], "fulfillment": "pickup"})
    snap, _ = build_card_snapshot_for_conv(h.db, h.db.get_or_create_conversation("U_A"), h.menu)
    assert snap is not None
    h.db.set_override("dish", "kaphrao", False)  # sold out after it was added
    add_water = _tool_call("u1", "update_cart", {"action": "add", "items": [WATER]})
    h.llm.responses += [_resp(tool_calls=[add_water]), _resp(content="ข้าวกะเพราหมดแล้วค่ะ")]
    h.text("U_A", "เอาน้ำเปล่าด้วย")
    assert _tool_result(h.llm.calls[1], "u1") == {"ok": False, "error": "ข้าวกะเพรา หมดแล้วค่ะ"}
    after = h.db.get_or_create_conversation("U_A")
    assert after.draft["items"] == [KAPHRAO]  # no duplicate or extra line
    assert after.active_card_id == snap["card_id"]
    assert after.draft_revision == conv.draft_revision + 1  # only the setup save

    # 20 lines is the maximum: a 21st line is refused and nothing is saved
    h.db.set_override("dish", "kaphrao", True)
    h.db.save_draft(after.id, {"items": [WATER] * 20, "fulfillment": "pickup"})
    h.llm.responses += [_resp(tool_calls=[add_water]), _resp(content="สั่งได้ไม่เกิน 20 รายการค่ะ")]
    h.text("U_A", "เอาน้ำเปล่าอีกขวด")
    assert _tool_result(h.llm.calls[3], "u1") == {
        "ok": False,
        "error": "สั่งได้ไม่เกิน 20 รายการต่อออเดอร์ค่ะ",
    }
    assert len(h.db.get_or_create_conversation("U_A").draft["items"]) == 20


def test_cancel_tap_clears_the_items_and_keeps_delivery_details(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    conv = h.db.get_or_create_conversation("U_A")
    pin = "https://maps.google.com/?q=13.7,100.5"
    h.db.save_draft(
        conv.id,
        {"items": [KAPHRAO], "fulfillment": "delivery", "address": "บ้าน 99", "location_url": pin},
    )
    old, _ = build_card_snapshot_for_conv(h.db, h.db.get_or_create_conversation("U_A"), h.menu)
    assert old is not None
    rev = h.db.get_or_create_conversation("U_A").draft_revision
    h.postback("U_A", f"action=cancel&card_id={old['card_id']}")
    after = h.db.get_or_create_conversation("U_A")
    assert after.active_card_id is None
    assert after.draft == {
        "items": [],
        "fulfillment": "delivery",
        "address": "บ้าน 99",
        "location_url": pin,
    }
    assert after.draft_revision == rev + 1
    assert h.line.replies[-1][1][0]["text"] == "ยกเลิกสรุปออเดอร์นี้แล้วค่ะ พิมพ์สั่งใหม่ได้เลยนะคะ"

    # A stale Cancel tap leaves the newer draft and card alone
    h.db.save_draft(after.id, {**after.draft, "items": [WATER]})
    new, _ = build_card_snapshot_for_conv(h.db, h.db.get_or_create_conversation("U_A"), h.menu)
    assert new is not None
    before = h.db.get_or_create_conversation("U_A")
    h.postback("U_A", f"action=cancel&card_id={old['card_id']}")
    stale = h.db.get_or_create_conversation("U_A")
    assert (stale.active_card_id, stale.draft, stale.draft_revision) == (
        new["card_id"],
        before.draft,
        before.draft_revision,
    )
    assert h.line.replies[-1][1][0]["text"] == "การ์ดนี้ถูกยกเลิกหรือยืนยันไปแล้วค่ะ"


def test_undelivered_text_or_location_card_keeps_the_items(tmp_path: Path) -> None:
    def broken_reply(reply_token: str, messages: list[dict[str, Any]]) -> None:
        raise ConnectionError("LINE down")

    h = Harness(tmp_path / "text")
    h.llm.responses += [_resp(tool_calls=BUILD_PICKUP_CARD), _resp(content="กดยืนยันได้เลยค่ะ")]
    h.line.reply_messages = broken_reply  # type: ignore[method-assign]
    h.text("U_A", "กะเพราหมูกรอบ 1 รับเอง")
    conv = h.db.get_or_create_conversation("U_A")
    assert conv.active_card_id is None
    assert conv.draft["items"] == [{**KAPHRAO, "note": ""}]

    h = Harness(tmp_path / "location")
    _draft_ready(h, "U_A")
    h.line.reply_messages = broken_reply  # type: ignore[method-assign]
    h.send("U_A", {"type": "location", "address": "บ้าน 9", "latitude": 13.7, "longitude": 100.5})
    conv = h.db.get_or_create_conversation("U_A")
    assert conv.active_card_id is None
    assert conv.draft["items"] == [{"dish_id": "kaphrao", "qty": 1, "option_ids": ["chicken"]}]


def test_resume_note_stays_owed_when_the_first_llm_call_fails(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.off_menu_hand_over("U_A")
    h.clock.now = T0 + timedelta(minutes=20)  # quiet is over, the note is owed

    def failing(**kwargs: Any) -> Any:
        h.llm.calls.append(kwargs)
        raise TimeoutError("LLM timed out")

    h.llm.chat = SimpleNamespace(completions=SimpleNamespace(create=failing))
    h.text("U_A", "ข้อความแรก")
    assert {"role": "system", "content": RESUME_NOTE} in h.llm.calls[-1]["messages"]
    assert h.line.replies[-1][1] == [{"type": "text", "text": ERROR_REPLY}]

    h.llm.chat = SimpleNamespace(completions=SimpleNamespace(create=h.llm._create))
    h.llm.responses += [_resp(content="รับอะไรดีคะ"), _resp(content="ได้ค่ะ")]
    h.text("U_A", "ข้อความสอง")
    assert {"role": "system", "content": RESUME_NOTE} in h.llm.calls[-1]["messages"]
    h.text("U_A", "ข้อความสาม")  # given once, after the call that succeeded
    assert {"role": "system", "content": RESUME_NOTE} not in h.llm.calls[-1]["messages"]


def test_hand_over_in_a_batch_runs_none_of_the_other_tools(tmp_path: Path) -> None:
    # [cancel_order, hand_over(edit_confirmed)]: the queued order is not cancelled
    h = Harness(tmp_path / "cancel")
    _draft_ready(h, "U_A")
    snap, _ = build_card_snapshot_for_conv(h.db, h.db.get_or_create_conversation("U_A"), h.menu)
    assert snap is not None
    order = h.db.confirm_card("U_A", snap["card_id"], h.menu).order
    assert order is not None
    h.llm.responses.append(
        _resp(
            tool_calls=[
                _tool_call("t1", "cancel_order", {"order_id": order.id}),
                _tool_call("t2", "hand_over", {"reason": "edit_confirmed", "queue_number": 1}),
            ]
        )
    )
    h.text("U_A", "ขอแก้ #1 เป็นไก่")
    assert h.db.get_order(order.id).status == "queued"  # type: ignore[union-attr]
    assert h.line.replies[-1][1] == [{"type": "text", "text": HANDOVER_REPLY}]
    assert h.cards("U_A")[0]["reasons"] == ["edit_confirmed"]

    # [update_cart, hand_over(off_menu)]: the draft is not changed
    h = Harness(tmp_path / "cart")
    h.llm.responses.append(
        _resp(
            tool_calls=[
                _tool_call("t1", "update_cart", {"action": "add", "items": [KAPHRAO]}),
                _tool_call("t2", "hand_over", {"reason": "off_menu"}),
            ]
        )
    )
    h.text("U_A", "กะเพราหมูกรอบ แพ้ถั่วด้วยค่ะ")
    conv = h.db.get_or_create_conversation("U_A")
    assert conv.draft == {} and conv.draft_revision == 0
    assert h.line.replies[-1][1] == [{"type": "text", "text": HANDOVER_REPLY}]
    assert h.cards("U_A")[0]["reasons"] == ["off_menu"]


def test_paid_cancel_request_stops_the_rest_of_the_batch(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    _draft_ready(h, "U_A")
    snap, _ = build_card_snapshot_for_conv(h.db, h.db.get_or_create_conversation("U_A"), h.menu)
    assert snap is not None
    order = h.db.confirm_card("U_A", snap["card_id"], h.menu).order
    assert order is not None
    h.db.set_payment_status(order.id, "transfer_manual")
    h.llm.responses.append(
        _resp(
            tool_calls=[
                _tool_call("t1", "cancel_order", {"order_id": order.id}),
                _tool_call("t2", "update_cart", {"action": "add", "items": [KAPHRAO]}),
            ]
        )
    )
    h.text("U_A", "ยกเลิก #1 แล้วสั่งกะเพราแทน")
    assert not h.db.get_or_create_conversation("U_A").draft.get("items")  # update_cart never ran
    assert h.line.replies[-1][1] == [{"type": "text", "text": HANDOVER_REPLY}]
    assert h.cards("U_A")[0]["reasons"] == ["paid_cancel_request"]


def test_pause_during_the_llm_call_sends_nothing_and_cancels_the_card(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    replies = iter([_resp(tool_calls=BUILD_PICKUP_CARD), _resp(content="กดยืนยันได้เลยค่ะ")])

    def pause_on_second_call(**kwargs: Any) -> Any:
        h.llm.calls.append(kwargs)
        if len(h.llm.calls) == 2:
            h.db.set_bot_enabled(False)  # the owner pauses the bot meanwhile
        return next(replies)

    h.llm.chat = SimpleNamespace(completions=SimpleNamespace(create=pause_on_second_call))
    h.text("U_A", ORDER_ASKING)
    assert h.line.replies == []
    conv = h.db.get_or_create_conversation("U_A")
    assert conv.active_card_id is None
    assert conv.messages == [{"role": "user", "content": ORDER_ASKING}]
    assert h.state()["inbox"] == []


def test_bold_markers_are_stripped_but_queue_numbers_kept(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.llm.responses.append(_resp(content="สั่ง **กะเพราหมูกรอบ** คิว #3 ค่ะ"))
    h.text("U_A", "สั่งอะไรไป")
    assert h.line.replies[-1][1] == [{"type": "text", "text": "สั่ง กะเพราหมูกรอบ คิว #3 ค่ะ"}]
    saved = h.db.get_or_create_conversation("U_A").messages[-1]
    assert saved == {"role": "assistant", "content": "สั่ง กะเพราหมูกรอบ คิว #3 ค่ะ"}


def test_broken_hand_over_calls_never_hand_over_or_block_the_batch(tmp_path: Path) -> None:
    """Review fixes: empty args, an unknown reason, and a huge integer are all tool errors."""
    for i, args in enumerate(["", '{"reason": "unsupported"}', '{"reason": 1' + "1" * 5000 + "}"]):
        h = Harness(tmp_path / str(i))
        h.llm.responses += [
            _resp(
                tool_calls=[
                    _tool_call("t1", "update_cart", {"action": "add", "items": [KAPHRAO]}),
                    _raw_call("t2", "hand_over", args),
                ]
            ),
            _resp(content="เพิ่มกะเพราหมูกรอบแล้วค่ะ"),
        ]
        h.text("U_A", "กะเพราหมูกรอบ 1")
        assert _tool_result(h.llm.calls[1], "t2")["ok"] is False
        assert h.cards("U_A") == [] and h.until("U_A") is None
        assert h.db.get_or_create_conversation("U_A").draft["items"] == [{**KAPHRAO, "note": ""}]
        assert h.line.replies[-1][1] == [{"type": "text", "text": "เพิ่มกะเพราหมูกรอบแล้วค่ะ"}]


def test_non_text_llm_content_after_a_card_does_not_crash_the_turn(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.llm.responses += [
        _resp(tool_calls=BUILD_PICKUP_CARD),
        SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=[{"x": 1}], tool_calls=None))]
        ),
    ]
    h.text("U_A", ORDER_ASKING)
    sent = h.line.replies[-1][1]
    assert [m["type"] for m in sent] == ["flex"]  # the card goes out, with no crash
    assert h.db.get_or_create_conversation("U_A").active_card_id is not None


def test_location_card_changed_while_rendering_is_not_sent(
    tmp_path: Path, monkeypatch: Any
) -> None:
    import app.agent as agent_mod

    h = Harness(tmp_path)
    _draft_ready(h, "U_A")
    real = agent_mod.card_wait_line

    def draft_write_meanwhile(db: ShopDB, menu: Any, snapshot: dict[str, Any]) -> str:
        conv = db.get_or_create_conversation("U_A")
        db.save_draft(conv.id, {**conv.draft, "items": [WATER]})  # e.g. a parallel write
        return real(db, menu, snapshot)

    monkeypatch.setattr(agent_mod, "card_wait_line", draft_write_meanwhile)
    h.send("U_A", {"type": "location", "address": "บ้าน 9", "latitude": 13.7, "longitude": 100.5})
    assert [m["type"] for m in h.line.replies[-1][1]] == ["text"]
    assert h.db.get_or_create_conversation("U_A").active_card_id is None


def test_turn_never_clears_a_newer_resume_note(tmp_path: Path) -> None:
    """Review fix: a takeover started and resolved during the LLM call still owes its note."""
    h = Harness(tmp_path)
    h.off_menu_hand_over("U_A")
    h.clock.now = T0 + timedelta(minutes=20)  # the first note is owed

    def takeover_and_resolve(**kwargs: Any) -> Any:
        h.llm.calls.append(kwargs)
        h.db.open_handover("U_A", "owner_takeover", "#1")
        h.clock.now += timedelta(minutes=1)
        assert h.db.resolve_handover(h.cards("U_A")[0]["id"]) is True
        return _resp(content="ได้ค่ะ")

    h.llm.chat = SimpleNamespace(completions=SimpleNamespace(create=takeover_and_resolve))
    h.text("U_A", "ข้อความแรก")
    h.llm.chat = SimpleNamespace(completions=SimpleNamespace(create=h.llm._create))
    h.llm.responses.append(_resp(content="รับอะไรดีคะ"))
    h.text("U_A", "ข้อความสอง")
    assert {"role": "system", "content": RESUME_NOTE} in h.llm.calls[-1]["messages"]


def test_failed_reply_tells_the_next_llm_turn_the_customer_never_saw_it(tmp_path: Path) -> None:
    """Ticket 1 review (I4): history must not claim a card reached the customer."""
    from app.main import undelivered_note

    note = undelivered_note("กดยืนยันได้เลยค่ะ")
    assert note == '[ระบบ: ข้อความตอบกลับของบอท "กดยืนยันได้เลยค่ะ…" ส่งไม่ถึงลูกค้า ลูกค้าไม่เห็นข้อความและการ์ดนั้น]'
    h = Harness(tmp_path)
    h.llm.responses += [_resp(tool_calls=BUILD_PICKUP_CARD), _resp(content="กดยืนยันได้เลยค่ะ")]
    real_reply = h.line.reply_messages

    def broken_reply(reply_token: str, messages: list[dict[str, Any]]) -> None:
        raise ConnectionError("LINE down")

    h.line.reply_messages = broken_reply  # type: ignore[method-assign]
    h.text("U_A", ORDER_ASKING)
    conv = h.db.get_or_create_conversation("U_A")
    assert conv.active_card_id is None
    assert conv.messages[-2:] == [
        {"role": "assistant", "content": "กดยืนยันได้เลยค่ะ"},
        {"role": "user", "content": note},
    ]
    h.line.reply_messages = real_reply  # type: ignore[method-assign]
    h.llm.responses.append(_resp(content="ส่งการ์ดใหม่ให้นะคะ"))
    h.text("U_A", "ไม่เห็นการ์ดเลยค่ะ")
    assert h.llm.calls[-1]["messages"][-2:] == [
        {"role": "user", "content": note},
        {"role": "user", "content": "ไม่เห็นการ์ดเลยค่ะ"},
    ]

    # The location path records the same note
    h2 = Harness(tmp_path / "loc")
    _draft_ready(h2, "U_A")
    h2.line.reply_messages = broken_reply  # type: ignore[method-assign]
    h2.send("U_A", {"type": "location", "address": "บ้าน 9", "latitude": 13.7, "longitude": 100.5})
    assert (
        h2.db.get_or_create_conversation("U_A")
        .messages[-1]["content"]
        .startswith('[ระบบ: ข้อความตอบกลับของบอท "บันทึกพิกัดจัดส่ง (บ้าน 9)')
    )


def test_card_built_before_a_failed_edit_in_the_batch_is_not_sent(tmp_path: Path) -> None:
    """Final re-review B1: [build, failing edits] must not send the old cart's card."""
    h = Harness(tmp_path)
    _draft_ready(h, "U_A")
    h.llm.responses += [
        _resp(
            tool_calls=[
                _tool_call("b1", "build_order_card", {}),
                _tool_call(
                    "u1", "update_cart", {"action": "add", "items": [{"dish_id": "nope", "qty": 1}]}
                ),
            ]
        ),
        _resp(content="ไม่มีเมนูนี้ค่ะ"),
    ]
    h.text("U_A", "เพิ่มเมนูลับด้วย")
    assert h.line.replies[-1][1] == [{"type": "text", "text": "ไม่มีเมนูนี้ค่ะ"}]
    assert h.db.get_or_create_conversation("U_A").active_card_id is None
    assert _tool_result(h.llm.calls[1], "b1") == {"ok": False, "error": CARD_AFTER_FAILED_EDIT}


def test_card_is_cancelled_when_the_turn_crashes_or_cannot_reply(tmp_path: Path) -> None:
    """Final re-review B2: a card the customer never got never stays confirmable."""
    from app.main import undelivered_note

    # (a) a crash after the card was saved (here: the DB fails on the next status read)
    h = Harness(tmp_path / "crash")
    _draft_ready(h, "U_A")
    real_status = h.db.get_shop_status
    calls = iter([_resp(tool_calls=[_tool_call("b1", "build_order_card", {})])])

    def create(**kwargs: Any) -> Any:
        try:
            return next(calls)
        except StopIteration:
            h.db.get_shop_status = boom  # type: ignore[method-assign]
            return _resp(content="ตรวจสอบการ์ดได้เลยค่ะ")

    def boom(menu: Any) -> Any:
        raise RuntimeError("database is locked")

    h.llm.chat = SimpleNamespace(completions=SimpleNamespace(create=create))
    h.text("U_A", "สั่งเลย")
    h.db.get_shop_status = real_status  # type: ignore[method-assign]
    assert h.line.replies == []
    assert h.db.get_or_create_conversation("U_A").active_card_id is None

    # (b) no reply token: nothing can be sent, so the card is cancelled and history says so
    h = Harness(tmp_path / "token")
    _draft_ready(h, "U_A")
    h.llm.responses += [
        _resp(tool_calls=[_tool_call("b1", "build_order_card", {})]),
        _resp(content="ตรวจสอบการ์ดได้เลยค่ะ"),
    ]
    h._post(
        {
            "webhookEventId": "evt-no-token",
            "type": "message",
            "source": {"type": "user", "userId": "U_A"},
            "message": {"id": "m-x", "type": "text", "text": "สั่งเลย"},
        }
    )
    conv = h.db.get_or_create_conversation("U_A")
    assert h.line.replies == [] and conv.active_card_id is None
    assert conv.messages[-1] == {"role": "user", "content": undelivered_note("ตรวจสอบการ์ดได้เลยค่ะ")}


def test_malformed_tool_names_are_not_replayed_and_notes_hide_delivery_data(
    tmp_path: Path,
) -> None:
    """Final re-review I1 + I3."""
    h = Harness(tmp_path)
    h.db.upsert_customer("U_A", last_address="บ้าน 45/2 ซอยลับ", update_address=True)
    conv = h.db.get_or_create_conversation("U_A")
    note = "ส่ง บ้าน 45/2 ซอยลับ https://maps.app/x"
    h.db.save_draft(conv.id, {"items": [{**WATER, "note": note}], "fulfillment": "pickup"})
    odd = SimpleNamespace(id="x1", function=SimpleNamespace(name=["x"], arguments=None))
    h.llm.responses += [_resp(tool_calls=[odd]), _resp(content="ค่ะ")]
    h.text("U_A", "โอเค")
    replayed = next(m for m in h.llm.calls[1]["messages"] if m.get("tool_calls"))
    assert replayed["tool_calls"][0]["function"] == {"name": "invalid", "arguments": "{}"}
    live = h.llm.calls[0]["messages"][1]["content"]
    assert "45/2" not in live and "maps" not in live
    assert 'โน้ตลูกค้า "ส่ง … …"' in live


def test_fix_verification_edge_cases(tmp_path: Path) -> None:
    """Fix-verification pass: cross-batch card drop, non-string args, address masking."""
    from app.agent import _cart_text

    # B1: [build] then [failed edit, build] in one turn -> no card is sent or left active
    h = Harness(tmp_path / "b1")
    _draft_ready(h, "U_A")
    bad_add = {"action": "add", "items": [{"dish_id": "nope", "qty": 1}]}
    h.llm.responses += [
        _resp(tool_calls=[_tool_call("b1", "build_order_card", {})]),
        _resp(
            tool_calls=[
                _tool_call("u1", "update_cart", bad_add),
                _tool_call("b2", "build_order_card", {}),
            ]
        ),
        _resp(content="ไม่มีเมนูนี้ค่ะ"),
    ]
    h.text("U_A", "สั่งเลย แล้วเพิ่มเมนูลับ")
    assert h.line.replies[-1][1] == [{"type": "text", "text": "ไม่มีเมนูนี้ค่ะ"}]
    assert h.db.get_or_create_conversation("U_A").active_card_id is None

    # I1: arguments that are not a JSON string never run the tool
    h = Harness(tmp_path / "i1")
    _draft_ready(h, "U_A")
    odd = SimpleNamespace(id="x1", function=SimpleNamespace(name="build_order_card", arguments={}))
    h.llm.responses += [_resp(tool_calls=[odd]), _resp(content="ค่ะ")]
    h.text("U_A", "สั่งเลย")
    assert [m["type"] for m in h.line.replies[-1][1]] == ["text"]
    assert _tool_result(h.llm.calls[1], "x1") == {"ok": False, "error": BAD_ARGS_ERROR}

    # I3: the longer address is masked first, and a link inside an address doesn't leak it
    menu = load_menu(MENU_PATH)
    items = [{"dish_id": "water", "qty": 1, "note": "ส่ง บ้าน 45/2 ซอยลับ นะ"}]
    assert "45" not in _cart_text(menu, items, hide=("บ้าน 45", "บ้าน 45/2 ซอยลับ"))
    linked = "บ้าน 9 https://maps.app/x"
    text = _cart_text(
        menu, [{"dish_id": "water", "qty": 1, "note": f"ส่ง {linked}"}], hide=(linked,)
    )
    assert "บ้าน 9" not in text and "maps" not in text


def test_no_line_access_token_counts_as_not_delivered(tmp_path: Path) -> None:
    """Fix-verification B2: with no access token the app replies through NoopLineClient,
    which sends nothing, so the card is cancelled like any undelivered one."""
    h = Harness(tmp_path)
    _draft_ready(h, "U_A")
    app = create_app(
        settings=h.settings.__class__(**{**h.settings.__dict__, "line_channel_access_token": ""}),
        db=h.db,
        menu=h.menu,
        llm_client=h.llm,
    )
    h.client = TestClient(app)
    h.llm.responses += [
        _resp(tool_calls=[_tool_call("b1", "build_order_card", {})]),
        _resp(content="ตรวจสอบการ์ดได้เลยค่ะ"),
    ]
    h.text("U_A", "สั่งเลย")
    assert h.db.get_or_create_conversation("U_A").active_card_id is None


def test_balance_check_runs_before_the_reply_is_saved(tmp_path: Path) -> None:
    """Fix-verification I4: a crash in the final QR check leaves no unsent reply in history."""
    h = Harness(tmp_path)
    h.settings = h.settings.__class__(
        **{
            **h.settings.__dict__,
            "promptpay_id": "0812345678",
            "public_base_url": "https://x.example",
        }
    )
    h.client = TestClient(
        create_app(settings=h.settings, db=h.db, menu=h.menu, line_client=h.line, llm_client=h.llm)
    )
    _draft_ready(h, "U_A")
    snap, _ = build_card_snapshot_for_conv(h.db, h.db.get_or_create_conversation("U_A"), h.menu)
    assert snap is not None and h.db.confirm_card("U_A", snap["card_id"], h.menu).ok
    real = h.db.get_unpaid_balance
    broken = {"on": False}

    def flaky(uid: str) -> Any:
        if broken["on"]:  # the final QR balance check, after the last LLM call
            raise RuntimeError("database is locked")
        return real(uid)

    replies = iter(
        [
            _resp(tool_calls=[_tool_call("p1", "get_payment_info", {"send_qr": True})]),
            _resp(content="ยอด 50 บาทค่ะ สแกน QR ได้เลย"),
        ]
    )

    def create(**kwargs: Any) -> Any:
        resp = next(replies)
        broken["on"] = resp.choices[0].message.tool_calls is None
        return resp

    h.llm.chat = SimpleNamespace(completions=SimpleNamespace(create=create))
    h.db.get_unpaid_balance = flaky  # type: ignore[method-assign,assignment]
    h.text("U_A", "ขอ QR")
    h.db.get_unpaid_balance = real  # type: ignore[method-assign]
    assert h.line.replies == []
    saved = h.db.get_or_create_conversation("U_A").messages
    assert {"role": "assistant", "content": "ยอด 50 บาทค่ะ สแกน QR ได้เลย"} not in saved


def test_fresh_review_batch_order_and_cleared_cards(tmp_path: Path) -> None:
    """Fresh review: a build listed before its edits still shows the edited cart, and an
    edit that cancels an earlier card tells the LLM so."""
    from app.agent import CARD_CLEARED_NOTE

    h = Harness(tmp_path / "order")
    _draft_ready(h, "U_A")
    h.llm.responses += [
        _resp(
            tool_calls=[
                _tool_call("b1", "build_order_card", {}),
                _tool_call("u1", "update_cart", {"action": "add", "items": [WATER]}),
                _tool_call("f1", "set_fulfillment", {"fulfillment": "pickup"}),
            ]
        ),
        _resp(content="ตรวจสอบการ์ดได้เลยค่ะ"),
    ]
    h.text("U_A", "เพิ่มน้ำเปล่า รับเองได้ไหม")  # a question word keeps the 2nd call
    sent = h.line.replies[-1][1]
    assert [m["type"] for m in sent] == ["text", "flex"]
    assert "น้ำเปล่า" in json.dumps(sent[1], ensure_ascii=False)  # the edited cart
    assert [m["tool_call_id"] for m in h.llm.calls[1]["messages"][-3:]] == ["b1", "u1", "f1"]

    h = Harness(tmp_path / "cleared")
    _draft_ready(h, "U_A")
    h.llm.responses += [
        _resp(tool_calls=[_tool_call("b1", "build_order_card", {})]),
        _resp(tool_calls=[_tool_call("u1", "update_cart", {"action": "add", "items": [WATER]})]),
        _resp(content="เพิ่มน้ำเปล่าแล้วค่ะ"),
    ]
    h.text("U_A", "สั่งเลย แล้วเพิ่มน้ำด้วย")
    assert _tool_result(h.llm.calls[2], "u1")["card_note"] == CARD_CLEARED_NOTE
    assert [m["type"] for m in h.line.replies[-1][1]] == ["text"]


def test_fresh_review_tool_entry_without_function_gets_the_error_reply(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.llm.responses.append(_resp(tool_calls=[SimpleNamespace(id="x1", function=None)]))
    h.text("U_A", "สั่งเลย")
    assert h.line.replies[-1][1] == [{"type": "text", "text": ERROR_REPLY}]


def test_fresh_review_failed_cancel_reply_is_marked_in_history(tmp_path: Path) -> None:
    from app.main import undelivered_note

    h = Harness(tmp_path)
    _draft_ready(h, "U_A")
    snap, _ = build_card_snapshot_for_conv(h.db, h.db.get_or_create_conversation("U_A"), h.menu)
    assert snap is not None

    def broken_reply(reply_token: str, messages: list[dict[str, Any]]) -> None:
        raise ConnectionError("LINE down")

    h.line.reply_messages = broken_reply  # type: ignore[method-assign]
    h.postback("U_A", f"action=cancel&card_id={snap['card_id']}")
    conv = h.db.get_or_create_conversation("U_A")
    assert conv.draft["items"] == []  # the cart is still cleared
    assert conv.messages[-1] == {
        "role": "user",
        "content": undelivered_note("ยกเลิกสรุปออเดอร์นี้แล้วค่ะ พิมพ์สั่งใหม่ได้เลยนะคะ"),
    }


def test_fix_review_resume_note_and_card_note_on_failed_fulfillment(tmp_path: Path) -> None:
    from app.agent import CARD_CLEARED_NOTE

    # A malformed first reply after a hand-over leaves the resume note owed
    h = Harness(tmp_path / "resume")
    h.off_menu_hand_over("U_A")
    h.clock.now = T0 + timedelta(minutes=20)
    h.llm.responses += [
        _resp(tool_calls=[SimpleNamespace(id="x1", function=None)]),
        _resp(content="รับอะไรดีคะ"),
    ]
    h.text("U_A", "ข้อความแรก")
    assert h.line.replies[-1][1] == [{"type": "text", "text": ERROR_REPLY}]
    h.text("U_A", "ข้อความสอง")
    assert {"role": "system", "content": RESUME_NOTE} in h.llm.calls[-1]["messages"]

    # Switching to delivery with no address clears the earlier card: the LLM is told
    h = Harness(tmp_path / "ff")
    _draft_ready(h, "U_A")
    h.llm.responses += [
        _resp(tool_calls=[_tool_call("b1", "build_order_card", {})]),
        _resp(tool_calls=[_tool_call("f1", "set_fulfillment", {"fulfillment": "delivery"})]),
        _resp(content="ขอที่อยู่จัดส่งด้วยค่ะ"),
    ]
    h.text("U_A", "สั่งเลย เปลี่ยนเป็นส่งนะ")
    result = _tool_result(h.llm.calls[2], "f1")
    assert result["ok"] is False and result["card_note"] == CARD_CLEARED_NOTE
    assert [m["type"] for m in h.line.replies[-1][1]] == ["text"]
    assert h.db.get_or_create_conversation("U_A").active_card_id is None


# --- Card fast path (docs/specs/2026-11-26-card-fast-path.md, Ticket 1) ---


# The owner-approved lines (spec §3.2), written out so a wording change fails the tests
LINE_PICKUP = "ตรวจสอบรายการบนการ์ดแล้วกดยืนยันได้เลยนะคะ"
LINE_SAVED = "ส่งที่เดิมนะคะ ถ้าเปลี่ยนที่อยู่บอกได้เลยค่ะ\nตรวจสอบรายการบนการ์ดแล้วกดยืนยันได้เลยนะคะ"
LINE_NEW = "ตรวจสอบรายการและที่อยู่บนการ์ดแล้วกดยืนยันได้เลยนะคะ"


def _card_paths(caplog: pytest.LogCaptureFixture) -> list[tuple[str, str]]:
    """(card_path, llm_calls) from each webhook_event log line."""
    out = []
    for r in caplog.records:
        if r.name == "app.main" and r.getMessage().startswith("webhook_event "):
            f = dict(re.findall(r"(\w+)=(\S+)", r.getMessage()))
            out.append((f["card_path"], f["llm_calls"]))
    return out


def test_fast_path_pickup_order_sends_the_fixed_line_after_one_llm_call(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    h = Harness(tmp_path)
    h.llm.responses.append(_resp(tool_calls=BUILD_PICKUP_CARD))
    h.text("U_A", "กะเพราหมูกรอบ 1 รับเองค่ะ")
    assert len(h.llm.calls) == 1
    ((_, sent),) = h.line.replies
    assert [m["type"] for m in sent] == ["text", "flex"]
    assert sent[0] == {"type": "text", "text": LINE_PICKUP}
    conv = h.db.get_or_create_conversation("U_A")
    assert conv.active_card_id is not None  # the card the customer got stays confirmable
    # History: the tool batch and its results, then exactly the line that was sent
    assert conv.messages[-1] == {"role": "assistant", "content": LINE_PICKUP}
    assert [m["role"] for m in conv.messages[-5:-1]] == ["assistant", "tool", "tool", "tool"]
    assert _card_paths(caplog) == [("fast", "1")]


@pytest.mark.parametrize("case", ["saved", "new"])
def test_fast_path_delivery_line_depends_on_the_address(tmp_path: Path, case: str) -> None:
    h = Harness(tmp_path)
    if case == "saved":
        h.db.upsert_customer("U_A", last_address="บ้าน 45/2", update_address=True)
        ff = {"fulfillment": "delivery", "use_saved_address": True}
    else:
        ff = {"fulfillment": "delivery", "address": "บ้าน 9 ซอย 2"}
    h.llm.responses.append(
        _resp(
            tool_calls=[
                _tool_call("c1", "update_cart", {"action": "add", "items": [KAPHRAO]}),
                _tool_call("c2", "set_fulfillment", ff),
                _tool_call("c3", "build_order_card", {}),
            ]
        )
    )
    h.text("U_A", "กะเพราหมูกรอบ 1 ส่งค่ะ")
    assert len(h.llm.calls) == 1
    expected = LINE_SAVED if case == "saved" else LINE_NEW
    sent = h.line.replies[-1][1]
    assert sent[0] == {"type": "text", "text": expected}
    assert sent[1]["type"] == "flex"


@pytest.mark.parametrize(
    "case", ["extra_tool", "edit_fails", "llm_text", "no_fulfillment_tool", "build_fails"]
)
def test_fast_path_keeps_the_llm_reply_when_any_condition_fails(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, case: str
) -> None:
    caplog.set_level(logging.INFO)
    h = Harness(tmp_path)
    first = _resp(tool_calls=BUILD_PICKUP_CARD)
    if case == "extra_tool":
        first = _resp(tool_calls=[*BUILD_PICKUP_CARD, _tool_call("s1", "get_shop_status", {})])
    elif case == "edit_fails":
        h.db.set_override("dish", "kaphrao", False)  # sold out: update_cart is refused
    elif case == "llm_text":
        first = _resp(content="รับทราบค่ะ", tool_calls=BUILD_PICKUP_CARD)
    elif case == "no_fulfillment_tool":
        _draft_ready(h, "U_A")  # pickup is already set, so the build succeeds
        first = _resp(
            tool_calls=[
                _tool_call("c1", "update_cart", {"action": "add", "items": [WATER]}),
                _tool_call("c3", "build_order_card", {}),
            ]
        )
    elif case == "build_fails":
        # Both edits succeed; the card builder itself refuses the empty cart
        first = _resp(
            tool_calls=[
                _tool_call("c1", "update_cart", {"action": "clear"}),
                _tool_call("c2", "set_fulfillment", {"fulfillment": "pickup"}),
                _tool_call("c3", "build_order_card", {}),
            ]
        )
    h.llm.responses += [first, _resp(content="ข้อความจาก LLM")]
    h.text("U_A", "กะเพราหมูกรอบ 1 รับเองค่ะ")
    if case == "build_fails":
        oks = [_tool_result(h.llm.calls[1], c)["ok"] for c in ("c1", "c2", "c3")]
        assert oks == [True, True, False]
    assert len(h.llm.calls) == 2
    assert h.line.replies[-1][1][0] == {"type": "text", "text": "ข้อความจาก LLM"}
    assert _card_paths(caplog) == [("-", "2")]


@pytest.mark.parametrize(
    ("second", "path"),
    [
        ("line", "qword_same"),
        ("", "qword_same"),
        ("text", "qword_diff"),
        ("hand_over", "qword_diff"),
    ],
)
def test_a_question_word_keeps_the_llm_reply_and_logs_what_it_said(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, second: str, path: str
) -> None:
    caplog.set_level(logging.INFO)
    h = Harness(tmp_path)
    reply = {"line": LINE_PICKUP, "": "", "text": "ได้ค่ะ เผ็ดน้อยนะคะ"}.get(second, "")
    if second == "hand_over":
        follow = _resp(tool_calls=[_tool_call("h1", "hand_over", {"reason": "off_menu"})])
    else:
        follow = _resp(content=reply)
    h.llm.responses += [_resp(tool_calls=BUILD_PICKUP_CARD), follow]
    h.text("U_A", "กะเพราหมูกรอบ 1 เผ็ดน้อยได้ไหมคะ รับเอง")
    assert len(h.llm.calls) == 2
    sent = h.line.replies[-1][1]
    if second in ("line", "text"):
        assert sent[0] == {"type": "text", "text": reply}
    if second == "":
        assert [m["type"] for m in sent] == ["flex"]  # the card alone: nothing more was said
    assert _card_paths(caplog) == [(path, "2")]


# Information requests with no question word (docs/specs/2026-11-27-fast-path-request-check.md)
INFO_REQUESTS = [
    "คะน้าหมูชิ้นพิเศษ 1 กล่อง กับน้ำเปล่า 1 ขวด รับเองนะคะ ขอที่อยู่ร้านหน่อยค่ะ เพิ่งเคยสั่งครั้งแรก",
    "กะเพราไก่ 1 รับเองค่ะ ขอเบอร์ร้านด้วยครับ",
    "ขอโลเคชั่นร้านหน่อยค่ะ ข้าวผัดกุ้ง 1 รับเอง",
    "บอกทางไปร้านหน่อยค่ะ ผัดไทย 1 รับเอง",
    "ขอเลขบัญชีด้วยค่ะ กะเพรา 1 รับเอง",
    "ขอคิวอาร์ด้วย ข้าวผัด 1 รับเอง",
    "แจ้งเวลาปิดร้านด้วยนะคะ สุกี้น้ำ 1 รับเอง",
]
PLAIN_ORDERS = [
    "ขอกะเพราหมูกรอบ 1 กล่อง รับเองค่ะ",
    "ขอเผ็ดน้อยหน่อยนะคะ ข้าวผัด 1 รับเอง",
    "ขอข้าวผัดกุ้ง 2 ส่งที่หอพักต้นฝน ห้อง 12",
    "ส่งตอนเที่ยงนะคะ กะเพรา 1 ส่งคอนโดริมเมฆ",
    "รบกวนแยกน้ำด้วยค่ะ สุกี้น้ำ 1 รับเอง",
]


@pytest.mark.parametrize("text", INFO_REQUESTS)
def test_an_information_request_keeps_the_llm_reply(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, text: str
) -> None:
    caplog.set_level(logging.INFO)
    h = Harness(tmp_path)
    h.llm.responses += [_resp(tool_calls=BUILD_PICKUP_CARD), _resp(content="ร้านอยู่ซอยตามสั่ง 7 ค่ะ")]
    h.text("U_A", text)
    assert len(h.llm.calls) == 2
    assert h.line.replies[-1][1][0] == {"type": "text", "text": "ร้านอยู่ซอยตามสั่ง 7 ค่ะ"}
    assert _card_paths(caplog) == [("qword_diff", "2")]


@pytest.mark.parametrize("text", PLAIN_ORDERS)
def test_a_plain_order_with_request_verbs_keeps_the_fast_path(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, text: str
) -> None:
    caplog.set_level(logging.INFO)
    h = Harness(tmp_path)
    h.llm.responses.append(_resp(tool_calls=BUILD_PICKUP_CARD))
    h.text("U_A", text)
    assert len(h.llm.calls) == 1
    assert h.line.replies[-1][1][0] == {"type": "text", "text": LINE_PICKUP}
    assert _card_paths(caplog) == [("fast", "1")]


def test_fast_path_falls_back_when_the_turn_ran_out_of_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    import app.agent as agent_mod
    from app.agent import LIMIT_REPLY

    monkeypatch.setattr(agent_mod, "TURN_DEADLINE_S", 0.2)
    h = Harness(tmp_path)

    def slow_first_call(**kwargs: Any) -> Any:
        h.llm.calls.append(kwargs)
        time.sleep(0.3)  # the first call returns after the turn's deadline
        return _resp(tool_calls=BUILD_PICKUP_CARD)

    h.llm.chat = SimpleNamespace(completions=SimpleNamespace(create=slow_first_call))
    h.text("U_A", "กะเพราหมูกรอบ 1 รับเองค่ะ")
    assert len(h.llm.calls) == 1
    assert h.line.replies[-1][1] == [{"type": "text", "text": LIMIT_REPLY}]  # deadline path
    assert h.db.get_or_create_conversation("U_A").active_card_id is None


def test_fast_path_falls_back_when_the_card_changed_before_the_reply(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    real_is_handed_over = h.db.is_handed_over

    def card_changes_meanwhile(conv: Any) -> bool:
        if conv.active_card_id:  # only once a card exists: drop it, as a concurrent edit would
            h.db.cancel_active_card("U_A", conv.active_card_id)
        return real_is_handed_over(conv)

    h.db.is_handed_over = card_changes_meanwhile  # type: ignore[method-assign]
    h.llm.responses += [_resp(tool_calls=BUILD_PICKUP_CARD), _resp(content="ข้อความจาก LLM")]
    h.text("U_A", "กะเพราหมูกรอบ 1 รับเองค่ะ")
    assert len(h.llm.calls) == 2  # the LLM writes the reply; no fixed line without the card
    assert h.line.replies[-1][1] == [{"type": "text", "text": "ข้อความจาก LLM"}]


def test_takeover_during_the_fast_path_batch_sends_nothing_and_cancels_the_card(
    tmp_path: Path,
) -> None:
    """CODING_STANDARDS Rule 1: the fast reply is a reply path, so คุยเอง is re-checked."""
    h = Harness(tmp_path)
    real_set_active_card = h.db.set_active_card

    def save_card_then_owner_takes_over(*args: Any, **kwargs: Any) -> None:
        real_set_active_card(*args, **kwargs)
        h.db.open_handover("U_A", "owner_takeover", "#1")  # the owner taps คุยเอง meanwhile

    h.db.set_active_card = save_card_then_owner_takes_over  # type: ignore[method-assign]
    h.llm.responses.append(_resp(tool_calls=BUILD_PICKUP_CARD))
    h.text("U_A", "กะเพราหมูกรอบ 1 รับเองค่ะ")
    assert len(h.llm.calls) == 1
    assert h.line.replies == []
    conv = h.db.get_or_create_conversation("U_A")
    assert conv.active_card_id is None
    assert conv.messages == [{"role": "user", "content": "กะเพราหมูกรอบ 1 รับเองค่ะ"}]
    assert h.cards("U_A")[0]["reasons"] == ["owner_takeover"]
