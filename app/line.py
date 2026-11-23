"""LINE Messaging API client, signature check, Flex order card, and message formatters."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from typing import Any, Protocol
from urllib.parse import parse_qsl

import httpx

from app.db import Order

log = logging.getLogger("app.line")

LINE_REPLY_URL = "https://api.line.me/v2/bot/message/reply"
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
LINE_LOADING_URL = "https://api.line.me/v2/bot/chat/loading/start"


class LineClient(Protocol):
    def reply_messages(self, reply_token: str, messages: list[dict[str, Any]]) -> None: ...
    def push_text(self, user_id: str, text: str) -> None: ...
    def show_loading(self, user_id: str, seconds: int = 20) -> None: ...


class NoopLineClient:
    def reply_messages(self, reply_token: str, messages: list[dict[str, Any]]) -> None:
        return None

    def push_text(self, user_id: str, text: str) -> None:
        return None

    def show_loading(self, user_id: str, seconds: int = 20) -> None:
        return None


class HttpLineClient:
    def __init__(self, access_token: str, timeout_s: float = 8.0) -> None:
        self._token = access_token
        self._timeout_s = timeout_s

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    def reply_messages(self, reply_token: str, messages: list[dict[str, Any]]) -> None:
        if not self._token or not reply_token or not messages:
            return
        with httpx.Client(timeout=self._timeout_s) as client:
            resp = client.post(
                LINE_REPLY_URL,
                headers=self._headers(),
                json={"replyToken": reply_token, "messages": messages[:5]},
            )
            resp.raise_for_status()

    def push_text(self, user_id: str, text: str) -> None:
        if not self._token or not user_id or not text:
            return
        with httpx.Client(timeout=self._timeout_s) as client:
            resp = client.post(
                LINE_PUSH_URL,
                headers=self._headers(),
                json={"to": user_id, "messages": [{"type": "text", "text": text}]},
            )
            resp.raise_for_status()

    def show_loading(self, user_id: str, seconds: int = 20) -> None:
        if not self._token or not user_id:
            return
        try:
            with httpx.Client(timeout=3.0) as client:
                client.post(
                    LINE_LOADING_URL,
                    headers=self._headers(),
                    json={"chatId": user_id, "loadingSeconds": max(5, min(seconds, 60))},
                )
        except Exception as exc:
            log.debug("LINE loading animation skipped: %s", type(exc).__name__)


def verify_signature(body: bytes, signature: str, channel_secret: str) -> bool:
    """Verify x-line-signature header using HMAC-SHA256 of the raw request body."""
    if not channel_secret or not signature:
        return False
    digest = hmac.new(channel_secret.encode("utf-8"), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode("ascii")
    return hmac.compare_digest(expected, signature)


def parse_postback_data(data: str) -> dict[str, str]:
    return dict(parse_qsl(data, keep_blank_values=False))


def text_message(text: str) -> dict[str, Any]:
    return {"type": "text", "text": text}


def build_order_flex(
    card_snapshot: dict[str, Any],
    *,
    shop_name: str,
    wait_minutes: int | None = None,
) -> dict[str, Any]:
    """Build a LINE Flex Message order summary with Confirm and Cancel postback buttons."""
    card_id = str(card_snapshot["card_id"])
    lines: list[dict[str, Any]] = card_snapshot["priced_lines"]
    fulfillment = str(card_snapshot["fulfillment"])
    address = card_snapshot.get("address")
    requested_time = card_snapshot.get("requested_time")
    food_total = int(card_snapshot["food_total"])
    delivery_fee = int(card_snapshot["delivery_fee"])
    total = int(card_snapshot["total"])

    body_contents: list[dict[str, Any]] = [
        {"type": "text", "text": f"สรุปออเดอร์ · {shop_name}", "weight": "bold", "size": "lg"},
    ]

    if wait_minutes:
        body_contents.append(
            {
                "type": "text",
                "text": f"ช่วงนี้คิวเยอะ รอประมาณ {wait_minutes} นาทีนะคะ",
                "size": "sm",
                "color": "#B45309",
                "weight": "bold",
                "wrap": True,
                "margin": "sm",
            }
        )

    body_contents.append({"type": "separator", "margin": "md"})

    for line in lines:
        opts = [str(o) for o in line.get("option_names_th", [])]
        note_str = str(line.get("note") or "").strip()
        row_box: dict[str, Any] = {
            "type": "box",
            "layout": "horizontal",
            "margin": "sm",
            "contents": [
                {
                    "type": "text",
                    "text": f"{line['dish_name_th']} ×{line['qty']}",
                    "size": "sm",
                    "weight": "bold",
                    "flex": 4,
                    "wrap": True,
                },
                {
                    "type": "text",
                    "text": f"{line['line_total']}฿",
                    "size": "sm",
                    "align": "end",
                    "flex": 1,
                },
            ],
        }
        body_contents.append(row_box)
        if opts:
            body_contents.append(
                {
                    "type": "text",
                    "text": ", ".join(opts),
                    "size": "xs",
                    "color": "#666666",
                    "wrap": True,
                }
            )
        if note_str:
            body_contents.append(
                {
                    "type": "text",
                    "text": f"หมายเหตุ: {note_str}",
                    "size": "xs",
                    "color": "#B45309",
                    "weight": "bold",
                    "wrap": True,
                }
            )

    body_contents.append({"type": "separator", "margin": "md"})

    fulfillment_text = (
        f"ส่ง: {address}" if fulfillment == "delivery" and address else "รับเองที่ร้าน"
    )
    body_contents.append(
        {"type": "text", "text": fulfillment_text, "size": "sm", "wrap": True, "margin": "md"}
    )
    if requested_time:
        body_contents.append(
            {
                "type": "text",
                "text": f"เวลาที่ขอ: {requested_time}",
                "size": "sm",
                "color": "#666666",
                "wrap": True,
            }
        )

    if fulfillment == "delivery":
        fee_label = "ส่งฟรี" if delivery_fee == 0 else f"{delivery_fee} บาท"
        body_contents.append(
            {
                "type": "box",
                "layout": "horizontal",
                "margin": "sm",
                "contents": [
                    {"type": "text", "text": "ค่าอาหาร", "size": "sm", "color": "#666666"},
                    {"type": "text", "text": f"{food_total} บาท", "size": "sm", "align": "end"},
                ],
            }
        )
        body_contents.append(
            {
                "type": "box",
                "layout": "horizontal",
                "contents": [
                    {"type": "text", "text": "ค่าส่ง", "size": "sm", "color": "#666666"},
                    {"type": "text", "text": fee_label, "size": "sm", "align": "end"},
                ],
            }
        )

    body_contents.append(
        {
            "type": "box",
            "layout": "horizontal",
            "margin": "md",
            "contents": [
                {"type": "text", "text": "รวมทั้งหมด", "size": "md", "weight": "bold"},
                {
                    "type": "text",
                    "text": f"{total} บาท",
                    "size": "md",
                    "weight": "bold",
                    "align": "end",
                    "color": "#15803D",
                },
            ],
        }
    )

    return {
        "type": "flex",
        "altText": f"สรุปออเดอร์ รวม {total} บาท (แตะเพื่อยืนยัน)",
        "contents": {
            "type": "bubble",
            "body": {"type": "box", "layout": "vertical", "contents": body_contents},
            "footer": {
                "type": "box",
                "layout": "horizontal",
                "spacing": "sm",
                "contents": [
                    {
                        "type": "button",
                        "style": "primary",
                        "color": "#15803D",
                        "action": {
                            "type": "postback",
                            "label": "ยืนยันออเดอร์",
                            "data": f"action=confirm&card_id={card_id}",
                        },
                    },
                    {
                        "type": "button",
                        "style": "secondary",
                        "action": {
                            "type": "postback",
                            "label": "ยกเลิก",
                            "data": f"action=cancel&card_id={card_id}",
                        },
                    },
                ],
            },
        },
    }


def format_order_receipt(
    order: Order,
    payment_info_text: str,
    *,
    already_confirmed: bool = False,
    wait_minutes: int | None = None,
) -> str:
    header = (
        f"ออเดอร์ #{order.queue_number} ยืนยันไว้แล้วค่ะ"
        if already_confirmed
        else f"รับออเดอร์ #{order.queue_number} แล้วค่ะ"
    )
    lines = [header]
    for item in order.items:
        opts = ", ".join(item.get("option_names_th") or [])
        note = f" ({item['note']})" if item.get("note") else ""
        opt_part = f" [{opts}]" if opts else ""
        lines.append(
            f"• {item['dish_name_th']}{opt_part}{note} ×{item['qty']} = {item['line_total']}฿"
        )
    if order.fulfillment == "delivery":
        fee_text = "ส่งฟรี" if order.delivery_fee == 0 else f"ค่าส่ง {order.delivery_fee} บาท"
        lines.append(f"ส่งที่: {order.address or '-'} ({fee_text})")
    else:
        lines.append("รับเองที่ร้าน")
    if order.requested_time:
        lines.append(f"เวลาที่ขอ: {order.requested_time}")
    lines.append(f"ยอดรวมทั้งหมด {order.total} บาท")
    if wait_minutes and not already_confirmed:
        lines.append(f"ช่วงนี้คิวเยอะ รอประมาณ {wait_minutes} นาทีนะคะ")
    if payment_info_text:
        lines.append("")
        lines.append(payment_info_text)
    return "\n".join(lines)


def format_owner_order_alert(order: Order) -> str:
    mode = f"ส่ง: {order.address}" if order.fulfillment == "delivery" else "รับเองที่ร้าน"
    lines = [f"ออเดอร์ใหม่ #{order.queue_number} · {mode}"]
    for item in order.items:
        opts = ", ".join(item.get("option_names_th") or [])
        note = f" | หมายเหตุ: {item['note']}" if item.get("note") else ""
        opt_part = f" ({opts})" if opts else ""
        lines.append(f"• {item['dish_name_th']}{opt_part}{note} ×{item['qty']}")
    if order.requested_time:
        lines.append(f"เวลาที่ขอ: {order.requested_time}")
    lines.append(f"รวม {order.total} บาท")
    if order.location_url:
        lines.append(f"แผนที่: {order.location_url}")
    return "\n".join(lines)
