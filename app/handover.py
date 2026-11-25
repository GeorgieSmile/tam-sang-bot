"""Hand-over reason labels and the side effects of a new inbox card (SPEC §3.2)."""

from __future__ import annotations

import logging

from app.config import Settings
from app.db import ShopDB
from app.line import LineClient

log = logging.getLogger("app.handover")

REASON_LABELS_TH = {
    "greeting": "ทักทาย/คุยเล่น",
    "edit_confirmed": "ขอแก้ออเดอร์",
    "off_menu": "ถามส่วนผสม/แพ้อาหาร",
    "image": "ส่งรูป",
    "turn_limit": "คุยเกิน 30 ข้อความวันนี้",
    "paid_cancel_request": "ขอยกเลิกออเดอร์ที่จ่ายแล้ว",
    "owner_takeover": "ร้านคุยเอง",
    "slip_duplicate": "สลิปซ้ำ",
    "slip_nothing_due": "ส่งสลิปแต่ไม่มียอดค้าง",
    "slip_unchecked": "ตรวจสลิปอัตโนมัติไม่ได้",
    "slip_wrong_receiver": "สลิปโอนเข้าบัญชีอื่น",
    "slip_amount_mismatch": "ยอดสลิปไม่ตรง",
    "slip_too_old": "สลิปเก่ากว่าออเดอร์",
    "slip_card_confirm_failed": "ยืนยันออเดอร์จากสลิปไม่ได้",
    "slip_balance_changed": "ยอดค้างเปลี่ยนระหว่างตรวจสลิป",
}

# `greeting` stays a label for old cards; greetings now get a bot reply (Ticket 7)
LLM_HANDOVER_REASONS = ("edit_confirmed", "off_menu")


def reason_label(reason: str, detail: str | None = None) -> str:
    label = REASON_LABELS_TH.get(reason, reason)
    return f"{label} {detail}" if detail else label


def on_new_card(
    db: ShopDB,
    line: LineClient,
    settings: Settings,
    line_user_id: str,
    reason: str,
    detail: str | None = None,
    *,
    owner_alerts_on: bool,
) -> None:
    """Fill the customer's display name from LINE, then push the owner once if alerts are on."""
    customer = db.get_customer(line_user_id)
    name = customer.display_name if customer else None
    if not name:
        try:
            name = line.get_profile(line_user_id)
        except Exception as exc:
            log.warning("LINE profile lookup failed: %s", type(exc).__name__)
            name = None
        if name:
            db.upsert_customer(line_user_id, display_name=name)
    if not owner_alerts_on or not settings.owner_line_user_id:
        return
    shown = name or f"ลูกค้า …{line_user_id[-4:]}"
    try:
        line.push_text(
            settings.owner_line_user_id,
            f"มีลูกค้ารอร้านตอบ: {shown} ({reason_label(reason, detail)})",
        )
    except Exception as exc:
        log.warning("Owner inbox push failed: %s", type(exc).__name__)
