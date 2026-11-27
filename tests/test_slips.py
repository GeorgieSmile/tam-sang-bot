"""Behavioral tests for v2 Ticket 4: slip intake and slip match (SlipOK stub, synthetic slips)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import segno
from fastapi.testclient import TestClient
from PIL import Image

from app.agent import IMAGE_HANDOVER_REPLY, build_card_snapshot_for_conv
from app.config import Settings
from app.db import Order, ShopDB
from app.main import create_app
from app.menu import load_menu
from app.slip_intake import SLIP_CLOSED_LINE, SLIP_GENERAL_REPLY
from app.slips import SlipCheck, build_slip_qr, match_slip, parse_slip_qr, parse_slipok_response

MENU_PATH = Path(__file__).resolve().parent.parent / "menu.yaml"
BKK = ZoneInfo("Asia/Bangkok")
SECRET = "test-channel-secret"
KEY = "?key=secret-123"
T0 = datetime(2026, 11, 24, 12, 0, tzinfo=BKK)
CART_60 = [{"dish_id": "kaphrao", "qty": 1, "option_ids": ["crispy_pork"]}]
CART_70 = [{"dish_id": "kaphrao", "qty": 1, "option_ids": ["crispy_pork", "fried_egg_runny"]}]


def slip_png(trans_ref: str, bank: str = "004") -> bytes:
    """A synthetic slip: just the mini-QR on white (no real names, accounts or references)."""
    buf = io.BytesIO()
    segno.make(build_slip_qr(bank, trans_ref), error="m").save(buf, kind="png", scale=6)
    return buf.getvalue()


def plain_png() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (200, 200), "white").save(buf, format="PNG")
    return buf.getvalue()


def other_qr_png() -> bytes:
    buf = io.BytesIO()
    segno.make("https://example.com/menu", error="m").save(buf, kind="png", scale=6)
    return buf.getvalue()


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class SpyLineClient:
    def __init__(self) -> None:
        self.replies: list[tuple[str, list[dict[str, Any]]]] = []
        self.pushes: list[tuple[str, str]] = []
        self.contents: dict[str, bytes] = {}
        self.on_download: Any = None

    def reply_messages(self, reply_token: str, messages: list[dict[str, Any]]) -> None:
        self.replies.append((reply_token, messages))

    def push_text(self, user_id: str, text: str) -> None:
        self.pushes.append((user_id, text))

    def show_loading(self, user_id: str, seconds: int = 20) -> None:
        return None

    def get_profile(self, user_id: str) -> str | None:
        return None

    def get_message_content(self, message_id: str) -> bytes:
        if self.on_download is not None:
            self.on_download()
        return self.contents[message_id]


class StubSlipClient:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.quota_calls = 0
        self.quota_left: int | None = 100
        self.next: SlipCheck = SlipCheck(ok=False)
        self.raise_timeout = False

    def check(self, qr_data: str) -> SlipCheck:
        self.calls.append(qr_data)
        if self.raise_timeout:
            raise TimeoutError("SlipOK timed out")
        return self.next

    def quota(self) -> int | None:
        self.quota_calls += 1
        return self.quota_left


class FailingLLM:
    @property
    def chat(self) -> Any:
        raise AssertionError("LLM must not be called for images")


class Harness:
    def __init__(self, tmp_path: Path, *, slipok_configured: bool = True) -> None:
        self.menu = load_menu(MENU_PATH)
        self.clock = Clock(T0)
        self.db = ShopDB(tmp_path / "shop.db", clock=self.clock)
        self.line = SpyLineClient()
        self.slipok = StubSlipClient()
        cfg = Settings(
            db_path=tmp_path / "shop.db",
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
        self.client = TestClient(
            create_app(
                settings=cfg,
                db=self.db,
                menu=self.menu,
                line_client=self.line,
                llm_client=FailingLLM(),
                slip_client=self.slipok if slipok_configured else None,
            )
        )
        self._n = 0

    def place(self, user_id: str, items: list[Any]) -> Order:
        card_id = self.stage(user_id, items)
        res = self.db.confirm_card(user_id, card_id, self.menu)
        assert res.ok and res.order is not None
        return res.order

    def stage(self, user_id: str, items: list[Any]) -> str:
        conv = self.db.get_or_create_conversation(user_id)
        self.db.save_draft(conv.id, {"items": items, "fulfillment": "pickup"})
        snap, err = build_card_snapshot_for_conv(
            self.db, self.db.get_or_create_conversation(user_id), self.menu
        )
        assert snap is not None, err
        return str(snap["card_id"])

    def send_image(self, user_id: str, content: bytes) -> list[dict[str, Any]]:
        self._n += 1
        mid = f"img-{self._n}"
        self.line.contents[mid] = content
        before = len(self.line.replies)
        event = {
            "webhookEventId": f"evt-{self._n}",
            "type": "message",
            "replyToken": f"rt-{self._n}",
            "source": {"type": "user", "userId": user_id},
            "message": {"type": "image", "id": mid},
        }
        raw = json.dumps({"events": [event]}).encode()
        sig = base64.b64encode(hmac.new(SECRET.encode(), raw, hashlib.sha256).digest()).decode()
        res = self.client.post(
            "/line/webhook",
            content=raw,
            headers={"Content-Type": "application/json", "x-line-signature": sig},
        )
        assert res.status_code == 200
        new = self.line.replies[before:]
        return new[0][1] if new else []

    def slip(
        self,
        user_id: str,
        ref: str,
        baht: float,
        at: datetime | None = None,
        returned_ref: str | None = None,
    ) -> list[Any]:
        self.slipok.next = SlipCheck(
            ok=True,
            amount_satang=round(baht * 100),
            transfer_at=at or self.clock.now,
            trans_ref=returned_ref or ref,
        )
        return [m["text"] for m in self.send_image(user_id, slip_png(ref))]

    def card(self, user_id: str) -> dict[str, Any]:
        cards = [
            c
            for c in self.client.get(f"/api/kitchen/state{KEY}").json()["inbox"]
            if c["line_user_id"] == user_id
        ]
        assert len(cards) == 1
        return cards[0]

    def payment(self, order_id: int) -> str:
        order = self.db.get_order(order_id)
        assert order is not None
        return order.payment_status


def test_slip_mini_qr_parses_and_other_qrs_do_not() -> None:
    raw = build_slip_qr("004", "TESTREF0001")
    parsed = parse_slip_qr(raw)
    assert parsed is not None
    assert (parsed.bank_code, parsed.trans_ref, parsed.crc_ok) == ("004", "TESTREF0001", True)
    assert parse_slip_qr("https://example.com") is None
    assert parse_slip_qr("00020101021229370016A000000677010111011300668123456785802TH") is None


def test_slipok_response_parsing_follows_the_documented_shapes() -> None:
    ok = parse_slipok_response(
        200,
        {
            "success": True,
            "data": {
                "success": True,
                "transRef": "TESTREF",
                "transDate": "20261124",
                "transTime": "10:15:07",
                "amount": 130,
            },
        },
    )
    assert ok.ok and ok.amount_satang == 13000 and ok.trans_ref == "TESTREF"
    assert ok.transfer_at == datetime(2026, 11, 24, 10, 15, 7, tzinfo=BKK)
    utc = parse_slipok_response(
        200,
        {"success": True, "data": {"amount": 70.5, "transTimestamp": "2026-11-23T20:00:00.000Z"}},
    )
    assert utc.amount_satang == 7050
    assert utc.transfer_at is not None
    assert utc.transfer_at.astimezone(BKK).date().isoformat() == "2026-11-24"
    err = parse_slipok_response(400, {"code": 1012, "message": "สลิปซ้ำ", "data": {}})
    assert (err.ok, err.error_code) == (False, 1012)


def test_match_rule_runs_of_oldest_then_single_exact() -> None:
    items = [("o3", 6000), ("card", 7000)]
    assert match_slip(items, 6000) == ["o3"]
    assert match_slip(items, 13000) == ["o3", "card"]
    assert match_slip(items, 7000) == ["card"]
    assert match_slip(items, 10000) is None
    assert match_slip(items, 7050) is None


def test_image_without_slip_qr_hands_over_without_slipok(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.place("U_A", CART_60)
    for content in (plain_png(), other_qr_png()):
        for open_card in h.db.get_open_handovers():
            h.db.resolve_handover(open_card["id"])
        msgs = h.send_image("U_A", content)
        assert [m["text"] for m in msgs] == [IMAGE_HANDOVER_REPLY]
        assert h.card("U_A")["reasons"] == ["image"]
    assert h.slipok.calls == []


@pytest.mark.parametrize(
    ("baht", "expect_o3", "expect_card"),
    [(60, "slip", None), (130, "slip", "slip"), (70, "unpaid", "slip"), (100, "unpaid", None)],
)
def test_slip_match_against_unpaid_order_and_open_card(
    tmp_path: Path, baht: int, expect_o3: str, expect_card: str | None
) -> None:
    h = Harness(tmp_path)
    for i in range(1, 3):
        h.place(f"U_F{i}", CART_60)
    o3 = h.place("U_A", CART_60)
    assert o3.queue_number == 3
    h.stage("U_A", CART_70)

    texts = h.slip("U_A", f"REF{baht}", baht)
    assert len(h.slipok.calls) == 1
    assert h.payment(o3.id) == expect_o3
    new_orders = [o for o in h.db.get_customer_orders_today("U_A") if o.id != o3.id]
    if expect_card is None:
        assert new_orders == []
    else:
        assert [(o.total, o.payment_status) for o in new_orders] == [(70, "slip")]
        assert h.db.get_or_create_conversation("U_A").active_card is None
    if baht == 60:
        assert texts == ["ได้รับยอด 60฿ แล้วค่ะ ✅ (#3)"]
    elif baht == 130:
        assert texts[0] == "ได้รับยอด 130฿ แล้วค่ะ ✅ (#3, #4)"
        assert texts[1].startswith("รับออเดอร์ #4 แล้วค่ะ") and "QR" not in texts[1]
    elif baht == 70:
        assert texts[0] == "ได้รับยอด 70฿ แล้วค่ะ ✅ (#4)"
    else:
        assert texts == ["ได้รับสลิป 100฿ แต่ยอดค้างคือ 130฿ ส่งให้ร้านตรวจแล้วค่ะ"]
        assert h.card("U_A")["reasons"] == ["slip_amount_mismatch"]
        assert h.card("U_A")["reason_lines"] == ["ยอดสลิปไม่ตรง สลิป 100฿ · ยอดค้าง 130฿"]
    # The LLM-facing history holds exactly what was sent, in order (AGENTS.md out-of-band rule)
    assert h.db.get_or_create_conversation("U_A").messages[-1]["content"] == "\n\n".join(texts)


def test_duplicate_wrong_receiver_and_nothing_due(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    o = h.place("U_A", CART_60)
    assert h.slip("U_A", "REFDUP", 60) == ["ได้รับยอด 60฿ แล้วค่ะ ✅ (#1)"]
    assert h.payment(o.id) == "slip"
    h.place("U_A", CART_60)

    # The same slip again: never sent to SlipOK
    assert h.slip("U_A", "REFDUP", 60) == [SLIP_GENERAL_REPLY]
    assert len(h.slipok.calls) == 1
    assert h.card("U_A")["reasons"] == ["slip_duplicate"]

    h.slipok.next = SlipCheck(ok=False, error_code=1014)
    h.place("U_B", CART_60)
    msgs = h.send_image("U_B", slip_png("REFRECV"))
    assert [m["text"] for m in msgs] == [SLIP_GENERAL_REPLY]
    assert h.card("U_B")["reasons"] == ["slip_wrong_receiver"]
    assert "ปลอม" not in SLIP_GENERAL_REPLY

    calls = len(h.slipok.calls)
    assert h.slip("U_C", "REFNONE", 60) == [SLIP_GENERAL_REPLY]
    assert len(h.slipok.calls) == calls
    assert h.card("U_C")["reasons"] == ["slip_nothing_due"]


def test_date_rule(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    o5 = h.place("U_A", CART_60)
    h.slip("U_A", "REFOLD", 60, at=T0 - timedelta(days=1))
    assert h.payment(o5.id) == "unpaid"
    assert h.card("U_A")["reasons"] == ["slip_too_old"]

    h2 = Harness(tmp_path / "b")
    y = h2.place("U_A", CART_60)
    h2.clock.now = T0 + timedelta(days=1)
    assert h2.slip("U_A", "REFLATE", 60) == ["ได้รับยอด 60฿ แล้วค่ะ ✅ (#1)"]
    assert h2.payment(y.id) == "slip"


def test_sold_out_card_is_not_confirmed_and_nothing_is_paid(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    o = h.place("U_A", CART_60)
    h.stage("U_A", CART_70)
    h.db.set_override("ingredient", "crispy_pork", False)
    h.slip("U_A", "REFSOLD", 130)
    assert h.payment(o.id) == "unpaid"
    assert len(h.db.get_customer_orders_today("U_A")) == 1
    assert h.card("U_A")["reasons"] == ["slip_card_confirm_failed"]


def test_shop_closed(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    o = h.place("U_A", CART_60)
    h.db.set_open_override("closed")
    assert h.slip("U_A", "REFC1", 100) == [
        "ได้รับสลิป 100฿ แต่ยอดค้างคือ 60฿ ส่งให้ร้านตรวจแล้วค่ะ\n" + SLIP_CLOSED_LINE
    ]
    h.db.resolve_handover(h.card("U_A")["id"])
    assert h.slip("U_A", "REFC2", 60) == ["ได้รับยอด 60฿ แล้วค่ะ ✅ (#1)"]
    assert h.payment(o.id) == "slip"


def test_quiet_rules_and_bot_paused(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    o1 = h.place("U_A", CART_60)
    h.place("U_A", CART_70)
    h.db.open_handover("U_A", "greeting")
    assert h.slip("U_A", "REFQ1", 60) == ["ได้รับยอด 60฿ แล้วค่ะ ✅ (#1)"]
    assert h.payment(o1.id) == "slip"
    assert h.slip("U_A", "REFQ2", 100) == []
    card = h.card("U_A")
    assert card["reasons"] == ["greeting", "slip_amount_mismatch"]
    assert "ยอดสลิปไม่ตรง สลิป 100฿ · ยอดค้าง 70฿" in card["reason_lines"]

    calls = len(h.slipok.calls)
    h.db.set_bot_enabled(False)
    h.place("U_B", CART_60)
    assert h.slip("U_B", "REFP", 60) == []
    assert len(h.slipok.calls) == calls


def test_quota_warning_and_zero_quota(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.slipok.quota_left = 15
    state = h.client.get(f"/api/kitchen/state{KEY}").json()
    assert state["slip_quota"] == {"left": 15, "warn": True}
    h.client.get(f"/api/kitchen/state{KEY}")
    assert h.slipok.quota_calls == 1  # read at most once an hour

    h2 = Harness(tmp_path / "zero")
    h2.slipok.quota_left = 0
    h2.place("U_A", CART_60)
    assert h2.slip("U_A", "REFZ", 60) == [SLIP_GENERAL_REPLY]
    assert h2.slipok.calls == []
    assert h2.card("U_A")["reasons"] == ["slip_unchecked"]


def test_paid_picker_marks_only_ticked_orders(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    o3 = h.place("U_A", CART_60)
    o5 = h.place("U_A", CART_70)
    h.slip("U_A", "REFPICK", 100)
    card = h.card("U_A")
    assert [o["id"] for o in card["unpaid_orders"]] == [o3.id, o5.id]
    res = h.client.post(
        f"/api/kitchen/handovers/{card['id']}/mark-paid{KEY}", json={"order_ids": [o5.id]}
    )
    assert res.status_code == 200
    assert (h.payment(o3.id), h.payment(o5.id)) == ("unpaid", "transfer_manual")
    # Orders of another customer can't be marked through this card
    other = h.place("U_B", CART_60)
    h.client.post(
        f"/api/kitchen/handovers/{card['id']}/mark-paid{KEY}", json={"order_ids": [other.id]}
    )
    assert h.payment(other.id) == "unpaid"


def test_every_decoded_slip_is_recorded_with_its_result(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    o = h.place("U_A", CART_60)
    h.slip("U_A", "REFREC1", 60)
    row = h.db.get_slip_check("REFREC1")
    assert row is not None
    assert (row["result"], row["amount_satang"], row["line_user_id"]) == ("matched", 6000, "U_A")
    assert json.loads(row["order_ids_json"]) == [o.id]
    h.place("U_A", CART_70)
    h.slip("U_A", "REFREC2", 100)
    row2 = h.db.get_slip_check("REFREC2")
    assert row2 is not None and row2["result"] == "slip_amount_mismatch"


def test_one_slip_can_never_pay_two_customers(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    a = h.place("U_A", CART_60)
    b = h.place("U_B", CART_60)
    assert h.slip("U_A", "REFSHARED", 60) == ["ได้รับยอด 60฿ แล้วค่ะ ✅ (#1)"]
    assert h.slip("U_B", "REFSHARED", 60) == [SLIP_GENERAL_REPLY]
    assert len(h.slipok.calls) == 1
    assert (h.payment(a.id), h.payment(b.id)) == ("slip", "unpaid")
    # Even if a second payment attempt got past intake, it needs this customer's pending claim
    res = h.db.pay_with_slip("U_B", "REFSHARED", 6000, T0, h.menu)
    assert res.result == "slip_duplicate" and res.paid_orders == []
    assert h.payment(b.id) == "unpaid"


def test_card_in_a_match_needs_a_slip_dated_today(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.stage("U_A", CART_70)
    h.slip("U_A", "REFCARDOLD", 70, at=T0 - timedelta(days=1))
    assert h.db.get_customer_orders_today("U_A") == []
    assert h.card("U_A")["reasons"] == ["slip_too_old"]


def test_card_is_not_confirmed_while_the_shop_is_closed(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.stage("U_A", CART_70)
    h.db.set_open_override("closed")
    assert h.slip("U_A", "REFCLOSEDCARD", 70) == [SLIP_GENERAL_REPLY + "\n" + SLIP_CLOSED_LINE]
    assert h.db.get_customer_orders_today("U_A") == []
    assert h.card("U_A")["reasons"] == ["slip_card_confirm_failed"]


def test_unconfigured_timeout_provider_duplicate_and_mismatched_ref(tmp_path: Path) -> None:
    h0 = Harness(tmp_path / "unconfigured", slipok_configured=False)
    h0.place("U_A", CART_60)
    assert h0.slip("U_A", "REFNOKEY", 60) == [SLIP_GENERAL_REPLY]
    assert h0.card("U_A")["reasons"] == ["slip_unchecked"]
    assert h0.slipok.calls == []

    h = Harness(tmp_path)
    o = h.place("U_A", CART_60)
    h.slipok.raise_timeout = True
    assert [m["text"] for m in h.send_image("U_A", slip_png("REFTIMEOUT"))] == [SLIP_GENERAL_REPLY]
    assert h.card("U_A")["reasons"] == ["slip_unchecked"]
    h.slipok.raise_timeout = False

    h.slipok.next = SlipCheck(ok=False, error_code=1012)
    h.send_image("U_A", slip_png("REFPROVDUP"))
    assert "slip_duplicate" in h.card("U_A")["reasons"]

    h.slip("U_A", "REFQR", 60, returned_ref="REFOTHER")
    assert h.payment(o.id) == "unpaid"
    row = h.db.get_slip_check("REFQR")
    assert row is not None and row["result"] == "slip_unchecked"


def test_slipok_amounts_must_be_exact_satang() -> None:
    def amount(value: Any) -> int | None:
        body = {"success": True, "data": {"amount": value, "transDate": "20261124"}}
        return parse_slipok_response(200, body).amount_satang

    assert amount(60) == 6000
    assert amount(70.5) == 7050
    assert amount("130.00") == 13000
    for bad in (60.001, "60.000000001", 0, -5, float("nan"), float("inf"), True, None, "abc"):
        assert amount(bad) is None
    assert parse_slip_qr("00" + "๑๒" + "x" * 12) is None


def test_quiet_slip_match_restarts_the_quiet_timer(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.place("U_A", CART_60)
    h.db.open_handover("U_A", "greeting")
    h.clock.now = T0 + timedelta(minutes=14)
    assert h.slip("U_A", "REFQT", 60) == ["ได้รับยอด 60฿ แล้วค่ะ ✅ (#1)"]
    h.clock.now = T0 + timedelta(minutes=16)
    assert h.db.is_handed_over(h.db.get_or_create_conversation("U_A")) is True


def test_unexpected_intake_failure_still_replies(tmp_path: Path, monkeypatch: Any) -> None:
    h = Harness(tmp_path)
    h.place("U_A", CART_60)

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("db hiccup")

    # A failure after the slip was claimed: general slip reply, claim finalized as unchecked
    monkeypatch.setattr(h.db, "pay_with_slip", boom)
    assert h.slip("U_A", "REFBOOM", 60) == [SLIP_GENERAL_REPLY]
    row = h.db.get_slip_check("REFBOOM")
    assert row is not None and row["result"] == "slip_unchecked"
    assert h.card("U_A")["reasons"] == ["slip_unchecked"]

    # A failure before any claim: the webhook still replies with a plain image hand-over
    import app.slip_intake as intake

    monkeypatch.setattr(intake, "find_slip_qr", boom)
    h.db.resolve_handover(h.card("U_A")["id"])
    assert [m["text"] for m in h.send_image("U_A", slip_png("REFBOOM2"))] == [IMAGE_HANDOVER_REPLY]


def test_paid_picker_is_only_for_slip_cards(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    o = h.place("U_A", CART_60)
    card_id, _ = h.db.open_handover("U_A", "greeting")
    res = h.client.post(
        f"/api/kitchen/handovers/{card_id}/mark-paid{KEY}", json={"order_ids": [o.id]}
    )
    assert res.status_code == 409
    assert h.payment(o.id) == "unpaid"


def test_slipok_success_without_matching_ref_never_pays(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    o = h.place("U_A", CART_60)
    h.slipok.next = SlipCheck(ok=True, amount_satang=6000, transfer_at=T0, trans_ref=None)
    assert [m["text"] for m in h.send_image("U_A", slip_png("REFNOREF"))] == [SLIP_GENERAL_REPLY]
    assert h.payment(o.id) == "unpaid"
    row = h.db.get_slip_check("REFNOREF")
    assert row is not None and row["result"] == "slip_unchecked"


def test_quiet_timer_restarts_before_a_slow_download(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    h.place("U_A", CART_60)
    h.db.open_handover("U_A", "greeting")  # quiet until T0 + 15 min
    h.clock.now = T0 + timedelta(minutes=14, seconds=59)

    def slow() -> None:
        h.clock.now += timedelta(seconds=2)

    h.line.on_download = slow
    h.slip("U_A", "REFSLOW", 100)  # mismatch while quiet: no reply
    assert h.db.is_handed_over(h.db.get_or_create_conversation("U_A")) is True
    card = h.card("U_A")
    assert card["unseen_count"] == 1
    assert "slip_amount_mismatch" in card["reasons"]


def test_picker_refuses_a_card_resolved_meanwhile(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    o = h.place("U_A", CART_60)
    h.slip("U_A", "REFRES", 100)
    card_id = h.card("U_A")["id"]
    assert h.db.resolve_handover(card_id) is True
    assert h.db.mark_transfer_manual(card_id, [o.id]) is None
    res = h.client.post(
        f"/api/kitchen/handovers/{card_id}/mark-paid{KEY}", json={"order_ids": [o.id]}
    )
    assert res.status_code == 409
    assert h.payment(o.id) == "unpaid"


def test_deadline_skips_slipok_and_the_quota_read(tmp_path: Path) -> None:
    from app.slip_intake import handle_image
    from app.slips import QuotaCache

    h = Harness(tmp_path)
    h.place("U_A", CART_60)
    h.line.contents["late"] = slip_png("REFLATE40")
    quota = QuotaCache(h.slipok, h.db.now_bkk)  # cold cache
    turn = handle_image(
        h.db,
        h.menu,
        h.client.app.state.settings,  # type: ignore[attr-defined]
        h.line,
        h.slipok,
        quota,
        "U_A",
        "late",
        started_at=time.monotonic() - 40,
    )
    assert turn.texts == [SLIP_GENERAL_REPLY]
    assert h.slipok.calls == [] and h.slipok.quota_calls == 0


def test_slipok_settings_are_validated_at_startup(tmp_path: Path) -> None:
    import dataclasses

    h = Harness(tmp_path)
    good = h.client.app.state.settings  # type: ignore[attr-defined]
    # Correct order, and not configured at all, both start
    for key, branch in (("SLIPOKABC123X", "12345"), ("", "")):
        create_app(
            settings=dataclasses.replace(good, slipok_api_key=key, slipok_branch_id=branch),
            db=h.db,
            menu=h.menu,
        )
    # Swapped values (the real Ticket 6 mistake) and a half-set config refuse to start
    for key, branch in (("12345", "SLIPOKABC123X"), ("SLIPOKABC123X", ""), ("", "12345")):
        bad = dataclasses.replace(good, slipok_api_key=key, slipok_branch_id=branch)
        with pytest.raises(ValueError):
            create_app(settings=bad, db=h.db, menu=h.menu)


def test_refused_slipok_check_logs_its_error_code(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    h = Harness(tmp_path)
    h.place("U_A", CART_60)
    h.slipok.next = SlipCheck(ok=False, error_code=1000)
    with caplog.at_level("WARNING", logger="app.slip_intake"):
        assert [m["text"] for m in h.send_image("U_A", slip_png("REFLOG"))] == [SLIP_GENERAL_REPLY]
    assert "SlipOK check not usable: ok=False code=1000" in caplog.text
    assert h.card("U_A")["reasons"] == ["slip_unchecked"]


def test_duplicate_and_unchecked_cards_say_what_the_owner_needs(tmp_path: Path) -> None:
    """Ticket 7: no bare "สลิป" on a card; say where the slip was used, or where to look."""
    h = Harness(tmp_path)
    h.place("U_A", CART_60)
    assert h.slip("U_A", "REFUSED1", 60) == ["ได้รับยอด 60฿ แล้วค่ะ ✅ (#1)"]
    h.place("U_A", CART_60)
    h.send_image("U_A", slip_png("REFUSED1"))
    assert h.card("U_A")["reason_lines"] == ["สลิปซ้ำ เคยใช้แล้วกับ #1 เมื่อ 12:00"]

    # The same slip from another customer says so
    h.place("U_B", CART_60)
    h.send_image("U_B", slip_png("REFUSED1"))
    assert h.card("U_B")["reason_lines"] == ["สลิปซ้ำ ลูกค้าคนอื่นเคยใช้แล้วกับ #1 เมื่อ 12:00"]

    # SlipOK could not check it: tell the owner where the slip image is
    h2 = Harness(tmp_path / "unchecked")
    h2.slipok.quota_left = 0
    h2.place("U_A", CART_60)
    h2.slip("U_A", "REFQ0", 60)
    assert h2.card("U_A")["reason_lines"] == ["ตรวจสลิปอัตโนมัติไม่ได้ ดูรูปสลิปใน LINE OA"]
    # A resend of that never-checked slip says when it was first sent
    h2.clock.now = T0 + timedelta(minutes=5)
    h2.db.resolve_handover(h2.card("U_A")["id"])
    h2.send_image("U_A", slip_png("REFQ0"))
    assert h2.card("U_A")["reason_lines"] == ["สลิปซ้ำ เคยส่งมาแล้วเมื่อ 12:00"]


def test_unchecked_slip_with_a_known_amount_still_points_to_line_oa(tmp_path: Path) -> None:
    """Review fix: a SlipOK answer missing the transfer time is unchecked, amount shown."""
    h = Harness(tmp_path)
    h.place("U_A", CART_60)
    h.slipok.next = SlipCheck(ok=True, amount_satang=6000, transfer_at=None, trans_ref="REFNT")
    h.send_image("U_A", slip_png("REFNT"))
    assert h.card("U_A")["reason_lines"] == ["ตรวจสลิปอัตโนมัติไม่ได้ สลิป 60฿ · ดูรูปสลิปใน LINE OA"]


def _check_with(h: Harness, effect: Any, result: SlipCheck) -> None:
    """Make the SlipOK stub run `effect` while it is 'checking' (a slow network call)."""

    def check(qr: str) -> SlipCheck:
        h.slipok.calls.append(qr)
        effect()
        return result

    h.slipok.check = check  # type: ignore[method-assign,assignment]


def test_owner_recording_the_transfer_during_the_check_never_double_credits(
    tmp_path: Path,
) -> None:
    """Full review #1: one transfer, one credit, even if the owner taps รับโอนแล้ว meanwhile."""
    h = Harness(tmp_path)
    o1 = h.place("U_A", CART_60)
    h.stage("U_A", CART_60)  # an open 60฿ card the same slip could otherwise pay
    _check_with(
        h,
        lambda: h.db.set_payment_status(o1.id, "transfer_manual"),
        SlipCheck(ok=True, amount_satang=6000, transfer_at=T0, trans_ref="REFRACE"),
    )
    msgs = h.send_image("U_A", slip_png("REFRACE"))
    assert [m["text"] for m in msgs] == [SLIP_GENERAL_REPLY]
    assert [o.id for o in h.db.get_customer_orders_today("U_A")] == [o1.id]  # no #2
    assert h.payment(o1.id) == "transfer_manual"
    assert h.card("U_A")["reasons"] == ["slip_balance_changed"]
    assert h.db.get_slip_check("REFRACE")["result"] == "slip_balance_changed"  # type: ignore[index]


def test_slip_problem_after_the_owner_closed_the_card_is_not_lost(tmp_path: Path) -> None:
    """Full review #2: a quiet card resolved during the SlipOK check -> normal hand-over."""
    h = Harness(tmp_path)
    h.place("U_A", CART_60)
    card_id, _ = h.db.open_handover("U_A", "off_menu")
    _check_with(
        h,
        lambda: h.db.resolve_handover(card_id),
        SlipCheck(ok=True, amount_satang=10000, transfer_at=T0, trans_ref="REFLATECARD"),
    )
    msgs = h.send_image("U_A", slip_png("REFLATECARD"))
    assert [m["text"] for m in msgs] == ["ได้รับสลิป 100฿ แต่ยอดค้างคือ 60฿ ส่งให้ร้านตรวจแล้วค่ะ"]
    assert h.card("U_A")["reasons"] == ["slip_amount_mismatch"]


def test_wrong_receiver_keeps_the_amount_and_uses_quota(tmp_path: Path) -> None:
    """Full review #10: SlipOK's documented 1014 shape carries data and costs one check."""
    h = Harness(tmp_path)
    h.slipok.quota_left = 5
    h.place("U_A", CART_60)
    h.slipok.next = parse_slipok_response(
        400,
        {
            "success": False,
            "code": 1014,
            "message": "บัญชีผู้รับไม่ตรงกับบัญชีหลักของร้าน",
            "data": {
                "amount": 60,
                "transRef": "REF1014",
                "transDate": "20261123",
                "transTime": "11:55:00",
            },
        },
    )
    h.send_image("U_A", slip_png("REF1014"))
    assert h.card("U_A")["reason_lines"] == ["สลิปโอนเข้าบัญชีอื่น สลิป 60฿"]
    check = h.db.get_slip_check("REF1014")
    assert check is not None
    assert (check["amount_satang"], check["transfer_at"]) == (6000, "2026-11-23T11:55:00+07:00")
    assert h.client.get(f"/api/kitchen/state{KEY}").json()["slip_quota"]["left"] == 4


def test_picker_rows_tell_same_numbered_orders_from_different_days_apart(
    tmp_path: Path,
) -> None:
    """Full review #4: yesterday's #1 and today's #1 must not look identical."""
    h = Harness(tmp_path)
    h.place("U_A", CART_60)
    h.clock.now = T0 + timedelta(days=1)
    h.place("U_A", CART_60)
    h.slip("U_A", "REFPICKDAY", 100, at=h.clock.now)
    refs = [o["ref"] for o in h.card("U_A")["unpaid_orders"]]
    assert refs == ["#1 (เมื่อวาน)", "#1"]


def test_takeover_during_the_check_sends_only_the_matched_line(tmp_path: Path) -> None:
    """Tier 3 review A: quiet is re-checked after SlipOK; no receipt over the owner."""
    h = Harness(tmp_path)
    h.place("U_A", CART_60)
    h.stage("U_A", CART_70)
    _check_with(
        h,
        lambda: h.db.open_handover("U_A", "owner_takeover", "#1"),
        SlipCheck(ok=True, amount_satang=13000, transfer_at=T0, trans_ref="REFTAKE"),
    )
    msgs = h.send_image("U_A", slip_png("REFTAKE"))
    assert [m["text"] for m in msgs] == ["ได้รับยอด 130฿ แล้วค่ะ ✅ (#1, #2)"]
    assert h.db.get_or_create_conversation("U_A").messages[-1] == {
        "role": "assistant",
        "content": "ได้รับยอด 130฿ แล้วค่ะ ✅ (#1, #2)",
    }


def test_slip_confirmation_is_in_the_next_llm_call(tmp_path: Path) -> None:
    """AGENTS.md: the exact next-LLM payload after a slip confirms the open card."""
    from types import SimpleNamespace

    from app.agent import handle_customer_text

    h = Harness(tmp_path)
    h.stage("U_A", CART_60)
    texts = h.slip("U_A", "REFNEXT", 60)
    calls: list[dict[str, Any]] = []

    def create(**kwargs: Any) -> Any:
        calls.append(kwargs)
        msg = SimpleNamespace(content="ค่ะ", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    llm = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    settings = h.client.app.state.settings  # type: ignore[attr-defined]
    handle_customer_text(h.db, h.menu, "U_A", "ขอบคุณ", llm_client=llm, settings=settings)
    assert calls[0]["messages"][-3:] == [
        {"role": "user", "content": "[ลูกค้ากดยืนยันออเดอร์ #1 ด้วยการโอนเงิน สลิป 60฿ ตะกร้าใหม่ว่างแล้ว]"},
        {"role": "assistant", "content": "\n\n".join(texts)},
        {"role": "user", "content": "ขอบคุณ"},
    ]
    assert calls[0]["messages"][1]["content"].endswith("| ยอดค้างชำระ=0 บาท")


def test_pause_during_the_check_pays_nothing_and_says_nothing(tmp_path: Path) -> None:
    """Agent polish Ticket 1: the bot paused while SlipOK checked the slip."""
    h = Harness(tmp_path)
    o1 = h.place("U_A", CART_60)
    card_id = h.stage("U_A", CART_70)
    _check_with(
        h,
        lambda: h.db.set_bot_enabled(False),
        SlipCheck(ok=True, amount_satang=13000, transfer_at=T0, trans_ref="REFPAUSE"),
    )
    assert h.send_image("U_A", slip_png("REFPAUSE")) == []
    assert h.line.replies == [] and h.line.pushes == []
    assert h.payment(o1.id) == "unpaid"
    assert h.db.get_or_create_conversation("U_A").active_card_id == card_id  # not confirmed
    assert [o.id for o in h.db.get_customer_orders_today("U_A")] == [o1.id]
    assert h.client.get(f"/api/kitchen/state{KEY}").json()["inbox"] == []
    check = h.db.get_slip_check("REFPAUSE")
    assert check is not None
    assert (check["result"], check["amount_satang"]) == ("slip_unchecked", 13000)
    assert check["transfer_at"] == T0.isoformat(timespec="seconds")


def test_pause_during_the_quota_lookup_sends_nothing(tmp_path: Path) -> None:
    """Review fix: every slip reply path re-checks the pause, not only after SlipOK."""
    h = Harness(tmp_path)
    h.place("U_A", CART_60)

    def quota_pausing() -> int | None:
        h.db.set_bot_enabled(False)
        return 0

    h.slipok.quota = quota_pausing  # type: ignore[method-assign]
    assert h.send_image("U_A", slip_png("REFQUOTA")) == []
    assert h.line.replies == [] and h.line.pushes == []
    assert h.client.get(f"/api/kitchen/state{KEY}").json()["inbox"] == []
    assert h.db.get_slip_check("REFQUOTA")["result"] == "slip_unchecked"  # type: ignore[index]


def test_pause_during_a_slow_quota_lookup_skips_the_paid_check(tmp_path: Path) -> None:
    """Fresh review: no paid SlipOK call after the bot was paused during the quota lookup."""
    h = Harness(tmp_path)
    h.place("U_A", CART_60)

    def quota_pausing() -> int | None:
        h.db.set_bot_enabled(False)
        return 50  # quota left: only the pause stops the check

    h.slipok.quota = quota_pausing  # type: ignore[method-assign]
    assert h.send_image("U_A", slip_png("REFSLOW")) == []
    assert h.slipok.calls == []  # SlipOK was never called
    assert h.line.replies == []
    assert h.db.get_slip_check("REFSLOW")["result"] == "slip_unchecked"  # type: ignore[index]


@pytest.mark.parametrize("case", ["pause", "deadline"])
def test_pause_or_deadline_during_the_snapshot_skips_the_paid_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """Fix review: the last pause/deadline checks sit right before the paid SlipOK call."""
    import app.slip_intake as slip_mod

    h = Harness(tmp_path)
    h.place("U_A", CART_60)
    real_snapshot = h.db.slip_snapshot
    real_monotonic = time.monotonic
    late = {"on": False}

    def snapshot(uid: str) -> Any:
        if case == "pause":
            h.db.set_bot_enabled(False)
        else:
            late["on"] = True  # the SQLite snapshot waited past the start deadline
        return real_snapshot(uid)

    h.db.slip_snapshot = snapshot  # type: ignore[method-assign,assignment]
    monkeypatch.setattr(
        slip_mod,
        "time",
        SimpleNamespace(monotonic=lambda: real_monotonic() + (99 if late["on"] else 0)),
    )
    h.send_image("U_A", slip_png("REFSNAP"))
    assert h.slipok.calls == []  # SlipOK was never called
    assert h.db.get_slip_check("REFSNAP")["result"] == "slip_unchecked"  # type: ignore[index]
    assert (h.line.replies == []) is (case == "pause")  # a late slip still hands over
