"""Behavioral tests for owner LINE Thai commands and webhook routing."""

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

from app.agent import build_card_snapshot_for_conv
from app.config import Settings
from app.db import ShopDB
from app.main import create_app
from app.menu import load_menu
from app.owner import handle_owner_text

MENU_PATH = Path(__file__).resolve().parent.parent / "menu.yaml"
BKK = ZoneInfo("Asia/Bangkok")


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


def _settings(db_path: Path) -> Settings:
    return Settings(
        db_path=db_path,
        menu_path=MENU_PATH,
        kitchen_secret="secret-123",
        public_base_url="https://shop.example",
        payment_info_text="",
        line_channel_secret="owner-secret",
        line_channel_access_token="tok",
        owner_line_user_id="U_OWNER",
        llm_base_url="https://openrouter.ai/api/v1",
        llm_api_key="key",
        llm_model="anthropic/claude-haiku-5.5",
        llm_reasoning_effort="low",
    )


def test_owner_commands_update_state_and_report_summary(tmp_path: Path) -> None:
    menu = load_menu(MENU_PATH)
    now = datetime(2026, 11, 23, 12, 0, tzinfo=BKK)
    db = ShopDB(tmp_path / "shop.db", clock=lambda: now)
    cfg = _settings(tmp_path / "shop.db")

    # Sold-out & restock
    r1 = handle_owner_text(db, menu, cfg, "หมูกรอบหมด")
    assert "หมูกรอบ" in r1
    assert db.get_overrides()[("ingredient", "crispy_pork")] is False

    r2 = handle_owner_text(db, menu, cfg, "หมูกรอบมาแล้ว")
    assert "หมูกรอบ" in r2
    assert db.get_overrides()[("ingredient", "crispy_pork")] is True

    handle_owner_text(db, menu, cfg, "ข้าวไข่เจียวหมด")
    assert db.get_overrides()[("dish", "omelette_rice")] is False
    handle_owner_text(db, menu, cfg, "เปิดครบ")
    assert db.get_overrides() == {}

    # Busy mode with explicit minutes, default minutes, and normal mode
    handle_owner_text(db, menu, cfg, "ยุ่ง 25 นาที")
    st = db.get_shop_status(menu)
    assert st.busy_mode is True and st.wait_minutes == 25

    handle_owner_text(db, menu, cfg, "ยุ่ง")
    st = db.get_shop_status(menu)
    assert st.busy_mode is True and st.wait_minutes == 20

    handle_owner_text(db, menu, cfg, "โหมดปกติ")
    st = db.get_shop_status(menu)
    assert st.busy_mode is False and st.wait_minutes is None

    # Open / close override
    handle_owner_text(db, menu, cfg, "ปิดร้าน")
    assert db.get_shop_status(menu).is_open is False
    handle_owner_text(db, menu, cfg, "เปิดร้าน")
    assert db.get_shop_status(menu).is_open is True

    # Bot pause / resume
    handle_owner_text(db, menu, cfg, "หยุดบอท")
    assert db.get_shop_status(menu).bot_enabled is False
    handle_owner_text(db, menu, cfg, "เปิดบอท")
    assert db.get_shop_status(menu).bot_enabled is True

    # Owner order alert toggle
    handle_owner_text(db, menu, cfg, "เปิดแจ้งเตือน")
    assert db.get_shop_status(menu).notify_owner_on_order is True
    handle_owner_text(db, menu, cfg, "ปิดแจ้งเตือน")
    assert db.get_shop_status(menu).notify_owner_on_order is False

    # Queue check & daily summary with real orders
    for uid, dish, status_action in [
        ("U1", "omelette_rice", "done"),
        ("U2", "chicken_bites", "cancel"),
        ("U3", "water", None),
    ]:
        conv = db.get_or_create_conversation(uid)
        db.save_draft(
            conv.id,
            {"items": [{"dish_id": dish, "qty": 1, "option_ids": []}], "fulfillment": "pickup"},
        )
        conv = db.get_or_create_conversation(uid)
        snap, _ = build_card_snapshot_for_conv(db, conv, menu)
        assert snap is not None
        res = db.confirm_card(uid, snap["card_id"], menu)
        assert res.ok and res.order is not None
        if status_action == "done":
            db.mark_order_done(res.order.id)
        elif status_action == "cancel":
            db.cancel_order_by_owner(res.order.id, "ของหมด")

    q_reply = handle_owner_text(db, menu, cfg, "คิว")
    assert "1 คิว" in q_reply
    assert "https://shop.example/kitchen?key=secret-123" in q_reply

    sum_reply = handle_owner_text(db, menu, cfg, "สรุปวันนี้")
    assert "ทำเสร็จแล้ว: 1 ออเดอร์" in sum_reply
    assert "ยอดขายรวม: 40 บาท" in sum_reply
    assert "ยกเลิก: 1 ออเดอร์" in sum_reply
    assert "รอทำอยู่: 1 ออเดอร์" in sum_reply


def test_owner_webhook_routes_with_zero_pushes_even_when_bot_disabled(tmp_path: Path) -> None:
    menu = load_menu(MENU_PATH)
    now = datetime(2026, 11, 23, 12, 0, tzinfo=BKK)
    db = ShopDB(tmp_path / "shop.db", clock=lambda: now)
    db.set_bot_enabled(False)
    spy = SpyLineClient()
    cfg = _settings(tmp_path / "shop.db")
    app = create_app(settings=cfg, db=db, menu=menu, line_client=spy)
    client = TestClient(app)

    payload = {
        "events": [
            {
                "webhookEventId": "evt-owner-1",
                "type": "message",
                "replyToken": "rt-owner-1",
                "source": {"type": "user", "userId": "U_OWNER"},
                "message": {"type": "text", "text": "เปิดบอท"},
            }
        ]
    }
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    sig = base64.b64encode(
        hmac.new(cfg.line_channel_secret.encode("utf-8"), raw, hashlib.sha256).digest()
    ).decode("ascii")
    resp = client.post(
        "/line/webhook",
        content=raw,
        headers={"Content-Type": "application/json", "x-line-signature": sig},
    )
    assert resp.status_code == 200
    assert db.get_shop_status(menu).bot_enabled is True
    assert len(spy.replies) == 1
    assert spy.pushes == []
