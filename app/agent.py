"""Customer ordering LLM agent with deterministic pricing tools, hand-over, and guardrails."""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from typing import Any

import openai

from app.config import Settings
from app.db import Conversation, ShopDB
from app.line import build_order_flex
from app.menu import Fulfillment, Menu, MenuError

log = logging.getLogger("app.agent")

MAX_TOOL_CALLS = 6
TURN_DEADLINE_S = 40.0
MAX_DAILY_TURNS = 30

HANDOVER_REPLY = "ส่งข้อความให้ร้านแล้วค่ะ เดี๋ยวร้านตอบกลับนะคะ"
IMAGE_HANDOVER_REPLY = "ได้รับรูปแล้วค่ะ ส่งให้ร้านดูให้นะคะ"
TURN_LIMIT_REPLY = "ตอนนี้ระบบส่งต่อให้เจ้าของร้านดูแลต่อนะคะ เดี๋ยวร้านตอบกลับเร็ว ๆ นี้ค่ะ"
LIMIT_REPLY = "ขอโทษค่ะ ลองพิมพ์ใหม่อีกครั้งนะคะ"
ERROR_REPLY = "ขอโทษค่ะ ระบบขัดข้องชั่วคราว ลองส่งใหม่อีกครั้งนะคะ"

STATUS_TH = {
    "queued": "กำลังรอทำ",
    "done": "ทำเสร็จแล้ว",
    "cancelled": "ยกเลิกแล้ว",
}

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_shop_status",
            "description": (
                "ดูสถานะร้าน (เปิด/ปิด เวลาเปิด โหมดยุ่งและเวลารอ ของหมด กติกาค่าส่ง "
                "และที่อยู่จัดส่งเดิมของลูกค้าคนนี้)"
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_cart",
            "description": (
                "เพิ่ม แทนที่ ลบ หรือเคลียร์รายการในตะกร้า โค้ดเป็นผู้คำนวณราคาทั้งหมด "
                "ระดับความเผ็ดและระดับความหวานมีค่าเริ่มต้นเป็นเผ็ดปกติ/หวานปกติ ไม่ต้องถามถ้าลูกค้าไม่ระบุ"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["add", "replace", "remove", "clear"],
                    },
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "dish_id": {"type": "string"},
                                "qty": {"type": "integer", "minimum": 1, "maximum": 20},
                                "option_ids": {"type": "array", "items": {"type": "string"}},
                                "note": {"type": "string"},
                            },
                            "required": ["dish_id", "qty"],
                            "additionalProperties": False,
                        },
                    },
                    "remove_index": {"type": "integer", "minimum": 0},
                },
                "required": ["action"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_fulfillment",
            "description": (
                "ตั้งวิธีรับอาหารเป็น pickup (รับเองที่ร้าน) หรือ delivery (ส่ง) พร้อมที่อยู่และเวลาที่ขอ "
                "ถ้าลูกค้าเคยสั่งส่งมาก่อน สามารถใช้ที่อยู่เดิมได้โดยไม่ต้องให้ลูกค้าพิมพ์ใหม่"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "fulfillment": {"type": "string", "enum": ["pickup", "delivery"]},
                    "address": {"type": "string"},
                    "requested_time": {"type": "string"},
                    "use_saved_address": {"type": "boolean"},
                },
                "required": ["fulfillment"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "build_order_card",
            "description": (
                "สร้างการ์ดสรุปออเดอร์ (LINE Flex) พร้อมปุ่มยืนยันเมื่อมีรายการและวิธีรับอาหารครบแล้ว"
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_my_orders",
            "description": "ดูรายการออเดอร์วันนี้ของลูกค้าคนนี้ พร้อมสถานะและเหตุผลการยกเลิก",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_order",
            "description": "ยกเลิกออเดอร์ของลูกค้าที่ยังอยู่ในสถานะ queued เท่านั้น",
            "parameters": {
                "type": "object",
                "properties": {"order_id": {"type": "integer"}},
                "required": ["order_id"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hand_over",
            "description": (
                "ส่งต่อข้อความให้เจ้าของร้านตอบเอง ใช้เมื่อเป็นคำทักทายล้วน คุยเล่น ขอแก้ไขออเดอร์ที่ยืนยันแล้ว "
                "ถามวัตถุดิบ/การแพ้อาหาร หรือขอเมนู/วัตถุดิบที่ไม่มีในเมนูบอท"
            ),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
]

HANDED_OVER_NOTE = (
    "ตอนนี้บทสนทนานี้ส่งต่อให้เจ้าของร้านดูแลอยู่ และคุณไม่เห็นข้อความที่เจ้าของร้านตอบในแชท "
    "ถ้าข้อความล่าสุดเป็นคำขอบคุณ การตอบกลับร้าน เช่น 'ขอบคุณค่ะ' 'ได้ค่ะ' หรือไม่ใช่การสั่งอาหารใหม่ "
    "ถามเมนู ถามเปิดร้าน/ของหมด ถามสถานะออเดอร์ หรือขอยกเลิกออเดอร์ที่รอทำ "
    "ห้ามเรียก get_shop_status หรือเครื่องมือใดๆ และไม่ต้องตอบ (ระบบจะเงียบไว้) "
    "ถ้าใช่ ให้เรียกเครื่องมือที่ตรงเรื่องก่อนตอบเสมอ"
)


@dataclass(frozen=True)
class AgentTurn:
    reply_text: str | None
    flex_card: dict[str, Any] | None = None
    reason: str = "ok"


def make_openai_client(settings: Settings) -> openai.OpenAI:
    return openai.OpenAI(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key or "missing",
        max_retries=0,
    )


def _system_prompt(menu: Menu, overrides: dict[tuple[str, str], bool]) -> dict[str, Any]:
    rules = (
        f'คุณคือผู้ช่วยสั่งอาหารทาง LINE ของร้านอาหารตามสั่ง "{menu.shop.name}" '
        "เข้าใจภาษาไทยไม่เป็นทางการ คำย่อ และพิมพ์ผิด\n"
        "กฎสำคัญ:\n"
        "1. ตอบภาษาไทยสั้น สุภาพ ลงท้ายค่ะ/นะคะ ใช้ข้อความธรรมดา (ห้ามใช้ Markdown เช่น ** หรือ #)\n"
        "2. ใช้เฉพาะ dish_id และ option_ids จากเมนู JSON ห้ามเดาราคา คิดเลขเอง หรือให้ส่วนลด "
        "ยอดรวมและค่าส่งต้องมาจากผลเครื่องมือเท่านั้น ยึดสถานะ 'ของหมดตอนนี้' และ 'ตะกร้าปัจจุบัน' "
        "จากข้อมูลระบบล่าสุดเสมอ ห้ามอ้างอิงข้อความเก่าในแชทว่าของยังหมดหรือยังอยู่ในตะกร้า "
        "(ตอบตามสถานะปัจจุบันอย่างเป็นธรรมชาติ ไม่ต้องอธิบายว่าข้อความเก่าไม่ใช่สถานะปัจจุบัน)\n"
        "3. ค่าเริ่มต้นอัตโนมัติ: ความเผ็ดเป็น 'เผ็ดปกติ' และความหวานเป็น 'หวานปกติ' "
        "ห้ามถามระดับความเผ็ด ความหวาน หรือเสนอเพิ่มไข่/ท็อปปิ้งถ้าลูกค้าไม่ได้ขอ "
        "ถามเฉพาะสิ่งที่กำกวมจริง เช่น เนื้อสัตว์ที่ต้องเลือก "
        "(เช่น กะเพราหมู -> หมูสับหรือหมูชิ้น), สุกี้ (แห้งหรือน้ำ), หรือไข่ดาว (สุกหรือไม่สุก) "
        "ถ้าจานนั้นยังขาดตัวเลือกที่ต้องถาม ให้ถามพร้อมวิธีรับอาหารในข้อความเดียวก่อนเรียก update_cart "
        "(อย่าเพิ่มจานที่ยังไม่ครบลงตะกร้าก่อน)\n"
        "4. 'พิเศษ' ให้ใส่ option_ids=['special'] สำหรับจานหลักได้เลยโดยไม่ต้องถามเพิ่ม "
        "คำขอปรับแต่งบนเมนูที่มี เช่น ไม่ใส่ผัก แยกน้ำ ให้เก็บใน note "
        "ถ้าลูกค้าขอเมนู/เส้น/เนื้อสัตว์ที่ไม่มีในเมนู หรือถามเรื่องส่วนผสม/แพ้อาหาร "
        "ห้ามเดาหรือรับรองความปลอดภัย ให้แจ้งว่าไม่มีในเมนูบอทและเรียก hand_over\n"
        "5. ร้านรับทั้งรับเองที่ร้าน (pickup) และส่ง (delivery) "
        f"กติกาค่าส่ง: {menu.shop.delivery.rule_text_th()} "
        "ถ้ายังไม่รู้ว่ารับเองหรือให้ส่ง ให้ถามครั้งเดียวก่อนเรียก build_order_card "
        "ถ้าลูกค้าเลือกส่งและมีที่อยู่เดิมในระบบ ให้ใช้ที่อยู่เดิมและแจ้งครั้งเดียวว่า "
        "'ส่งที่เดิม (...) นะคะ ถ้าเปลี่ยนที่อยู่บอกได้เลยค่ะ'\n"
        "6. เมื่อรายการและวิธีรับอาหารครบแล้ว ให้เรียก build_order_card เพื่อให้ระบบส่งการ์ดยืนยัน "
        "เมื่อ build_order_card สำเร็จ ระบบจะส่งการ์ดสรุปเต็มรูปแบบให้ลูกค้าเอง "
        "ให้ตอบข้อความสั้นเพียง 1 บรรทัด (เช่น 'ตรวจสอบรายการบนการ์ดแล้วกดยืนยันได้เลยนะคะ') "
        "ไม่ต้องทวนรายการอาหารหรือราคาซ้ำในข้อความ ห้ามอ้างว่าสั่งสำเร็จจนกว่าลูกค้าจะกดปุ่มยืนยันบนการ์ด "
        "ถ้าร้านปิดให้แจ้งเวลาเปิดปกติและไม่รับออเดอร์\n"
        "7. ถ้าระบบระบุว่าร้านอยู่ในโหมดยุ่ง ให้แจ้งเวลารอให้ลูกค้าทราบตอนรับออเดอร์ด้วย\n"
        "8. ถามสถานะออเดอร์เรียก get_my_orders ขอยกเลิกออเดอร์ที่ยัง queued เรียก cancel_order "
        "แต่ถ้าขอแก้ไขออเดอร์ที่ยืนยันไปแล้ว ทักทายล้วนๆ ('สวัสดีค่ะ') หรือคุยเล่น ให้เรียก hand_over อย่างเดียว\n"
        "9. ออเดอร์ที่ลูกค้ากดยืนยันไปแล้วถือว่าเสร็จสิ้นและระบบเคลียร์ตะกร้าให้ใหม่แล้ว "
        "ห้ามนำรายการหรือยอดของออเดอร์เก่ามารวมกับตะกร้าใหม่ และห้ามทักว่ารายการเก่าหายไปจากตะกร้า"
    )
    menu_json = json.dumps(
        menu.to_prompt_dict(overrides), ensure_ascii=False, separators=(",", ":")
    )
    text = f"{rules}\n\nเมนูวันนี้ (JSON):\n{menu_json}"
    return {
        "role": "system",
        "content": [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}],
    }


def build_card_snapshot_for_conv(
    db: ShopDB, conv: Conversation, menu: Menu
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate draft & shop open status, build priced card snapshot, and save as active card."""
    status = db.get_shop_status(menu)
    if not status.is_open:
        return None, f"ตอนนี้ร้านปิดรับออเดอร์แล้วค่ะ (ปกติเปิด {status.hours_text})"

    draft = conv.draft
    raw_items: list[dict[str, Any]] = list(draft.get("items") or [])
    if not raw_items:
        return None, "ยังไม่มีรายการอาหารในตะกร้าค่ะ"

    fulfillment_raw = draft.get("fulfillment")
    if fulfillment_raw not in ("pickup", "delivery"):
        return None, "กรุณาเลือกว่าจะรับเองที่ร้านหรือให้จัดส่งก่อนค่ะ"
    fulfillment: Fulfillment = "delivery" if fulfillment_raw == "delivery" else "pickup"

    address = draft.get("address")
    location_url = draft.get("location_url")
    if fulfillment == "delivery" and not (address and str(address).strip()):
        return None, "กรุณาแจ้งที่อยู่จัดส่งหรือส่งพิกัดก่อนค่ะ"

    overrides = db.get_overrides()
    try:
        priced = menu.price_cart(raw_items, fulfillment, overrides)
    except MenuError as exc:
        return None, str(exc)

    card_id = uuid.uuid4().hex[:12]
    snapshot: dict[str, Any] = {
        "card_id": card_id,
        "draft_revision": conv.draft_revision,
        "raw_items": raw_items,
        "priced_lines": [line.to_dict() for line in priced.lines],
        "fulfillment": fulfillment,
        "address": str(address).strip() if address else None,
        "location_url": str(location_url).strip() if location_url else None,
        "requested_time": draft.get("requested_time"),
        "food_total": priced.food_total,
        "delivery_fee": priced.delivery_fee,
        "total": priced.total,
    }
    db.set_active_card(conv.id, card_id, snapshot)
    return snapshot, None


_PLUS_CODE_RE = re.compile(r"^[A-Z0-9]{4,8}\+[A-Z0-9]{2,4}\s+")


def clean_location_address(raw_address: str, latitude: float, longitude: float) -> str:
    """Strip leading Google Plus Codes (e.g. 'XGV6+R86 ') from LINE location addresses."""
    cleaned = _PLUS_CODE_RE.sub("", raw_address.strip()).strip()
    return cleaned or f"พิกัด {latitude:.5f}, {longitude:.5f}"


def handle_location_message(
    db: ShopDB,
    menu: Menu,
    line_user_id: str,
    address: str,
    latitude: float,
    longitude: float,
) -> AgentTurn:
    """Save a LINE location pin as delivery address (+ Maps URL), building a card if cart ready."""
    status = db.get_shop_status(menu)
    if not status.is_open:
        return AgentTurn(
            reply_text=f"ตอนนี้ร้านปิดรับออเดอร์แล้วค่ะ (ปกติเปิด {status.hours_text})",
            reason="closed",
        )

    conv = db.get_or_create_conversation(line_user_id)
    if db.is_handed_over(conv):
        db.clear_handed_over(conv.id)

    loc_url = f"https://maps.google.com/?q={latitude},{longitude}"
    clean_addr = clean_location_address(address, latitude, longitude)
    draft = dict(conv.draft)
    draft["fulfillment"] = "delivery"
    draft["address"] = clean_addr
    draft["location_url"] = loc_url
    db.save_draft(conv.id, draft)
    conv = db.get_or_create_conversation(line_user_id)

    if draft.get("items"):
        snapshot, err = build_card_snapshot_for_conv(db, conv, menu)
        if snapshot is not None:
            flex = build_order_flex(
                snapshot,
                shop_name=menu.shop.name,
                wait_minutes=status.wait_minutes if status.busy_mode else None,
            )
            reply = f"บันทึกพิกัดจัดส่ง ({clean_addr}) แล้วค่ะ ตรวจสอบรายการบนการ์ดแล้วกดยืนยันได้เลยนะคะ"
            db.append_conversation_event(
                line_user_id, f"[ลูกค้าส่งพิกัดจัดส่ง: {clean_addr}]", reply
            )
            return AgentTurn(reply_text=reply, flex_card=flex, reason="ok")
        reply_err = err or f"บันทึกพิกัดจัดส่ง ({clean_addr}) แล้วค่ะ"
        db.append_conversation_event(
            line_user_id, f"[ลูกค้าส่งพิกัดจัดส่ง: {clean_addr}]", reply_err
        )
        return AgentTurn(reply_text=reply_err, reason="ok")

    reply_empty = f"บันทึกพิกัดจัดส่ง ({clean_addr}) แล้วค่ะ ต้องการสั่งเมนูอะไรดีคะ?"
    db.append_conversation_event(
        line_user_id, f"[ลูกค้าส่งพิกัดจัดส่ง: {clean_addr}]", reply_empty
    )
    return AgentTurn(reply_text=reply_empty, reason="ok")


def handle_image_message(db: ShopDB, line_user_id: str) -> AgentTurn:
    """Skip LLM for images (usually payment slips); hand over once, stay silent if already."""
    conv = db.get_or_create_conversation(line_user_id)
    if db.is_handed_over(conv):
        return AgentTurn(reply_text=None, reason="silent")
    db.set_handed_over(conv.id)
    db.append_conversation_event(line_user_id, "[ลูกค้าส่งรูปภาพ]", IMAGE_HANDOVER_REPLY)
    return AgentTurn(reply_text=IMAGE_HANDOVER_REPLY, reason="hand_over")


def handle_customer_text(
    db: ShopDB,
    menu: Menu,
    line_user_id: str,
    text: str,
    *,
    llm_client: Any,
    settings: Settings,
    started_at: float | None = None,
) -> AgentTurn:
    """Run one customer turn with tool execution, hand-over rules, and safety guardrails."""
    t_start = time.monotonic() if started_at is None else started_at
    status = db.get_shop_status(menu)
    if not status.bot_enabled:
        return AgentTurn(reply_text=None, reason="bot_disabled")

    conv = db.get_or_create_conversation(line_user_id)
    handed_over = db.is_handed_over(conv)

    user_turns = sum(1 for m in conv.messages if m.get("role") == "user")
    if user_turns >= MAX_DAILY_TURNS:
        if handed_over:
            return AgentTurn(reply_text=None, reason="silent")
        db.set_handed_over(conv.id)
        return AgentTurn(reply_text=TURN_LIMIT_REPLY, reason="turn_limit")

    overrides = db.get_overrides()
    system_msgs: list[dict[str, Any]] = [_system_prompt(menu, overrides)]

    # Dynamic live-state note (separate from cached menu prefix) so the LLM never trusts stale chat
    sold_parts = [*status.sold_out_ingredients, *status.sold_out_dishes]
    sold_text = ", ".join(sold_parts) if sold_parts else "ไม่มี (ทุกเมนูและวัตถุดิบพร้อมขาย)"
    draft_items = list(conv.draft.get("items") or [])
    cart_text = f"มี {len(draft_items)} รายการ" if draft_items else "ว่าง (0 รายการ)"
    busy_text = (
        f"โหมดยุ่ง รอประมาณ {status.wait_minutes} นาที"
        if status.busy_mode and status.wait_minutes
        else "โหมดปกติ"
    )
    open_text = "เปิดรับออเดอร์" if status.is_open else f"ปิดร้าน (ปกติเปิด {status.hours_text})"
    system_msgs.append(
        {
            "role": "system",
            "content": (
                f"สถานะระบบล่าสุดตอนนี้ (ยึดข้อมูลนี้เหนือข้อความเก่าในแชท): "
                f"ร้าน={open_text} | {busy_text} | ของหมดตอนนี้={sold_text} | ตะกร้าปัจจุบัน={cart_text}"
            ),
        }
    )
    if handed_over:
        system_msgs.append({"role": "system", "content": HANDED_OVER_NOTE})

    user_msg = {"role": "user", "content": text}
    messages: list[dict[str, Any]] = [*conv.messages, user_msg]

    tool_calls_made = 0
    non_handover_tool_called = False
    built_flex: dict[str, Any] | None = None

    extra_body: dict[str, Any] = {}
    if settings.llm_reasoning_effort:
        extra_body["reasoning"] = {"effort": settings.llm_reasoning_effort}

    def _run_tool(name: str, args: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """Execute a tool; returns (result_payload, should_hand_over_now)."""
        nonlocal conv, built_flex, non_handover_tool_called
        if name == "hand_over":
            return {"ok": True}, True

        non_handover_tool_called = True
        if handed_over:
            db.clear_handed_over(conv.id)

        if name == "get_shop_status":
            st = db.get_shop_status(menu)
            cust = db.get_customer(line_user_id)
            return {
                "is_open": st.is_open,
                "hours": st.hours_text,
                "busy_mode": st.busy_mode,
                "wait_minutes": st.wait_minutes,
                "delivery_rule": st.delivery_rule_text,
                "sold_out_ingredients": st.sold_out_ingredients,
                "sold_out_dishes": st.sold_out_dishes,
                "saved_address": cust.last_address if cust else None,
                "has_saved_location_pin": bool(cust and cust.last_location_url),
            }, False

        if name == "update_cart":
            action = str(args.get("action", ""))
            st = db.get_shop_status(menu)
            if not st.is_open and action in ("add", "replace"):
                return {
                    "ok": False,
                    "error": f"ร้านปิดรับออเดอร์แล้วค่ะ (ปกติเปิด {st.hours_text})",
                }, False

            draft = dict(conv.draft)
            items: list[dict[str, Any]] = list(draft.get("items") or [])
            if action == "clear":
                items = []
            elif action == "remove":
                raw_idx = args.get("remove_index")
                if not isinstance(raw_idx, int) or isinstance(raw_idx, bool):
                    return {"ok": False, "error": "ลำดับรายการไม่ถูกต้องค่ะ"}, False
                if 0 <= raw_idx < len(items):
                    items.pop(raw_idx)
                else:
                    return {"ok": False, "error": "ไม่พบลำดับรายการที่ต้องการลบค่ะ"}, False
            elif action in ("add", "replace"):
                incoming_raw = args.get("items")
                if not isinstance(incoming_raw, list) or not incoming_raw:
                    return {"ok": False, "error": "ยังไม่มีรายการอาหารที่จะเพิ่มค่ะ"}, False
                normalized: list[dict[str, Any]] = []
                ov = db.get_overrides()
                try:
                    for raw_line in incoming_raw:
                        if not isinstance(raw_line, dict) or not raw_line.get("dish_id"):
                            raise MenuError("ข้อมูลรายการอาหารไม่ครบค่ะ")
                        raw_qty = raw_line.get("qty", 1)
                        if isinstance(raw_qty, bool) or not isinstance(raw_qty, int):
                            raise MenuError("จำนวนต้องเป็นตัวเลขจำนวนเต็มค่ะ")
                        raw_opts = raw_line.get("option_ids") or []
                        if not isinstance(raw_opts, list):
                            raise MenuError("รูปแบบตัวเลือกไม่ถูกต้องค่ะ")
                        opt_ids = [str(o) for o in raw_opts if o is not None]
                        note_str = str(raw_line.get("note") or "")
                        menu.price_line(
                            dish_id=str(raw_line["dish_id"]),
                            qty=raw_qty,
                            option_ids=opt_ids,
                            note=note_str,
                            overrides=ov,
                        )
                        normalized.append(
                            {
                                "dish_id": str(raw_line["dish_id"]),
                                "qty": raw_qty,
                                "option_ids": opt_ids,
                                "note": note_str,
                            }
                        )
                except MenuError as exc:
                    return {"ok": False, "error": str(exc)}, False
                items = normalized if action == "replace" else [*items, *normalized]
            else:
                return {"ok": False, "error": "คำสั่งตะกร้าไม่ถูกต้อง"}, False

            draft["items"] = items
            db.save_draft(conv.id, draft)
            conv = db.get_or_create_conversation(line_user_id)

            if not items:
                return {"ok": True, "items": [], "food_total": 0}, False
            ff: Fulfillment = "delivery" if draft.get("fulfillment") == "delivery" else "pickup"
            priced = menu.price_cart(items, ff, db.get_overrides())
            cust = db.get_customer(line_user_id)
            return {
                "ok": True,
                "items": [line.to_dict() for line in priced.lines],
                "food_total": priced.food_total,
                "fulfillment": draft.get("fulfillment"),
                "address": draft.get("address"),
                "saved_address": cust.last_address if cust else None,
            }, False

        if name == "set_fulfillment":
            st = db.get_shop_status(menu)
            if not st.is_open:
                return {
                    "ok": False,
                    "error": f"ร้านปิดรับออเดอร์แล้วค่ะ (ปกติเปิด {st.hours_text})",
                }, False
            ff_val = str(args.get("fulfillment", ""))
            if ff_val not in ("pickup", "delivery"):
                return {"ok": False, "error": "กรุณาเลือก pickup หรือ delivery"}, False

            draft = dict(conv.draft)
            draft["fulfillment"] = ff_val
            used_saved = False
            if ff_val == "pickup":
                draft["address"] = None
                draft["location_url"] = None
            else:
                addr_arg = str(args.get("address") or "").strip()
                cust = db.get_customer(line_user_id)
                current_addr = str(draft.get("address") or "").strip()
                if addr_arg:
                    if addr_arg == current_addr:
                        # Preserve existing location pin when LLM echoes the same address text
                        draft["address"] = addr_arg
                    elif cust and cust.last_address and addr_arg == cust.last_address.strip():
                        draft["address"] = cust.last_address
                        draft["location_url"] = cust.last_location_url
                        used_saved = True
                    else:
                        draft["address"] = addr_arg
                        draft["location_url"] = None
                elif (args.get("use_saved_address") or not current_addr) and (
                    cust and cust.last_address
                ):
                    draft["address"] = cust.last_address
                    draft["location_url"] = cust.last_location_url
                    used_saved = True
                elif not current_addr:
                    draft["fulfillment"] = "delivery"
                    db.save_draft(conv.id, draft)
                    conv = db.get_or_create_conversation(line_user_id)
                    return {
                        "ok": False,
                        "fulfillment": "delivery",
                        "error": (
                            "ลูกค้าเลือกให้จัดส่งแล้ว แต่ยังไม่มีที่อยู่จัดส่ง "
                            "กรุณาขอเฉพาะที่อยู่หรือให้ส่งพิกัด (ไม่ต้องถามว่ารับเองหรือส่งอีก)"
                        ),
                    }, False

            if "requested_time" in args and args["requested_time"] is not None:
                draft["requested_time"] = str(args["requested_time"]).strip() or None

            db.save_draft(conv.id, draft)
            conv = db.get_or_create_conversation(line_user_id)
            return {
                "ok": True,
                "fulfillment": draft["fulfillment"],
                "address": draft.get("address"),
                "used_saved_address": used_saved,
                "requested_time": draft.get("requested_time"),
            }, False

        if name == "build_order_card":
            conv = db.get_or_create_conversation(line_user_id)
            snapshot, err = build_card_snapshot_for_conv(db, conv, menu)
            if snapshot is None:
                return {"ok": False, "error": err or "สร้างสรุปออเดอร์ไม่สำเร็จค่ะ"}, False
            st = db.get_shop_status(menu)
            built_flex = build_order_flex(
                snapshot,
                shop_name=menu.shop.name,
                wait_minutes=st.wait_minutes if st.busy_mode else None,
            )
            return {
                "ok": True,
                "card_id": snapshot["card_id"],
                "food_total": snapshot["food_total"],
                "delivery_fee": snapshot["delivery_fee"],
                "total": snapshot["total"],
                "wait_minutes": st.wait_minutes if st.busy_mode else None,
            }, False

        if name == "get_my_orders":
            orders = db.get_customer_orders_today(line_user_id)
            return {
                "orders": [
                    {
                        "order_id": o.id,
                        "queue_number": o.queue_number,
                        "status": o.status,
                        "status_th": STATUS_TH.get(o.status, o.status),
                        "total": o.total,
                        "fulfillment": o.fulfillment,
                        "cancel_reason": o.cancel_reason,
                        "items": o.items,
                    }
                    for o in orders
                ]
            }, False

        if name == "cancel_order":
            raw_oid = args.get("order_id")
            if isinstance(raw_oid, bool) or not isinstance(raw_oid, int):
                return {"ok": False, "error": "เลขออเดอร์ไม่ถูกต้องค่ะ"}, False
            cancelled = db.cancel_customer_order(line_user_id, raw_oid)
            if cancelled is None:
                return {
                    "ok": False,
                    "error": "ยกเลิกไม่ได้ค่ะ ออเดอร์อาจเสร็จแล้ว ถูกยกเลิกแล้ว หรือไม่ใช่ของลูกค้า",
                }, False
            return {
                "ok": True,
                "queue_number": cancelled.queue_number,
                "status": cancelled.status,
            }, False

        return {"ok": False, "error": f"unknown tool: {name}"}, False

    while True:
        remaining = TURN_DEADLINE_S - (time.monotonic() - t_start)
        if remaining <= 0:
            if handed_over and not non_handover_tool_called:
                db.save_conversation_messages(conv.id, [*conv.messages, user_msg])
                return AgentTurn(reply_text=None, reason="silent")
            db.save_conversation_messages(
                conv.id, [*messages, {"role": "assistant", "content": LIMIT_REPLY}]
            )
            return AgentTurn(reply_text=LIMIT_REPLY, reason="deadline")

        try:
            kwargs: dict[str, Any] = {
                "model": settings.llm_model,
                "messages": system_msgs + messages,
                "tools": TOOLS,
                "max_tokens": 1200,
                "timeout": remaining,
            }
            if extra_body:
                kwargs["extra_body"] = extra_body
            resp = llm_client.chat.completions.create(**kwargs)
        except Exception as exc:
            log.warning("LLM call failed: %s", type(exc).__name__)
            if handed_over and not non_handover_tool_called:
                db.save_conversation_messages(conv.id, [*conv.messages, user_msg])
                return AgentTurn(reply_text=None, reason="silent")
            return AgentTurn(reply_text=ERROR_REPLY, reason="error")

        msg = resp.choices[0].message
        tool_calls = msg.tool_calls or []

        if not tool_calls:
            reply_content = (msg.content or "").strip()
            if handed_over and not non_handover_tool_called:
                db.save_conversation_messages(conv.id, [*conv.messages, user_msg])
                return AgentTurn(reply_text=None, reason="silent")
            if not reply_content and built_flex is None:
                reply_content = LIMIT_REPLY
            db.save_conversation_messages(
                conv.id, [*messages, {"role": "assistant", "content": reply_content}]
            )
            return AgentTurn(
                reply_text=reply_content or None,
                flex_card=built_flex,
                reason="ok",
            )

        if tool_calls_made + len(tool_calls) > MAX_TOOL_CALLS:
            if handed_over and not non_handover_tool_called:
                db.save_conversation_messages(conv.id, [*conv.messages, user_msg])
                return AgentTurn(reply_text=None, reason="silent")
            db.save_conversation_messages(
                conv.id, [*messages, {"role": "assistant", "content": LIMIT_REPLY}]
            )
            return AgentTurn(reply_text=LIMIT_REPLY, reason="max_tool_calls")

        assistant_tool_msg: dict[str, Any] = {
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in tool_calls
            ],
        }
        tool_results: list[dict[str, Any]] = []
        handed_over_now = False

        for tc in tool_calls:
            tool_calls_made += 1
            try:
                parsed_args = json.loads(tc.function.arguments or "{}")
                if not isinstance(parsed_args, dict):
                    parsed_args = {}
            except json.JSONDecodeError:
                parsed_args = {}
            try:
                result, trigger_handover = _run_tool(tc.function.name, parsed_args)
            except Exception as exc:
                log.warning("Tool %s failed: %s", tc.function.name, type(exc).__name__)
                result, trigger_handover = (
                    {"ok": False, "error": "ข้อมูลคำสั่งไม่ถูกต้อง กรุณาลองใหม่ค่ะ"},
                    False,
                )
            if trigger_handover:
                handed_over_now = True
            tool_results.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, ensure_ascii=False),
                }
            )

        if handed_over_now:
            if handed_over:
                db.save_conversation_messages(conv.id, [*conv.messages, user_msg])
                return AgentTurn(reply_text=None, reason="silent")
            db.set_handed_over(conv.id)
            db.save_conversation_messages(
                conv.id,
                [*conv.messages, user_msg, {"role": "assistant", "content": HANDOVER_REPLY}],
            )
            return AgentTurn(reply_text=HANDOVER_REPLY, reason="hand_over")

        messages.append(assistant_tool_msg)
        messages.extend(tool_results)
