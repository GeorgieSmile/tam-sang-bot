"""Behavioral tests for v2 Ticket 2: hand-over inbox, strict bot quiet, voice reply."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app.agent import (
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
    assert sent[1]["content"].endswith("| ยอดค้างชำระ=0 บาท")  # the picker's payment
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
