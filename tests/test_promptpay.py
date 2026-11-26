"""Behavioral tests for v2 Ticket 3: PromptPay payload, QR image, receipt, get_payment_info."""

from __future__ import annotations

import base64
import binascii
import dataclasses
import hashlib
import hmac
import io
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import zxingcpp
from fastapi.testclient import TestClient
from PIL import Image

from app.agent import build_card_snapshot_for_conv
from app.config import Settings
from app.db import Order, ShopDB
from app.line import build_order_flex
from app.main import create_app
from app.menu import load_menu
from app.promptpay import build_payload, crc16_ccitt_false

MENU_PATH = Path(__file__).resolve().parent.parent / "menu.yaml"
BKK = ZoneInfo("Asia/Bangkok")
SECRET = "test-channel-secret"

# Published by an established generator (kittinan/php-promptpay-qr README):
PUBLISHED_NO_AMOUNT = "00020101021129370016A000000677010111011300668999999995802TH53037646304FE29"
PUBLISHED_420 = (
    "00020101021229370016A000000677010111011300668999999995802TH53037645406420.006304CF9E"
)
# Fixed value for PROMPTPAY_ID=0812345678, 130 baht (same field order and CRC as above).
FIXED_0812345678_130 = (
    "00020101021229370016A000000677010111011300668123456785802TH53037645406130.00630496CE"
)

CART_60 = [{"dish_id": "kaphrao", "qty": 1, "option_ids": ["crispy_pork"]}]
CART_70 = [{"dish_id": "kaphrao", "qty": 1, "option_ids": ["crispy_pork", "fried_egg_runny"]}]


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


def _settings(db_path: Path, promptpay_id: str) -> Settings:
    return Settings(
        db_path=db_path,
        menu_path=MENU_PATH,
        kitchen_secret="secret-123",
        public_base_url="https://shop.example",
        payment_info_text="พร้อมเพย์ 081-234-5678",
        line_channel_secret=SECRET,
        line_channel_access_token="token",
        owner_line_user_id="U_OWNER",
        llm_base_url="https://openrouter.ai/api/v1",
        llm_api_key="test-key",
        llm_model="anthropic/claude-haiku-5.5",
        llm_reasoning_effort="low",
        promptpay_id=promptpay_id,
    )


def _post(client: TestClient, event: dict[str, Any]) -> None:
    raw = json.dumps({"events": [event]}, ensure_ascii=False).encode("utf-8")
    sig = base64.b64encode(hmac.new(SECRET.encode(), raw, hashlib.sha256).digest()).decode()
    res = client.post(
        "/line/webhook",
        content=raw,
        headers={"Content-Type": "application/json", "x-line-signature": sig},
    )
    assert res.status_code == 200


def _stage(db: ShopDB, user_id: str, items: list[Any]) -> str:
    conv = db.get_or_create_conversation(user_id)
    db.save_draft(conv.id, {"items": items, "fulfillment": "pickup"})
    snap, err = build_card_snapshot_for_conv(
        db, db.get_or_create_conversation(user_id), load_menu(MENU_PATH)
    )
    assert snap is not None, err
    return str(snap["card_id"])


def _place(db: ShopDB, user_id: str, items: list[Any]) -> Order:
    card_id = _stage(db, user_id, items)
    res = db.confirm_card(user_id, card_id, load_menu(MENU_PATH))
    assert res.ok and res.order is not None
    return res.order


def _confirm_via_webhook(client: TestClient, user_id: str, card_id: str) -> None:
    _post(
        client,
        {
            "webhookEventId": f"evt-{card_id}",
            "type": "postback",
            "replyToken": f"rt-{card_id}",
            "source": {"type": "user", "userId": user_id},
            "postback": {"data": f"action=confirm&card_id={card_id}"},
        },
    )


def test_payload_matches_published_and_fixed_values() -> None:
    assert build_payload("0899999999") == PUBLISHED_NO_AMOUNT
    assert build_payload("089-999-9999", 420) == PUBLISHED_420
    assert build_payload("0812345678", 130) == FIXED_0812345678_130

    # Independent CRC-16/CCITT-FALSE reference (binascii.crc_hqx, init 0xFFFF)
    for payload in (PUBLISHED_420, FIXED_0812345678_130):
        assert payload[-4:] == f"{binascii.crc_hqx(payload[:-4].encode(), 0xFFFF):04X}"

    national = build_payload("1-2345-67890-12-3", 130)
    assert national.startswith(
        "00020101021229370016A000000677010111" + "0213" + "1234567890123" + "5802TH"
    )
    assert "5406130.00" in national
    assert national[-4:] == crc16_ccitt_false(national[:-4])
    assert national[-4:] == f"{binascii.crc_hqx(national[:-4].encode(), 0xFFFF):04X}"


def test_confirm_reply_has_balance_lines_and_scannable_qr(tmp_path: Path) -> None:
    menu = load_menu(MENU_PATH)
    clock = Clock(datetime(2026, 11, 23, 12, 0, tzinfo=BKK))
    db = ShopDB(tmp_path / "shop.db", clock=clock)
    spy = SpyLineClient()
    cfg = _settings(db.db_path, "0812345678")
    client = TestClient(create_app(settings=cfg, db=db, menu=menu, line_client=spy))

    for i in range(1, 5):
        _place(db, f"U_F{i}", CART_60)
    o5 = _place(db, "U_A", CART_70)  # yesterday's #5, 70฿, unpaid
    assert o5.queue_number == 5

    clock.now = datetime(2026, 11, 24, 12, 0, tzinfo=BKK)
    _place(db, "U_F9", CART_60)
    card_id = _stage(db, "U_A", CART_60)
    _confirm_via_webhook(client, "U_A", card_id)

    assert len(spy.replies) == 1
    text_msg, img_msg = spy.replies[0][1]
    assert "รับออเดอร์ #2 แล้วค่ะ" in text_msg["text"]
    assert "ออเดอร์นี้ #2 60฿\nค้างจากเมื่อวาน #5 70฿\nรวมที่ต้องโอน 130฿" in text_msg["text"]
    assert "พร้อมเพย์ 081-234-5678" not in text_msg["text"]
    assert img_msg["type"] == "image"
    url = img_msg["originalContentUrl"]
    assert img_msg["previewImageUrl"] == url
    assert url.startswith("https://shop.example/qr/")
    assert "U_A" not in url

    res = client.get(url.removeprefix("https://shop.example"))
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/png"
    decoded = zxingcpp.read_barcodes(Image.open(io.BytesIO(res.content)))
    assert [d.text for d in decoded] == [FIXED_0812345678_130]

    # A tampered path is not served
    bad = url.removeprefix("https://shop.example").replace("/qr/13000-", "/qr/1300-")
    assert client.get(bad).status_code == 404


def test_without_promptpay_id_confirm_reply_is_the_v1_receipt(tmp_path: Path) -> None:
    menu = load_menu(MENU_PATH)
    db = ShopDB(tmp_path / "shop.db", clock=Clock(datetime(2026, 11, 23, 12, 0, tzinfo=BKK)))
    spy = SpyLineClient()
    cfg = _settings(db.db_path, "")
    client = TestClient(create_app(settings=cfg, db=db, menu=menu, line_client=spy))

    card_id = _stage(db, "U_A", CART_60)
    _confirm_via_webhook(client, "U_A", card_id)
    # Frozen output of the shipped v1 formatter (git a516222:app/line.py), plus the queue
    # position line from SPEC §3.3 (Ticket 5)
    v1_receipt = (
        "รับออเดอร์ #1 แล้วค่ะ\n• ข้าวกะเพรา [หมูกรอบ, เผ็ดปกติ] ×1 = 60฿\nรับเองที่ร้าน\n"
        "ยอดรวมทั้งหมด 60 บาท\nคิวที่ 1 · ประมาณ 10–15 นาที\n\nพร้อมเพย์ 081-234-5678"
    )
    assert spy.replies[0][1] == [{"type": "text", "text": v1_receipt}]
    assert client.get("/qr/6000-abc.png").status_code == 404


def test_get_payment_info_with_zero_balance_sends_no_qr(tmp_path: Path) -> None:
    menu = load_menu(MENU_PATH)
    db = ShopDB(tmp_path / "shop.db", clock=Clock(datetime(2026, 11, 23, 12, 0, tzinfo=BKK)))
    spy = SpyLineClient()
    cfg = _settings(db.db_path, "0812345678")
    call = SimpleNamespace(
        id="p1", function=SimpleNamespace(name="get_payment_info", arguments="{}")
    )
    llm = StubLLM([_resp(tool_calls=[call]), _resp(content="ไม่มียอดค้างค่ะ")])
    client = TestClient(create_app(settings=cfg, db=db, menu=menu, line_client=spy, llm_client=llm))

    _post(
        client,
        {
            "webhookEventId": "evt-qr",
            "type": "message",
            "replyToken": "rt-qr",
            "source": {"type": "user", "userId": "U_A"},
            "message": {"id": "m1", "type": "text", "text": "ขอ QR"},
        },
    )
    tool_msg = llm.calls[1]["messages"][-1]
    assert tool_msg["role"] == "tool"
    assert json.loads(tool_msg["content"])["unpaid_balance"] == 0
    assert spy.replies == [("rt-qr", [{"type": "text", "text": "ไม่มียอดค้างค่ะ"}])]

    # With a balance, the same request sends the QR image after the text
    _place(db, "U_A", CART_70)
    llm.responses = [_resp(tool_calls=[call]), _resp(content="โอนตาม QR ได้เลยค่ะ")]
    _post(
        client,
        {
            "webhookEventId": "evt-qr2",
            "type": "message",
            "replyToken": "rt-qr2",
            "source": {"type": "user", "userId": "U_A"},
            "message": {"id": "m2", "type": "text", "text": "ค้างเท่าไหร่"},
        },
    )
    assert json.loads(llm.calls[3]["messages"][-1]["content"])["unpaid_balance"] == 70
    msgs = spy.replies[-1][1]
    assert [m["type"] for m in msgs] == ["text", "image"]
    assert msgs[1]["originalContentUrl"].startswith("https://shop.example/qr/7000-")


def test_order_card_flex_never_contains_the_qr(tmp_path: Path) -> None:
    db = ShopDB(tmp_path / "shop.db", clock=Clock(datetime(2026, 11, 23, 12, 0, tzinfo=BKK)))
    menu = load_menu(MENU_PATH)
    _place(db, "U_A", CART_70)  # unpaid balance exists
    _stage(db, "U_A", CART_60)
    snap = db.get_or_create_conversation("U_A").active_card
    assert snap is not None
    flex = json.dumps(build_order_flex(snap, shop_name=menu.shop.name), ensure_ascii=False)
    assert "/qr/" not in flex and '"type": "image"' not in flex


def test_qr_endpoint_rejects_malformed_names_without_errors(tmp_path: Path) -> None:
    db = ShopDB(tmp_path / "shop.db", clock=Clock(datetime(2026, 11, 23, 12, 0, tzinfo=BKK)))
    cfg = _settings(db.db_path, "0812345678")
    client = TestClient(create_app(settings=cfg, db=db, menu=load_menu(MENU_PATH)))
    for name in ("๑๓๐๐๐-" + "a" * 24, "13000-" + "é" * 24, "0-" + "a" * 24, "x", "1-2-3"):
        assert client.get(f"/qr/{name}.png").status_code == 404


def test_promptpay_settings_are_validated_at_startup(tmp_path: Path) -> None:
    db = ShopDB(tmp_path / "shop.db")
    menu = load_menu(MENU_PATH)
    good = _settings(db.db_path, "0812345678")
    for bad in (
        dataclasses.replace(good, public_base_url="http://localhost:8000"),
        dataclasses.replace(good, kitchen_secret=""),
        dataclasses.replace(good, promptpay_id="12345"),
        dataclasses.replace(good, promptpay_id="๐๘๑๒๓๔๕๖๗๘"),
        dataclasses.replace(good, public_base_url="https://"),
        dataclasses.replace(good, public_base_url="https://shop.example?x=1"),
        dataclasses.replace(good, public_base_url="https://shop.example#frag"),
        dataclasses.replace(good, promptpay_id="08123x45678"),
    ):
        with pytest.raises(ValueError):
            create_app(settings=bad, db=db, menu=menu)
    create_app(settings=dataclasses.replace(good, promptpay_id=""), db=db, menu=menu)


def test_payment_lookup_never_sends_a_stale_qr_and_live_state_has_balance(tmp_path: Path) -> None:
    menu = load_menu(MENU_PATH)
    db = ShopDB(tmp_path / "shop.db", clock=Clock(datetime(2026, 11, 23, 12, 0, tzinfo=BKK)))
    spy = SpyLineClient()
    cfg = _settings(db.db_path, "0812345678")
    order = _place(db, "U_A", CART_70)

    def call(name: str, args: dict[str, Any], cid: str) -> Any:
        return SimpleNamespace(
            id=cid, function=SimpleNamespace(name=name, arguments=json.dumps(args))
        )

    llm = StubLLM(
        [
            _resp(tool_calls=[call("get_payment_info", {}, "a")]),
            _resp(tool_calls=[call("cancel_order", {"order_id": order.id}, "b")]),
            _resp(tool_calls=[call("get_payment_info", {}, "c")]),
            _resp(content="ยกเลิกแล้ว ไม่มียอดค้างค่ะ"),
        ]
    )
    client = TestClient(create_app(settings=cfg, db=db, menu=menu, line_client=spy, llm_client=llm))
    _post(
        client,
        {
            "webhookEventId": "evt-1",
            "type": "message",
            "replyToken": "rt-1",
            "source": {"type": "user", "userId": "U_A"},
            "message": {"id": "m1", "type": "text", "text": "ยกเลิกแล้วขอ QR"},
        },
    )
    assert spy.replies[-1][1] == [{"type": "text", "text": "ยกเลิกแล้ว ไม่มียอดค้างค่ะ"}]
    assert "| ยอดค้างชำระ=70 บาท" in llm.calls[0]["messages"][1]["content"]

    # An owner payment change shows up in the next call's live state
    order2 = _place(db, "U_A", CART_60)
    db.set_payment_status(order2.id, "cash")
    llm.responses = [_resp(content="ค่ะ")]
    _post(
        client,
        {
            "webhookEventId": "evt-2",
            "type": "message",
            "replyToken": "rt-2",
            "source": {"type": "user", "userId": "U_A"},
            "message": {"id": "m2", "type": "text", "text": "จ่ายแล้วนะ"},
        },
    )
    assert llm.calls[-1]["messages"][1]["content"].endswith("| ยอดค้างชำระ=0 บาท | ที่อยู่เดิม=ไม่มี")


def test_qr_is_dropped_when_a_later_tool_clears_the_balance(tmp_path: Path) -> None:
    menu = load_menu(MENU_PATH)
    db = ShopDB(tmp_path / "shop.db", clock=Clock(datetime(2026, 11, 23, 12, 0, tzinfo=BKK)))
    spy = SpyLineClient()
    cfg = _settings(db.db_path, "0812345678")
    order = _place(db, "U_A", CART_70)

    def call(name: str, args: dict[str, Any], cid: str) -> Any:
        return SimpleNamespace(
            id=cid, function=SimpleNamespace(name=name, arguments=json.dumps(args))
        )

    llm = StubLLM(
        [
            _resp(tool_calls=[call("get_payment_info", {}, "a")]),
            _resp(tool_calls=[call("cancel_order", {"order_id": order.id}, "b")]),
            _resp(content="ยกเลิกให้แล้วค่ะ"),
        ]
    )
    client = TestClient(create_app(settings=cfg, db=db, menu=menu, line_client=spy, llm_client=llm))
    _post(
        client,
        {
            "webhookEventId": "evt-1",
            "type": "message",
            "replyToken": "rt-1",
            "source": {"type": "user", "userId": "U_A"},
            "message": {"id": "m1", "type": "text", "text": "ขอ QR แล้วยกเลิก"},
        },
    )
    assert spy.replies[-1][1] == [{"type": "text", "text": "ยกเลิกให้แล้วค่ะ"}]


def _call(call_id: str, name: str, args: dict[str, Any]) -> Any:
    return SimpleNamespace(
        id=call_id, function=SimpleNamespace(name=name, arguments=json.dumps(args))
    )


def _say(client: TestClient, user_id: str, text: str, n: int) -> None:
    _post(
        client,
        {
            "webhookEventId": f"evt-say-{n}",
            "type": "message",
            "replyToken": f"rt-say-{n}",
            "source": {"type": "user", "userId": user_id},
            "message": {"id": f"say-{n}", "type": "text", "text": text},
        },
    )


def test_receipt_is_one_line_when_only_this_order_is_owed(tmp_path: Path) -> None:
    """Ticket 7: no total repeated three times when nothing older is owed."""
    db = ShopDB(tmp_path / "shop.db", clock=Clock(datetime(2026, 11, 23, 12, 0, tzinfo=BKK)))
    spy = SpyLineClient()
    cfg = _settings(db.db_path, "0812345678")
    client = TestClient(create_app(settings=cfg, db=db, menu=load_menu(MENU_PATH), line_client=spy))
    _confirm_via_webhook(client, "U_A", _stage(db, "U_A", CART_60))
    text_msg, img_msg = spy.replies[0][1]
    assert text_msg["text"].endswith(
        "ยอดรวมทั้งหมด 60 บาท\nคิวที่ 1 · ประมาณ 10–15 นาที\n\nโอน 60฿ ตาม QR ด้านล่างได้เลยค่ะ"
    )
    assert "รวมที่ต้องโอน" not in text_msg["text"]
    assert img_msg["originalContentUrl"].startswith("https://shop.example/qr/6000-")


def test_cash_customer_gets_no_qr_until_they_ask_for_one(tmp_path: Path) -> None:
    """Ticket 7: the QR is the default; only a customer who says cash stops getting it."""
    db = ShopDB(tmp_path / "shop.db", clock=Clock(datetime(2026, 11, 23, 12, 0, tzinfo=BKK)))
    spy = SpyLineClient()
    cfg = _settings(db.db_path, "0812345678")
    llm = StubLLM([])
    client = TestClient(
        create_app(settings=cfg, db=db, menu=load_menu(MENU_PATH), line_client=spy, llm_client=llm)
    )
    first = _place(db, "U_A", CART_60)  # #1, placed before the customer mentioned cash
    assert first.pay_cash is False

    # 1. The customer says cash: every unpaid order and today's next order are cash
    llm.responses = [
        _resp(tool_calls=[_call("c1", "set_payment_method", {"method": "cash"})]),
        _resp(content="ได้ค่ะ ชำระเงินสดตอนรับอาหารได้เลยค่ะ"),
    ]
    _say(client, "U_A", "จ่ายเงินสดนะคะ", 1)
    result = json.loads(llm.calls[1]["messages"][-1]["content"])
    assert (result["method"], result["unpaid_balance"]) == ("cash", 60)
    assert db.get_order(first.id).pay_cash is True  # type: ignore[union-attr]
    assert spy.replies[-1][1] == [{"type": "text", "text": "ได้ค่ะ ชำระเงินสดตอนรับอาหารได้เลยค่ะ"}]

    # 2. The next receipt asks for cash and sends no QR image
    _confirm_via_webhook(client, "U_A", _stage(db, "U_A", CART_60))
    msgs = spy.replies[-1][1]
    assert [m["type"] for m in msgs] == ["text"]
    assert msgs[0]["text"].endswith(
        "ออเดอร์นี้ #2 60฿\nออเดอร์ก่อนหน้า #1 60฿\nรวมที่ต้องจ่าย 120฿\nชำระเงินสดตอนรับอาหารได้เลยค่ะ"
    )
    queued = client.get("/api/kitchen/state?key=secret-123").json()["orders"]["queued"]
    assert [o["pay_cash"] for o in queued] == [True, True]

    # 3. Asking the balance: no QR, and the live state tells the LLM why
    llm.responses = [
        _resp(tool_calls=[_call("p1", "get_payment_info", {})]),
        _resp(content="ยอดค้าง 120 บาทค่ะ ชำระเงินสดตอนรับได้เลยค่ะ"),
    ]
    _say(client, "U_A", "ค้างเท่าไหร่", 2)
    assert llm.calls[-2]["messages"][1]["content"].endswith(
        "| ยอดค้างชำระ=120 บาท | ที่อยู่เดิม=ไม่มี | วิธีจ่าย=เงินสด (ไม่ต้องส่ง QR)"
    )
    info = json.loads(llm.calls[-1]["messages"][-1]["content"])
    assert (info["unpaid_balance"], info["qr_sent"]) == (120, False)
    assert [m["type"] for m in spy.replies[-1][1]] == ["text"]

    # 4. The customer changes their mind and asks for a QR: it is sent, cash is cleared
    llm.responses = [
        _resp(
            tool_calls=[
                _call("t1", "set_payment_method", {"method": "transfer"}),
                _call("t2", "get_payment_info", {"send_qr": True}),
            ]
        ),
        _resp(content="สแกน QR ได้เลยค่ะ"),
    ]
    _say(client, "U_A", "ขอ QR ดีกว่า", 3)
    msgs = spy.replies[-1][1]
    assert [m["type"] for m in msgs] == ["text", "image"]
    assert msgs[1]["originalContentUrl"].startswith("https://shop.example/qr/12000-")
    assert db.get_order(first.id).pay_cash is False  # type: ignore[union-attr]
    assert db.get_or_create_conversation("U_A").pay_cash is False


def test_cash_said_after_a_qr_lookup_in_the_same_turn_sends_no_qr(tmp_path: Path) -> None:
    """Review fix: the final payment intent of the turn decides whether the QR goes out."""
    db = ShopDB(tmp_path / "shop.db", clock=Clock(datetime(2026, 11, 23, 12, 0, tzinfo=BKK)))
    spy = SpyLineClient()
    llm = StubLLM([])
    cfg = _settings(db.db_path, "0812345678")
    client = TestClient(
        create_app(settings=cfg, db=db, menu=load_menu(MENU_PATH), line_client=spy, llm_client=llm)
    )
    _place(db, "U_A", CART_60)
    llm.responses = [
        _resp(
            tool_calls=[
                _call("a", "get_payment_info", {}),
                _call("b", "set_payment_method", {"method": "cash"}),
            ]
        ),
        _resp(content="ยอด 60 บาท ชำระเงินสดตอนรับได้เลยค่ะ"),
    ]
    _say(client, "U_A", "ค้างเท่าไหร่ จ่ายสดนะ", 1)
    assert [m["type"] for m in spy.replies[-1][1]] == ["text"]

    # ...while an explicit QR request in the same turn as cash still sends it
    llm.responses = [
        _resp(
            tool_calls=[
                _call("c", "set_payment_method", {"method": "cash"}),
                _call("d", "get_payment_info", {"send_qr": True}),
            ]
        ),
        _resp(content="QR ค่ะ"),
    ]
    _say(client, "U_A", "ขอ QR ไว้ก่อน", 2)
    assert [m["type"] for m in spy.replies[-1][1]] == ["text", "image"]


def test_cash_customer_never_gets_bank_details_without_promptpay(tmp_path: Path) -> None:
    """Review fix: with PROMPTPAY_ID unset, a cash receipt says cash, not PAYMENT_INFO_TEXT."""
    db = ShopDB(tmp_path / "shop.db", clock=Clock(datetime(2026, 11, 23, 12, 0, tzinfo=BKK)))
    spy = SpyLineClient()
    llm = StubLLM([])
    cfg = _settings(db.db_path, "")  # PAYMENT_INFO_TEXT = "พร้อมเพย์ 081-234-5678"
    client = TestClient(
        create_app(settings=cfg, db=db, menu=load_menu(MENU_PATH), line_client=spy, llm_client=llm)
    )
    db.set_pay_cash("U_A", True)
    _confirm_via_webhook(client, "U_A", _stage(db, "U_A", CART_60))
    text = spy.replies[-1][1][0]["text"]
    assert text.endswith("\n\nชำระเงินสดตอนรับอาหารได้เลยค่ะ")
    assert "พร้อมเพย์" not in text

    llm.responses = [
        _resp(tool_calls=[_call("p", "get_payment_info", {})]),
        _resp(content="ยอด 60 บาทค่ะ"),
    ]
    _say(client, "U_A", "ค้างเท่าไหร่", 1)
    info = json.loads(llm.calls[-1]["messages"][-1]["content"])
    assert "payment_info_text" not in info and info["qr_sent"] is False


def test_cash_rule_holds_across_midnight_and_for_replayed_confirm_taps(tmp_path: Path) -> None:
    """Review fix: one rule (said cash today, or every unpaid order is cash) everywhere."""
    clock = Clock(datetime(2026, 11, 23, 12, 0, tzinfo=BKK))
    db = ShopDB(tmp_path / "shop.db", clock=clock)
    spy = SpyLineClient()
    llm = StubLLM([])
    cfg = _settings(db.db_path, "0812345678")
    client = TestClient(
        create_app(settings=cfg, db=db, menu=load_menu(MENU_PATH), line_client=spy, llm_client=llm)
    )
    db.set_pay_cash("U_A", True)
    first_card = _stage(db, "U_A", CART_60)
    _confirm_via_webhook(client, "U_A", first_card)
    assert [m["type"] for m in spy.replies[-1][1]] == ["text"]

    # Next day: the only unpaid order is a cash order -> still no QR, live state says cash
    clock.now = datetime(2026, 11, 24, 12, 0, tzinfo=BKK)
    llm.responses = [
        _resp(tool_calls=[_call("p", "get_payment_info", {})]),
        _resp(content="ยอด 60 บาท จ่ายเงินสดได้เลยค่ะ"),
    ]
    _say(client, "U_A", "ค้างเท่าไหร่", 1)
    assert llm.calls[-2]["messages"][1]["content"].endswith("| วิธีจ่าย=เงินสด (ไม่ต้องส่ง QR)")
    assert [m["type"] for m in spy.replies[-1][1]] == ["text"]

    # The customer switches to transfer; replaying yesterday's Confirm now offers the QR
    db.set_pay_cash("U_A", False)
    _post(
        client,
        {
            "webhookEventId": "evt-replay",
            "type": "postback",
            "replyToken": "rt-replay",
            "source": {"type": "user", "userId": "U_A"},
            "postback": {"data": f"action=confirm&card_id={first_card}"},
        },
    )
    assert [m["type"] for m in spy.replies[-1][1]] == ["text", "image"]


def test_qr_is_dropped_when_the_owner_records_a_payment_mid_reply(tmp_path: Path) -> None:
    """Full review #5: the QR must match the amount the reply text quotes."""
    db = ShopDB(tmp_path / "shop.db", clock=Clock(datetime(2026, 11, 23, 12, 0, tzinfo=BKK)))
    spy = SpyLineClient()
    cfg = _settings(db.db_path, "0812345678")
    first = _place(db, "U_A", CART_60)
    _place(db, "U_A", CART_70)
    responses = [
        _resp(tool_calls=[_call("p", "get_payment_info", {})]),
        _resp(content="ยอดค้าง 130 บาท สแกน QR ได้เลยค่ะ"),
    ]

    def create(**kwargs: Any) -> Any:
        if len(responses) == 1:  # the owner taps รับเงินสดแล้ว for #1 while the LLM writes
            db.set_payment_status(first.id, "cash")
        return responses.pop(0)

    llm = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    client = TestClient(
        create_app(settings=cfg, db=db, menu=load_menu(MENU_PATH), line_client=spy, llm_client=llm)
    )
    _say(client, "U_A", "ขอ QR", 1)
    assert [m["type"] for m in spy.replies[-1][1]] == ["text"]  # no 70฿ QR under a 130฿ text


def test_replayed_confirm_with_nothing_owed_sends_no_payment_details(tmp_path: Path) -> None:
    """Full review #5: PromptPay off, order already paid -> no PAYMENT_INFO_TEXT."""
    db = ShopDB(tmp_path / "shop.db", clock=Clock(datetime(2026, 11, 23, 12, 0, tzinfo=BKK)))
    spy = SpyLineClient()
    client = TestClient(
        create_app(
            settings=_settings(db.db_path, ""), db=db, menu=load_menu(MENU_PATH), line_client=spy
        )
    )
    card_id = _stage(db, "U_A", CART_60)
    _confirm_via_webhook(client, "U_A", card_id)
    assert "พร้อมเพย์" in spy.replies[-1][1][0]["text"]
    order = db.get_customer_orders_today("U_A")[0]
    db.set_payment_status(order.id, "cash")
    _post(
        client,
        {
            "webhookEventId": "evt-replay-paid",
            "type": "postback",
            "replyToken": "rt-replay-paid",
            "source": {"type": "user", "userId": "U_A"},
            "postback": {"data": f"action=confirm&card_id={card_id}"},
        },
    )
    text = spy.replies[-1][1][0]["text"]
    assert text.startswith("ออเดอร์ #1 ยืนยันไว้แล้วค่ะ") and "พร้อมเพย์" not in text
