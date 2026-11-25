"""Behavioral tests for LINE webhook signature, dedupe, media routing, and postbacks."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from app.agent import IMAGE_HANDOVER_REPLY, build_card_snapshot_for_conv
from app.config import Settings
from app.db import ShopDB
from app.main import create_app
from app.menu import load_menu

MENU_PATH = Path(__file__).resolve().parent.parent / "menu.yaml"
BKK = ZoneInfo("Asia/Bangkok")
LINE_SECRET = "test-channel-secret"


class SpyLineClient:
    def __init__(self) -> None:
        self.replies: list[tuple[str, list[dict[str, Any]]]] = []
        self.pushes: list[tuple[str, str]] = []
        self.loadings: list[str] = []
        self.fail_reply_token: str | None = None

    def reply_messages(self, reply_token: str, messages: list[dict[str, Any]]) -> None:
        if self.fail_reply_token and reply_token == self.fail_reply_token:
            raise RuntimeError("expired reply token")
        self.replies.append((reply_token, messages))

    def push_text(self, user_id: str, text: str) -> None:
        self.pushes.append((user_id, text))

    def show_loading(self, user_id: str, seconds: int = 20) -> None:
        self.loadings.append(user_id)

    def get_profile(self, user_id: str) -> str | None:
        return None

    def get_message_content(self, message_id: str) -> bytes:
        return b""


class FailingLLM:
    def __init__(self) -> None:
        self.called = 0

    @property
    def chat(self) -> Any:
        self.called += 1
        raise AssertionError("LLM must not be called in this test")


def _sign(body: bytes, secret: str = LINE_SECRET) -> str:
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode("ascii")


def _post_webhook(client: TestClient, payload: dict[str, Any], *, sig: str | None = None) -> Any:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    header_sig = _sign(raw) if sig is None else sig
    return client.post(
        "/line/webhook",
        content=raw,
        headers={"Content-Type": "application/json", "x-line-signature": header_sig},
    )


def _setup(tmp_path: Path) -> tuple[TestClient, ShopDB, SpyLineClient, FailingLLM]:
    menu = load_menu(MENU_PATH)
    now = datetime(2026, 11, 23, 12, 0, tzinfo=BKK)
    db = ShopDB(tmp_path / "shop.db", clock=lambda: now)
    spy = SpyLineClient()
    llm = FailingLLM()
    settings = Settings(
        db_path=tmp_path / "shop.db",
        menu_path=MENU_PATH,
        kitchen_secret="secret-123",
        public_base_url="http://localhost:8000",
        payment_info_text="พร้อมเพย์ 081-234-5678 นายสมชาย",
        line_channel_secret=LINE_SECRET,
        line_channel_access_token="token",
        owner_line_user_id="U_OWNER",
        llm_base_url="https://openrouter.ai/api/v1",
        llm_api_key="test-key",
        llm_model="anthropic/claude-haiku-5.5",
        llm_reasoning_effort="low",
    )
    app = create_app(settings=settings, db=db, menu=menu, line_client=spy, llm_client=llm)
    return TestClient(app), db, spy, llm


def test_webhook_rejects_bad_signature_and_deduplicates_events(tmp_path: Path) -> None:
    client, _db, spy, _llm = _setup(tmp_path)

    # Empty events (LINE console Verify button) with valid signature -> 200
    assert _post_webhook(client, {"events": []}).status_code == 200

    # Invalid signature with a real event -> 400 and handler must NOT run
    loc_event = {
        "webhookEventId": "evt-dup-loc",
        "type": "message",
        "replyToken": "rt-1",
        "source": {"type": "user", "userId": "U_CUST"},
        "message": {
            "type": "location",
            "address": "บ้าน 11",
            "latitude": 13.7,
            "longitude": 100.5,
        },
    }
    bad = _post_webhook(client, {"events": [loc_event]}, sig="wrong-sig")
    assert bad.status_code == 400
    assert spy.replies == []

    # Duplicate webhookEventId on a repeatable location event -> processed once
    assert _post_webhook(client, {"events": [loc_event]}).status_code == 200
    assert _post_webhook(client, {"events": [loc_event]}).status_code == 200
    assert len(spy.replies) == 1
    assert "บ้าน 11" in spy.replies[0][1][0]["text"]


def test_webhook_isolates_event_failures_and_respects_bot_pause_on_media(tmp_path: Path) -> None:
    client, db, spy, _llm = _setup(tmp_path)
    menu = load_menu(MENU_PATH)

    # bot_enabled = 0 silences image and location too
    db.set_bot_enabled(False)
    for idx, msg in enumerate(
        [
            {"type": "image", "id": "i-paused"},
            {"type": "location", "address": "บ้าน 1", "latitude": 13.7, "longitude": 100.5},
        ],
        start=1,
    ):
        assert (
            _post_webhook(
                client,
                {
                    "events": [
                        {
                            "webhookEventId": f"evt-paused-{idx}",
                            "type": "message",
                            "replyToken": f"rt-p-{idx}",
                            "source": {"type": "user", "userId": "U_PAUSED"},
                            "message": msg,
                        }
                    ]
                },
            ).status_code
            == 200
        )
    assert spy.replies == []
    db.set_bot_enabled(True)

    # Event 1 fails reply_messages on confirm, yet owner push STILL fires AND Event 2 succeeds
    db.set_notify_owner(True)
    conv = db.get_or_create_conversation("U_FAIL_REPLY")
    db.save_draft(
        conv.id,
        {"items": [{"dish_id": "water", "qty": 1, "option_ids": []}], "fulfillment": "pickup"},
    )
    conv = db.get_or_create_conversation("U_FAIL_REPLY")
    snap, _ = build_card_snapshot_for_conv(db, conv, menu)
    assert snap is not None

    spy.fail_reply_token = "rt-fail-1"
    res = _post_webhook(
        client,
        {
            "events": [
                {
                    "webhookEventId": "evt-batch-1",
                    "type": "postback",
                    "replyToken": "rt-fail-1",
                    "source": {"type": "user", "userId": "U_FAIL_REPLY"},
                    "postback": {"data": f"action=confirm&card_id={snap['card_id']}"},
                },
                {
                    "webhookEventId": "evt-batch-2",
                    "type": "message",
                    "replyToken": "rt-ok-2",
                    "source": {"type": "user", "userId": "U_OTHER"},
                    "message": {
                        "type": "location",
                        "address": "บ้าน 22",
                        "latitude": 13.7,
                        "longitude": 100.5,
                    },
                },
            ]
        },
    )
    assert res.status_code == 200
    assert len(spy.pushes) == 1
    assert spy.pushes[0][0] == "U_OWNER"
    assert len(spy.replies) == 1
    assert spy.replies[0][0] == "rt-ok-2"


def test_media_routing_image_sticker_and_location_pin(tmp_path: Path) -> None:
    client, db, spy, llm = _setup(tmp_path)

    # Sticker, video -> ignored with 0 replies and 0 LLM calls.
    # Audio now gets the voice reply (SPEC v2 §3.8 item 5).
    for idx, mtype in enumerate(("sticker", "audio", "video"), start=1):
        res = _post_webhook(
            client,
            {
                "events": [
                    {
                        "webhookEventId": f"evt-media-{idx}",
                        "type": "message",
                        "replyToken": f"rt-m-{idx}",
                        "source": {"type": "user", "userId": "U_CUST"},
                        "message": {"type": mtype, "id": f"m-{idx}"},
                    }
                ]
            },
        )
        assert res.status_code == 200
    assert spy.replies == [("rt-m-2", [{"type": "text", "text": "รบกวนพิมพ์เป็นข้อความนะคะ 🙏"}])]
    spy.replies.clear()
    assert llm.called == 0

    # Image -> replies once + sets handed_over; second image stays silent
    for idx in (1, 2):
        _post_webhook(
            client,
            {
                "events": [
                    {
                        "webhookEventId": f"evt-img-{idx}",
                        "type": "message",
                        "replyToken": f"rt-img-{idx}",
                        "source": {"type": "user", "userId": "U_IMG"},
                        "message": {"type": "image", "id": f"i-{idx}"},
                    }
                ]
            },
        )
    assert len(spy.replies) == 1
    assert spy.replies[0][1][0]["text"] == IMAGE_HANDOVER_REPLY
    assert llm.called == 0

    # Location pin with existing cart -> saves address + Maps URL and replies with Flex card
    conv = db.get_or_create_conversation("U_LOC")
    db.save_draft(
        conv.id,
        {"items": [{"dish_id": "kaphrao", "qty": 1, "option_ids": ["crispy_pork"]}]},
    )
    res_loc = _post_webhook(
        client,
        {
            "events": [
                {
                    "webhookEventId": "evt-loc-1",
                    "type": "message",
                    "replyToken": "rt-loc-1",
                    "source": {"type": "user", "userId": "U_LOC"},
                    "message": {
                        "type": "location",
                        "address": "XGV6+R86 45/2 หมู่ 3",
                        "latitude": 13.7563,
                        "longitude": 100.5018,
                    },
                }
            ]
        },
    )
    assert res_loc.status_code == 200
    assert len(spy.replies) == 2
    loc_msgs = spy.replies[1][1]
    assert loc_msgs[0]["type"] == "text"
    assert loc_msgs[1]["type"] == "flex"
    updated_conv = db.get_or_create_conversation("U_LOC")
    assert updated_conv.draft["address"] == "45/2 หมู่ 3"
    assert updated_conv.draft["location_url"] == "https://maps.google.com/?q=13.7563,100.5018"
    assert any("45/2 หมู่ 3" in str(m.get("content", "")) for m in updated_conv.messages)


def test_confirm_and_cancel_postbacks_and_owner_alert_toggle(tmp_path: Path) -> None:
    client, db, spy, _llm = _setup(tmp_path)
    menu = load_menu(MENU_PATH)

    # Stage an active card for U_BUYER
    conv = db.get_or_create_conversation("U_BUYER")
    db.save_draft(
        conv.id,
        {
            "items": [{"dish_id": "kaphrao", "qty": 1, "option_ids": ["crispy_pork"]}],
            "fulfillment": "delivery",
            "address": "บ้าน 99",
            "location_url": "https://maps.google.com/?q=13.7,100.5",
        },
    )
    conv = db.get_or_create_conversation("U_BUYER")
    snapshot, err = build_card_snapshot_for_conv(db, conv, menu)
    assert err is None and snapshot is not None
    card_id = snapshot["card_id"]

    # Seed stale pre-confirm chat history; confirm must compact it and record Order #1 receipt
    db.save_conversation_messages(
        conv.id,
        [
            {"role": "user", "content": "กะเพราหมูกรอบ 1"},
            {"role": "assistant", "content": "เพิ่มแล้วค่ะ กดยืนยันบนการ์ด"},
        ],
    )
    # Mark conversation handed over before tapping Confirm -> postback must clear hand-over
    db.set_handed_over(conv.id)
    assert db.is_handed_over(db.get_or_create_conversation("U_BUYER")) is True

    # Confirm postback (notify_owner_on_order=0 -> 0 pushes, receipt via reply with payment info)
    confirm_evt = {
        "webhookEventId": "evt-confirm-1",
        "type": "postback",
        "replyToken": "rt-confirm-1",
        "source": {"type": "user", "userId": "U_BUYER"},
        "postback": {"data": f"action=confirm&card_id={card_id}"},
    }
    assert _post_webhook(client, {"events": [confirm_evt]}).status_code == 200
    buyer_conv_after = db.get_or_create_conversation("U_BUYER")
    assert db.is_handed_over(buyer_conv_after) is False
    # Old cart-building chat turns compacted away; confirmed receipt recorded for next turn
    assert len(buyer_conv_after.messages) == 2
    assert "ยืนยันออเดอร์ #1" in buyer_conv_after.messages[0]["content"]
    assert "รับออเดอร์ #1 แล้วค่ะ" in buyer_conv_after.messages[1]["content"]
    assert len(spy.replies) == 1
    receipt_text = spy.replies[0][1][0]["text"]
    assert "#1" in receipt_text
    assert "70 บาท" in receipt_text
    assert "พร้อมเพย์ 081-234-5678" in receipt_text
    assert spy.pushes == []

    # Returning address saved
    cust = db.get_customer("U_BUYER")
    assert cust is not None
    assert cust.last_address == "บ้าน 99"
    assert cust.last_location_url == "https://maps.google.com/?q=13.7,100.5"

    # Double-tap confirm with new event id -> idempotent, still 0 pushes
    confirm_evt_2 = {**confirm_evt, "webhookEventId": "evt-confirm-2", "replyToken": "rt-c2"}
    assert _post_webhook(client, {"events": [confirm_evt_2]}).status_code == 200
    assert "ยืนยันไว้แล้ว" in spy.replies[1][1][0]["text"]
    assert db.get_kitchen_orders_today()["stats"]["queued_count"] == 1
    assert spy.pushes == []

    # Turn ON owner order alerts -> next confirmed order pushes 1 alert to U_OWNER
    db.set_notify_owner(True)
    conv2 = db.get_or_create_conversation("U_BUYER_2")
    db.save_draft(
        conv2.id,
        {
            "items": [{"dish_id": "thai_tea", "qty": 1, "option_ids": []}],
            "fulfillment": "pickup",
        },
    )
    conv2 = db.get_or_create_conversation("U_BUYER_2")
    snap2, _ = build_card_snapshot_for_conv(db, conv2, menu)
    assert snap2 is not None
    _post_webhook(
        client,
        {
            "events": [
                {
                    "webhookEventId": "evt-confirm-3",
                    "type": "postback",
                    "replyToken": "rt-c3",
                    "source": {"type": "user", "userId": "U_BUYER_2"},
                    "postback": {"data": f"action=confirm&card_id={snap2['card_id']}"},
                }
            ]
        },
    )
    assert len(spy.pushes) == 1
    assert spy.pushes[0][0] == "U_OWNER"
    assert "#2" in spy.pushes[0][1]

    # Cancel postback clears active card
    conv3 = db.get_or_create_conversation("U_CANCEL")
    db.save_draft(
        conv3.id,
        {"items": [{"dish_id": "water", "qty": 1, "option_ids": []}], "fulfillment": "pickup"},
    )
    conv3 = db.get_or_create_conversation("U_CANCEL")
    snap3, _ = build_card_snapshot_for_conv(db, conv3, menu)
    assert snap3 is not None
    _post_webhook(
        client,
        {
            "events": [
                {
                    "webhookEventId": "evt-cancel-card",
                    "type": "postback",
                    "replyToken": "rt-cancel",
                    "source": {"type": "user", "userId": "U_CANCEL"},
                    "postback": {"data": f"action=cancel&card_id={snap3['card_id']}"},
                }
            ]
        },
    )
    assert db.get_or_create_conversation("U_CANCEL").active_card_id is None
