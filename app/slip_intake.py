"""Slip intake (SPEC §3.1.5–7): an image from a customer, checked and matched to unpaid orders.

Every step that ends in a hand-over makes no SlipOK call. Runs only while the bot isn't paused.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from app.agent import handle_image_message, start_handover
from app.config import Settings
from app.db import Order, ShopDB
from app.line import LineClient, format_order_receipt
from app.menu import Menu
from app.slips import (
    SLIPOK_DUPLICATE,
    SLIPOK_PACKAGE_OVER_QUOTA,
    SLIPOK_WRONG_RECEIVER,
    QuotaCache,
    SlipCheck,
    SlipClient,
    find_slip_qr,
    format_baht,
)
from app.wait import receipt_queue_line

log = logging.getLogger("app.slip_intake")

SLIP_GENERAL_REPLY = "ได้รับสลิปแล้วค่ะ ร้านจะตรวจให้นะคะ"
SLIP_CLOSED_LINE = "ตอนนี้ร้านปิดแล้ว ร้านจะตรวจสลิปให้ตอนเปิดร้านค่ะ"
# LINE reply tokens last about a minute: past this, skip SlipOK and hand over instead
SLIPOK_START_DEADLINE_S = 35.0


@dataclass(frozen=True)
class SlipTurn:
    texts: list[str] = field(default_factory=list)
    new_card_reason: str | None = None
    new_card_detail: str | None = None
    confirmed_order: Order | None = None


def handle_image(
    db: ShopDB,
    menu: Menu,
    settings: Settings,
    line: LineClient,
    slip_client: SlipClient | None,
    quota: QuotaCache,
    line_user_id: str,
    message_id: str,
    started_at: float | None = None,
) -> SlipTurn:
    t_start = time.monotonic() if started_at is None else started_at
    quiet = db.is_handed_over(db.get_or_create_conversation(line_user_id))
    if quiet:
        # An image is a customer message: restart the quiet timer on arrival, before download
        db.record_quiet_activity(line_user_id, "[รูปภาพ]")

    # 1–3. Download, decode a QR, parse it as a slip mini-QR; otherwise a plain image hand-over
    try:
        content = line.get_message_content(message_id)
    except Exception as exc:
        log.warning("LINE content download failed: %s", type(exc).__name__)
        content = b""
    _, slip = find_slip_qr(content)
    if slip is None:
        turn = handle_image_message(db, line_user_id, activity_recorded=quiet)
        return SlipTurn(
            texts=[turn.reply_text] if turn.reply_text else [],
            new_card_reason=turn.new_card_reason,
            new_card_detail=turn.new_card_detail,
        )
    ref = slip.trans_ref

    def problem(
        reason: str,
        amount: int | None = None,
        owed: int | None = None,
        card_detail: str | None = None,
    ) -> SlipTurn:
        amount_text = f"สลิป {format_baht(amount)}฿" if amount is not None else "สลิป"
        # Card line: what the owner needs to act on, never a bare "สลิป" (Ticket 7)
        detail = card_detail
        if detail is None and amount is not None:
            detail = amount_text + (f" · ยอดค้าง {format_baht(owed)}฿" if owed is not None else "")
        if reason == "slip_unchecked":
            detail = f"{detail} · ดูรูปสลิปใน LINE OA" if detail else "ดูรูปสลิปใน LINE OA"
        preview = f"[{amount_text}]"
        # Quiet: add to the open card. If the owner resolved it meanwhile, hand over normally
        # so the slip problem is never lost.
        # Quiet is re-checked now: the owner may have tapped คุยเอง during the SlipOK check
        quiet_now = quiet or db.is_handed_over(db.get_or_create_conversation(line_user_id))
        if quiet_now and db.add_reason_to_open_card(line_user_id, reason, detail):
            return SlipTurn()
        if reason == "slip_amount_mismatch" and amount is not None and owed is not None:
            text = (
                f"ได้รับสลิป {format_baht(amount)}฿ แต่ยอดค้างคือ {format_baht(owed)}฿ ส่งให้ร้านตรวจแล้วค่ะ"
            )
        else:
            text = SLIP_GENERAL_REPLY
        if not db.get_shop_status(menu).is_open:
            text += "\n" + SLIP_CLOSED_LINE
        db.append_conversation_event(line_user_id, f"[ลูกค้าส่ง{amount_text}]", text)
        turn = start_handover(db, line_user_id, reason, text, detail=detail, customer_text=preview)
        return SlipTurn(
            texts=[text],
            new_card_reason=turn.new_card_reason,
            new_card_detail=turn.new_card_detail,
        )

    # 4. Seen before (claimed atomically, so two customers can't both use it)
    if not db.claim_slip(ref, line_user_id):
        return problem("slip_duplicate", card_detail=_seen_before(db, ref, line_user_id))
    try:
        return _after_claim(
            db, menu, slip_client, quota, line_user_id, slip.raw, ref, t_start, quiet, problem
        )
    except Exception as exc:
        # Never leave a claim pending: the owner checks this slip by hand
        log.warning("Slip intake failed after claim: %s", type(exc).__name__)
        db.record_slip_check(ref, line_user_id, "slip_unchecked")
        return problem("slip_unchecked")


def _seen_before(db: ShopDB, ref: str, line_user_id: str) -> str | None:
    """Card line for a duplicate: which order the slip paid, or when it was first sent."""
    prev = db.get_slip_check(ref)
    if prev is None:
        return None
    sent = datetime.fromisoformat(str(prev["created_at"]))
    when = sent.strftime("%H:%M")
    if sent.date() != db.now_bkk().date():
        when = f"{sent.day}/{sent.month} {when}"
    who = "" if prev["line_user_id"] == line_user_id else "ลูกค้าคนอื่น"
    numbers = [
        f"#{o.queue_number}"
        for oid in json.loads(str(prev["order_ids_json"] or "[]"))
        if (o := db.get_order(int(oid))) is not None
    ]
    if prev["result"] == "matched" and numbers:
        return f"{who}เคยใช้แล้วกับ {', '.join(numbers)} เมื่อ {when}"
    return f"{who}เคยส่งมาแล้วเมื่อ {when}"


def _after_claim(
    db: ShopDB,
    menu: Menu,
    slip_client: SlipClient | None,
    quota: QuotaCache,
    line_user_id: str,
    raw_qr: str,
    ref: str,
    t_start: float,
    quiet: bool,
    problem: Callable[..., SlipTurn],
) -> SlipTurn:
    # 5. Nothing to pay
    if db.amount_due_satang(line_user_id) == 0:
        db.record_slip_check(ref, line_user_id, "slip_nothing_due")
        return problem("slip_nothing_due")
    # 6. SlipOK not configured, or its quota is known to be used up
    if (
        slip_client is None
        or time.monotonic() - t_start > SLIPOK_START_DEADLINE_S
        or quota.get() == 0
    ):
        db.record_slip_check(ref, line_user_id, "slip_unchecked")
        return problem("slip_unchecked")
    # What this slip could pay before the (slow) SlipOK call; re-checked when paying
    expected = db.slip_snapshot(line_user_id)
    # 7. SlipOK check (receiver + duplicate checks need `log: true`)
    try:
        check = slip_client.check(raw_qr)
    except Exception as exc:
        log.warning("SlipOK check failed: %s", type(exc).__name__)
        check = SlipCheck(ok=False)
    if check.ok and (check.trans_ref or "").strip() != ref:
        # SlipOK must confirm the very transaction we read from the QR
        log.warning("SlipOK transRef missing or different from the slip QR")
        check = SlipCheck(ok=False)
    if not check.ok or check.amount_satang is None or check.transfer_at is None:
        log.warning("SlipOK check not usable: ok=%s code=%s", check.ok, check.error_code)
        if check.error_code == SLIPOK_PACKAGE_OVER_QUOTA:
            quota.mark_exhausted()
        elif check.error_code == SLIPOK_WRONG_RECEIVER:
            quota.used_one()  # SlipOK counts a wrong-receiver check against the quota
        reason = {
            SLIPOK_DUPLICATE: "slip_duplicate",
            SLIPOK_WRONG_RECEIVER: "slip_wrong_receiver",
        }.get(check.error_code or 0, "slip_unchecked")
        db.record_slip_check(ref, line_user_id, reason, check.amount_satang, check.transfer_at)
        card_detail = "SlipOK แจ้งว่าสลิปนี้เคยใช้แล้ว" if reason == "slip_duplicate" else None
        return problem(reason, check.amount_satang, card_detail=card_detail)
    quota.used_one()
    amount = check.amount_satang
    transfer_at: datetime = check.transfer_at

    # 8. Slip match, date rule, and order card confirmation, in one transaction
    res = db.pay_with_slip(line_user_id, ref, amount, transfer_at, menu, expected=expected)
    if res.result != "matched":
        owed = res.owed_satang if res.result == "slip_amount_mismatch" else None
        return problem(res.result, amount, owed)

    numbers = ", ".join(f"#{o.queue_number}" for o in res.paid_orders)
    matched_line = f"ได้รับยอด {format_baht(amount)}฿ แล้วค่ะ ✅ ({numbers})"
    texts = [matched_line]
    # Re-check quiet: the owner may have tapped คุยเอง during the SlipOK check
    quiet = quiet or db.is_handed_over(db.get_or_create_conversation(line_user_id))
    # History records exactly what the customer was sent, in order (AGENTS.md)
    if res.confirmed_order is not None:
        if not quiet:  # while the owner is chatting, only the "matched" line is sent
            texts.append(
                format_order_receipt(
                    res.confirmed_order,
                    "",
                    queue_line=receipt_queue_line(db, menu, res.confirmed_order),
                )
            )
        db.append_conversation_event(
            line_user_id,
            f"[ลูกค้ากดยืนยันออเดอร์ #{res.confirmed_order.queue_number} ด้วยการโอนเงิน"
            f" สลิป {format_baht(amount)}฿ ตะกร้าใหม่ว่างแล้ว]",
            "\n\n".join(texts),
            compact_for_confirmed_order=True,
        )
    else:
        db.append_conversation_event(
            line_user_id, f"[ลูกค้าส่งสลิป {format_baht(amount)}฿]", matched_line
        )
    return SlipTurn(texts=texts, confirmed_order=res.confirmed_order)
