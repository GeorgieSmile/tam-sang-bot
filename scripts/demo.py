"""Seeded demo server for running the real app with no live LINE, LLM or SlipOK calls.

    ./scripts/dev.sh demo                      # start on 127.0.0.1:8765 with a fresh seeded DB
    ./scripts/dev.sh say U_DEMO slip 70        # send a fake LINE event, print what LINE got

Kitchen board: http://127.0.0.1:8765/kitchen?key=demo-kitchen-key
Every LINE reply and push is appended to local/demo-line.log. The demo DB is local/demo.db.

The LLM is a stub that echoes the customer's text, so text never creates an order card.
`DEMO_REAL_LLM=1` uses the LLM_* settings from the env or .env instead (paid calls).
U_DEMO starts with an open order card (70฿), so `confirm`, `cancel` and `slip` work without it.
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import hashlib
import hmac
import io
import json
import os
import re
import socket
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
import segno  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from app.config import Settings  # noqa: E402
from app.db import Order, ShopDB  # noqa: E402
from app.main import create_app  # noqa: E402
from app.menu import Menu, load_menu  # noqa: E402
from app.slips import SlipCheck, build_slip_qr, parse_slip_qr  # noqa: E402

BKK = ZoneInfo("Asia/Bangkok")
PORT = int(os.environ.get("DEMO_PORT", "8765"))
DB_PATH = ROOT / "local" / "demo.db"
LOG_PATH = ROOT / "local" / "demo-line.log"
CHANNEL_SECRET = "demo-channel-secret"
KITCHEN_KEY = "demo-kitchen-key"
DEMO_USER = "U_DEMO"
NAMES = {DEMO_USER: "ลูกค้าเดโม", "U_SLIP": "น้องเอ", "U_HI": "พี่บี"}


class DemoClock:
    """Real time, so the board's "minutes ago" add up. Seeding pins one instant (`pinned`)
    and shifts it by `offset` for earlier days, so a midnight mid-seed can't split the days."""

    def __init__(self) -> None:
        self.pinned: datetime | None = None
        self.offset = timedelta(0)

    def __call__(self) -> datetime:
        return (self.pinned or datetime.now(BKK)) + self.offset


class LogLine:
    """LINE stub: logs every outgoing message; serves fake images by message id."""

    def __init__(self, log_path: Path, slip_amounts: dict[str, int]) -> None:
        self.log_path = log_path
        self.slip_amounts = slip_amounts  # transRef -> satang, read by StubSlipOk

    def _log(self, kind: str, target: str, payload: Any) -> None:
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps({"kind": kind, "to": target, "messages": payload}, ensure_ascii=False)
            )
            f.write("\n")

    def reply_messages(self, reply_token: str, messages: list[dict[str, Any]]) -> None:
        self._log("reply", reply_token, messages)

    def push_text(self, user_id: str, text: str) -> None:
        self._log("push", user_id, [{"type": "text", "text": text}])

    def show_loading(self, user_id: str, seconds: int = 20) -> None:
        return None

    def get_profile(self, user_id: str) -> str | None:
        return NAMES.get(user_id)

    def get_message_content(self, message_id: str) -> bytes:
        # "slip-<satang>-<ref>": a slip QR for transRef DEMO<ref>; the amount goes to the stub
        if message_id.startswith("slip-"):
            _, satang, ref = message_id.split("-", 2)
            self.slip_amounts.setdefault(f"DEMO{ref}", int(satang))
            return _png(build_slip_qr("004", f"DEMO{ref}"))
        if message_id.startswith("photo-"):
            return _png("not a slip")
        return b""


def _png(text: str) -> bytes:
    buf = io.BytesIO()
    segno.make(text, micro=False).save(buf, kind="png", scale=8, border=4)
    return buf.getvalue()


class StubSlipOk:
    """SlipOK stub: every demo slip is valid, for the amount it was first sent with."""

    def __init__(self, clock: DemoClock, slip_amounts: dict[str, int]) -> None:
        self.clock = clock
        self.slip_amounts = slip_amounts

    def check(self, qr_data: str) -> SlipCheck:
        slip = parse_slip_qr(qr_data)
        satang = self.slip_amounts.get(slip.trans_ref) if slip else None
        if slip is None or satang is None:
            return SlipCheck(ok=False)
        return SlipCheck(
            ok=True, amount_satang=satang, transfer_at=self.clock(), trans_ref=slip.trans_ref
        )

    def quota(self) -> int | None:
        return 15  # below 20, so the board shows the low-quota warning


class EchoLLM:
    """LLM stub: replies with the customer's last message, never calls a tool."""

    def __init__(self) -> None:
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> Any:
        last = next(
            (m["content"] for m in reversed(kwargs["messages"]) if m.get("role") == "user"), ""
        )
        message = SimpleNamespace(content=f"[demo LLM] ได้รับ: {last}", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


def _kaphrao(qty: int, note: str = "") -> dict[str, Any]:
    item: dict[str, Any] = {
        "dish_id": "kaphrao",
        "qty": qty,
        "option_ids": ["crispy_pork", "fried_egg_runny"],
    }
    if note:
        item["note"] = note
    return item


def _open_card(db: ShopDB, menu: Menu, uid: str, card_id: str, items: list[Any]) -> int:
    conv = db.get_or_create_conversation(uid)
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
    return int(total)


def _place(db: ShopDB, menu: Menu, uid: str, card_id: str, items: list[Any]) -> Order:
    _open_card(db, menu, uid, card_id, items)
    res = db.confirm_card(uid, card_id, menu)
    assert res.ok and res.order is not None, res.error
    return res.order


def seed(db: ShopDB, menu: Menu, clock: DemoClock) -> None:
    """Yesterday's leftovers, today's queue, payment states, inbox cards, and U_DEMO's card."""
    clock.pinned = datetime.now(BKK)
    clock.offset = timedelta(days=-1)
    yesterday = _place(db, menu, "U_OLD", "y1", [_kaphrao(1)])
    db.mark_order_done(yesterday.id)  # ค้างจ่าย from an earlier day
    legacy = _place(db, menu, "U_LEGACY", "y2", [_kaphrao(2)])
    clock.offset = timedelta(0)
    with sqlite3.connect(db.db_path) as conn:  # a pre-v2 order still queued
        conn.execute("UPDATE orders SET payment_status = 'legacy' WHERE id = ?", (legacy.id,))

    done = _place(db, menu, "U_DONE", "t1", [_kaphrao(1)])
    db.mark_order_done(done.id)  # ค้างจ่าย today
    _place(db, menu, "U_Q2", "t2", [_kaphrao(1)])
    db.set_pay_cash("U_Q2", True)  # จะจ่ายสด
    _place(db, menu, "U_Q3", "t3", [_kaphrao(1, "ไม่ใส่ผัก")])
    cash = _place(db, menu, "U_CASH", "t4", [_kaphrao(1)])
    db.set_payment_status(cash.id, "cash")
    _place(db, menu, "U_SLIP", "t5", [_kaphrao(1)])

    db.open_handover("U_SLIP", "slip_amount_mismatch", "สลิป 100฿ · ยอดค้าง 70฿", "[สลิป 100฿]")
    db.open_handover("U_Q3", "slip_duplicate", "เคยใช้แล้วกับ #2 เมื่อ 12:12", "[สลิป]")
    db.open_handover("U_DONE", "slip_unchecked", "ดูรูปสลิปใน LINE OA", "[สลิป]")
    db.open_handover("U_HI", "off_menu", None, "แพ้กุ้งค่ะ กะเพราใส่กุ้งไหมคะ")

    _open_card(db, menu, DEMO_USER, "demo-card", [_kaphrao(1)])
    clock.pinned = None


def _llm_settings() -> dict[str, str]:
    """Only the LLM_* keys, from the env or .env; nothing else from .env is read or exported."""
    keys = {
        "LLM_BASE_URL": "https://openrouter.ai/api/v1",
        "LLM_API_KEY": "",
        "LLM_MODEL": "anthropic/claude-haiku-5.5",
        "LLM_REASONING_EFFORT": "low",
    }
    env_file = ROOT / ".env"
    found: dict[str, str] = {}
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, sep, val = line.strip().partition("=")
            if sep and key.strip() in keys:
                found[key.strip()] = val.strip().strip('"').strip("'")
    return {k: os.environ.get(k) or found.get(k) or default for k, default in keys.items()}


def build_app(db_path: Path, log_path: Path, *, real_llm: bool = False) -> FastAPI:
    """Fresh seeded DB and an app wired to stubs (or the real LLM when asked)."""
    for suffix in ("", "-wal", "-shm", "-journal"):  # the DB and its SQLite sidecars only
        Path(f"{db_path}{suffix}").unlink(missing_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("", encoding="utf-8")
    menu = load_menu(ROOT / "menu.yaml")
    # Open all day, so the demo works at any hour (menu.yaml is not changed)
    menu.shop = dataclasses.replace(menu.shop, open_time="00:00", close_time="24:00")
    clock = DemoClock()
    db = ShopDB(db_path, clock=clock)
    seed(db, menu, clock)
    llm = _llm_settings() if real_llm else None
    slip_amounts: dict[str, int] = {}
    settings = Settings(
        db_path=db_path,
        menu_path=ROOT / "menu.yaml",
        kitchen_secret=KITCHEN_KEY,
        public_base_url="https://demo.invalid",  # PromptPay QR URLs must be https
        payment_info_text="พร้อมเพย์ 081-234-5678",
        line_channel_secret=CHANNEL_SECRET,
        line_channel_access_token="",
        owner_line_user_id="U_OWNER",
        llm_base_url=llm["LLM_BASE_URL"] if llm else "http://127.0.0.1:9/none",
        llm_api_key=llm["LLM_API_KEY"] if llm else "none",
        llm_model=llm["LLM_MODEL"] if llm else "none",
        llm_reasoning_effort=llm["LLM_REASONING_EFFORT"] if llm else "low",
        promptpay_id="0812345678",
    )
    return create_app(
        settings=settings,
        db=db,
        menu=menu,
        line_client=LogLine(log_path, slip_amounts),
        llm_client=None if real_llm else EchoLLM(),
        slip_client=StubSlipOk(clock, slip_amounts),
    )


def make_event(user: str, kind: str, arg: str, db_path: Path) -> dict[str, Any]:
    """One LINE webhook event. kind: text, slip <baht> [ref], photo, voice, confirm, cancel."""
    eid = uuid.uuid4().hex
    event: dict[str, Any] = {
        "type": "message",
        "webhookEventId": eid,
        "replyToken": f"demo-{eid[:8]}",
        "source": {"type": "user", "userId": user},
        "timestamp": 0,
    }
    if kind == "text":
        event["message"] = {"type": "text", "id": eid, "text": arg}
    elif kind == "slip":
        baht, _, ref = arg.partition(" ")
        if ref and not re.fullmatch(r"[A-Za-z0-9]{1,20}", ref):
            raise SystemExit("A slip ref is 1-20 ASCII letters or digits")
        satang = round(float(baht) * 100)
        event["message"] = {"type": "image", "id": f"slip-{satang}-{ref or eid[:12]}"}
    elif kind == "photo":
        event["message"] = {"type": "image", "id": f"photo-{eid}"}
    elif kind == "voice":
        event["message"] = {"type": "audio", "id": eid}
    elif kind in ("confirm", "cancel"):
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT active_card_id FROM conversations WHERE line_user_id = ?", (user,)
            ).fetchone()
        card = row[0] if row and row[0] else arg or "none"
        event["type"] = "postback"
        event["postback"] = {"data": f"action={kind}&card_id={card}"}
    else:
        raise SystemExit(f"Unknown kind {kind!r}: use text, slip, photo, voice, confirm, cancel")
    return event


def sign(body: bytes) -> str:
    digest = hmac.new(CHANNEL_SECRET.encode(), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def _say(user: str, kind: str, arg: str) -> int:
    before = LOG_PATH.stat().st_size if LOG_PATH.exists() else 0
    event = make_event(user, kind, arg, DB_PATH)
    body = json.dumps({"events": [event]}, ensure_ascii=False)
    raw = body.encode()
    resp = httpx.post(
        f"http://127.0.0.1:{PORT}/line/webhook",
        content=raw,
        headers={"x-line-signature": sign(raw), "content-type": "application/json"},
        timeout=120,
    )
    print(f"webhook: HTTP {resp.status_code}")
    with LOG_PATH.open(encoding="utf-8") as f:
        f.seek(before)
        new = f.read().splitlines()
    # Only this event's reply (a concurrent `say` has its own token); pushes have no token
    mine = [
        ln
        for ln in new
        if json.loads(ln)["to"] == event["replyToken"] or json.loads(ln)["kind"] == "push"
    ]
    print("\n".join(mine) or "(no LINE messages sent)")
    return 0 if resp.status_code == 200 else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="start the seeded demo server")
    say = sub.add_parser("say", help="send one fake LINE event to the running demo server")
    say.add_argument("user", help="LINE user id, e.g. U_DEMO")
    say.add_argument("kind", choices=["text", "slip", "photo", "voice", "confirm", "cancel"])
    say.add_argument("arg", nargs="*", help="text, or slip amount in baht plus optional ref")
    args = parser.parse_args()
    if args.cmd == "say":
        return _say(args.user, args.kind, " ".join(args.arg))
    with socket.socket() as probe:  # before wiping the DB: a running demo keeps its data
        if probe.connect_ex(("127.0.0.1", PORT)) == 0:
            print(f"A demo is already running on port {PORT}; stop it first.")
            return 1
    app = build_app(DB_PATH, LOG_PATH, real_llm=os.environ.get("DEMO_REAL_LLM") == "1")
    print(f"Kitchen: http://127.0.0.1:{PORT}/kitchen?key={KITCHEN_KEY}")
    print(f"LINE log: {LOG_PATH}")
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
