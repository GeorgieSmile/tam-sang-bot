"""The seeded demo server (scripts/demo.py) still boots, seeds and handles events end to end."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("demo", ROOT / "scripts" / "demo.py")
assert _spec is not None and _spec.loader is not None
demo: Any = importlib.util.module_from_spec(_spec)
sys.modules["demo"] = demo
_spec.loader.exec_module(demo)


def _send(client: TestClient, log: Path, db: Path, user: str, kind: str, arg: str = "") -> str:
    before = log.stat().st_size
    raw = json.dumps({"events": [demo.make_event(user, kind, arg, db)]}).encode()
    resp = client.post(
        "/line/webhook",
        content=raw,
        headers={"x-line-signature": demo.sign(raw), "content-type": "application/json"},
    )
    assert resp.status_code == 200
    with log.open(encoding="utf-8") as f:
        f.seek(before)
        return f.read()


def test_demo_seeds_board_and_runs_customer_flows(tmp_path: Path) -> None:
    db, log = tmp_path / "demo.db", tmp_path / "line.log"
    client = TestClient(demo.build_app(db, log))

    state = client.get(f"/api/kitchen/state?key={demo.KITCHEN_KEY}").json()
    reasons = sorted(r for card in state["inbox"] for r in card["reasons"])
    assert reasons == ["off_menu", "slip_amount_mismatch", "slip_duplicate", "slip_unchecked"]
    assert state["slip_quota"] == {"left": 15, "warn": True}
    assert len(state["orders"]["queued"]) == 5  # 4 today + the pre-v2 leftover from yesterday
    assert len(state["orders"]["unpaid"]) == 2  # done unpaid: yesterday and today

    assert "รบกวนพิมพ์เป็นข้อความ" in _send(client, log, db, "U_NEW", "voice")
    assert "[demo LLM] ได้รับ: มีอะไรบ้าง" in _send(client, log, db, "U_NEW", "text", "มีอะไรบ้าง")

    # U_DEMO's open 70฿ card: a matching slip confirms it and pays it
    paid = _send(client, log, db, demo.DEMO_USER, "slip", "70 REF1")
    assert "ได้รับยอด 70฿ แล้วค่ะ ✅" in paid
    assert "คิวที่" in paid  # the receipt for the newly confirmed order follows
    # The same slip again, even with another amount, is a duplicate for the owner
    assert "ได้รับสลิปแล้วค่ะ ร้านจะตรวจให้นะคะ" in _send(
        client, log, db, demo.DEMO_USER, "slip", "100 REF1"
    )
    state = client.get(f"/api/kitchen/state?key={demo.KITCHEN_KEY}").json()
    demo_card = next(c for c in state["inbox"] if c["line_user_id"] == demo.DEMO_USER)
    assert demo_card["reasons"] == ["slip_duplicate"]


def test_demo_cancel_then_scripted_order_shows_only_the_new_item(tmp_path: Path) -> None:
    db, log = tmp_path / "demo.db", tmp_path / "line.log"
    client = TestClient(demo.build_app(db, log))
    assert "ยกเลิกสรุปออเดอร์นี้แล้วค่ะ" in _send(client, log, db, demo.DEMO_USER, "cancel")
    order = json.dumps(
        [
            ["update_cart", {"action": "add", "items": [{"dish_id": "water", "qty": 1}]}],
            ["build_order_card", {}],
        ]
    )
    sent = [
        json.loads(ln)
        for ln in _send(client, log, db, demo.DEMO_USER, "text", f"!tools {order}").splitlines()
    ]
    msgs = sent[-1]["messages"]
    assert msgs[0] == {"type": "text", "text": "[demo LLM] ทำแล้ว"}
    card = json.dumps(msgs[1], ensure_ascii=False)
    assert "น้ำเปล่า" in card and "กะเพรา" not in card
