"""Customer ordering LLM agent with deterministic pricing tools, hand-over, and guardrails."""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import openai

from app.config import Settings
from app.db import Conversation, ShopDB, customer_turn_count
from app.handover import LLM_HANDOVER_REASONS
from app.line import build_order_flex
from app.menu import Fulfillment, Menu, MenuError
from app.promptpay import qr_image_url
from app.wait import card_wait_line, one_box_minutes, queue_info, range_text

log = logging.getLogger("app.agent")

MAX_TOOL_CALLS = 6
TURN_DEADLINE_S = 40.0
MAX_DAILY_TURNS = 30

HANDOVER_REPLY = "ส่งข้อความให้ร้านแล้วค่ะ เดี๋ยวร้านตอบกลับนะคะ"
IMAGE_HANDOVER_REPLY = "ได้รับรูปแล้วค่ะ ส่งให้ร้านดูให้นะคะ"
TURN_LIMIT_REPLY = "ตอนนี้ระบบส่งต่อให้เจ้าของร้านดูแลต่อนะคะ เดี๋ยวร้านตอบกลับเร็ว ๆ นี้ค่ะ"
LIMIT_REPLY = "ขอโทษค่ะ ลองพิมพ์ใหม่อีกครั้งนะคะ"
VOICE_REPLY = "รบกวนพิมพ์เป็นข้อความนะคะ 🙏"
ERROR_REPLY = "ขอโทษค่ะ ระบบขัดข้องชั่วคราว ลองส่งใหม่อีกครั้งนะคะ"
# Tool result when the LLM's arguments are not a JSON object: the tool is not run
BAD_ARGS_ERROR = "รูปแบบคำสั่งไม่ถูกต้อง กรุณาลองใหม่ค่ะ"
CARD_CLEARED_NOTE = "การ์ดที่สร้างไว้ก่อนหน้าถูกยกเลิกแล้วเพราะตะกร้าเปลี่ยน ต้องเรียก build_order_card ใหม่"
CARD_AFTER_FAILED_EDIT = "ยังไม่สร้างการ์ด เพราะคำสั่งแก้ตะกร้าหรือวิธีรับก่อนหน้านี้ไม่สำเร็จ แก้ให้ครบก่อนค่ะ"

# Card fast path (docs/specs/2026-11-26-card-fast-path.md): a one-message order that builds its
# card cleanly gets one of these lines instead of a second LLM call
CARD_LINE_PICKUP = "ตรวจสอบรายการบนการ์ดแล้วกดยืนยันได้เลยนะคะ"
CARD_LINE_SAVED_ADDRESS = "ส่งที่เดิมนะคะ ถ้าเปลี่ยนที่อยู่บอกได้เลยค่ะ\n" + CARD_LINE_PICKUP
CARD_LINE_NEW_ADDRESS = "ตรวจสอบรายการและที่อยู่บนการ์ดแล้วกดยืนยันได้เลยนะคะ"
_CARD_TOOLS = frozenset({"update_cart", "set_fulfillment", "build_order_card"})
# A side question the LLM could answer without a tool: keep the LLM reply for it.
# Kept until real traffic shows it never catches anything (docs/adr/0002, revisit rule)
_QUESTION_RE = re.compile(
    r"ไหม|มั้ย|มั๊ย|ไม๊|กี่|อะไร|ยังไง|อย่างไร|เท่าไหร่|เท่าไร|ไหน|เมื่อไหร่|ทำไม|รึเปล่า|หรือเปล่า|\?"
)
# A request for shop information with no question word ("ขอที่อยู่ร้านหน่อยค่ะ"): a request verb
# shortly before an information noun. Polite orders ("ขอกะเพรา 1") don't match
# (docs/specs/2026-11-27-fast-path-request-check.md)
_INFO_REQUEST_RE = re.compile(
    r"(?:ขอ|บอก|แจ้ง|รบกวน|ส่ง)\S{0,8}?"
    r"(?:ที่อยู่ร้าน|ที่ตั้ง|โลเคชั่น|โลเคชัน|โลเค|พิกัด|แผนที่|ทางไป|เบอร์|เลขบัญชี|บัญชี|คิวอาร์|qr|QR|เวลา|เมนู)"
)


def has_side_question(text: str) -> bool:
    """A question word or an information request: the fast path keeps the LLM reply for it."""
    return bool(_QUESTION_RE.search(text) or _INFO_REQUEST_RE.search(text))


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
                "ดูสถานะร้าน (เปิด/ปิด เวลาเปิด โหมดยุ่งและเวลารอ ของหมด กติกาค่าส่ง และที่อยู่จัดส่งเดิมของลูกค้าคนนี้)"
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
            "description": ("สร้างการ์ดสรุปออเดอร์ (LINE Flex) พร้อมปุ่มยืนยันเมื่อมีรายการและวิธีรับอาหารครบแล้ว"),
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "show_menu",
            "description": (
                "ส่งรายการเมนูวันนี้ทั้งหมด (ราคา และไม่รวมของหมด) ให้ลูกค้า ระบบส่งเองเป็นข้อความแยก "
                "ใช้เมื่อลูกค้าถามว่ามีเมนูอะไรบ้างหรือขอดูเมนู"
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
            "name": "get_payment_info",
            "description": (
                "ดูยอดค้างชำระทั้งหมดของลูกค้า (ทุกวัน) และส่ง QR พร้อมเพย์ยอดตรงให้ลูกค้า "
                "ใช้เมื่อลูกค้าขอ QR ถามวิธีจ่าย หรือถามยอดค้าง ยอดเงินมาจากระบบเท่านั้น ห้ามคำนวณเอง"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "send_qr": {
                        "type": "boolean",
                        "description": "true เมื่อลูกค้าขอ QR เองโดยตรง",
                    }
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_payment_method",
            "description": (
                "บันทึกวิธีจ่ายของลูกค้า ค่าเริ่มต้นคือโอนตาม QR ใช้ method=cash เมื่อลูกค้าบอกว่าจะจ่ายเงินสด "
                "(ระบบจะไม่ส่ง QR ให้) ใช้ method=transfer เมื่อลูกค้าเปลี่ยนใจจะโอน"
            ),
            "parameters": {
                "type": "object",
                "properties": {"method": {"type": "string", "enum": ["cash", "transfer"]}},
                "required": ["method"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_order",
            "description": (
                "ยกเลิกออเดอร์ของลูกค้าที่ยังอยู่ในสถานะ queued และยังไม่ได้จ่ายเงินเท่านั้น "
                "ถ้าจ่ายแล้วระบบจะส่งต่อให้ร้านเอง"
            ),
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
                "ส่งต่อข้อความให้เจ้าของร้านตอบเอง ใช้เมื่อ "
                "ขอแก้ไขออเดอร์ที่ยืนยันแล้ว (reason=edit_confirmed พร้อม queue_number ถ้ารู้) "
                "ถามส่วนผสม/การแพ้อาหาร หรือคำขอที่บอทตอบเองไม่ได้ (reason=off_menu) "
                "แต่ถ้าแค่ขอเมนูที่ไม่มี ให้ตอบเองว่าไม่มีและแนะนำเมนูที่มี ไม่ต้องส่งต่อ"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string", "enum": list(LLM_HANDOVER_REASONS)},
                    "queue_number": {"type": "integer", "minimum": 1},
                },
                "required": ["reason"],
                "additionalProperties": False,
            },
        },
    },
]

# Exact note for the first LLM call after a hand-over ends (SPEC §3.2.6).
RESUME_NOTE = "ร้านคุยกับลูกค้าเรื่องก่อนหน้านี้แล้ว บอทไม่เห็นข้อความของร้าน อย่าตอบคำถามเดิมซ้ำ ให้ช่วยเฉพาะเรื่องใหม่"


@dataclass(frozen=True)
class AgentTurn:
    reply_text: str | None
    flex_card: dict[str, Any] | None = None
    reason: str = "ok"
    # Set when this turn opened a new inbox card (caller fetches the profile / alerts the owner)
    new_card_reason: str | None = None
    new_card_detail: str | None = None
    # PromptPay QR image sent after the text (get_payment_info)
    image_url: str | None = None
    # Full menu list built in code (show_menu), sent before the reply text
    menu_text: str | None = None
    # The order card in `flex_card`; if LINE fails to deliver it, the caller cancels it
    card_id: str | None = None


@dataclass
class TurnStats:
    """One webhook event's PII-free telemetry, filled in while it runs (SPEC polish §3.4)."""

    turn_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    llm_calls: int = 0
    reason: str = "ignored"
    reply_ok: bool | None = None  # None: nothing was sent
    # An LLM call that returned but is not logged yet: (model, latency_ms, response)
    unlogged_call: tuple[str, int, Any] | None = None
    # Order card saved this turn and not yet handed back: cancelled if the turn crashes
    card_id: str | None = None
    # Card fast path: fast | qword_same | qword_diff (only the question word kept the LLM) | -.
    # The qword counts decide whether to keep the question-word check (docs/adr/0002)
    card_path: str = "-"


# A tool's request to hand over now: (reason, inbox card detail), or None
type HandOver = tuple[str, str | None] | None

_TOOL_NAMES = frozenset(t["function"]["name"] for t in TOOLS)


def _tool_label(name: Any) -> str:
    """Tool names come from the LLM: log only known ones."""
    return name if isinstance(name, str) and name in _TOOL_NAMES else "unknown"


# Provider-supplied finish reasons: anything else is logged as "other" (no free text in logs)
_FINISH_REASONS = frozenset({"stop", "length", "tool_calls", "content_filter", "function_call"})


def _ms_since(t: float) -> int:
    return round((time.monotonic() - t) * 1000)


def _known(value: Any, allowed: frozenset[str]) -> str:
    if value is None:
        return "-"
    return value if isinstance(value, str) and value in allowed else "other"


def _num(value: Any) -> int | str:
    return value if isinstance(value, int) and not isinstance(value, bool) else "-"


def log_llm_call(
    stats: TurnStats,
    model: str,
    latency_ms: int,
    resp: Any = None,
    tools: str = "",
    error: str = "",
) -> None:
    """One line per LLM call. Never logs message text, tool arguments or error bodies."""
    stats.unlogged_call = None
    usage = getattr(resp, "usage", None)
    details = getattr(usage, "prompt_tokens_details", None)
    choices = getattr(resp, "choices", None)
    first = choices[0] if isinstance(choices, list) and choices else None
    log.info(
        "llm_call turn_id=%s model=%s latency_ms=%d prompt_tokens=%s cached_tokens=%s "
        "completion_tokens=%s finish_reason=%s tools=%s error=%s",
        stats.turn_id,
        model,
        latency_ms,
        _num(getattr(usage, "prompt_tokens", None)),
        _num(getattr(details, "cached_tokens", None)),
        _num(getattr(usage, "completion_tokens", None)),
        _known(getattr(first, "finish_reason", None), _FINISH_REASONS),
        tools or "-",
        error or "-",
    )


def flush_llm_call(stats: TurnStats, error: str) -> None:
    """The turn crashed after an LLM call returned: still log that call, once."""
    if stats.unlogged_call is not None:
        model, latency_ms, resp = stats.unlogged_call
        log_llm_call(stats, model, latency_ms, resp, error=error)


def start_handover(
    db: ShopDB,
    line_user_id: str,
    reason: str,
    reply_text: str | None,
    *,
    detail: str | None = None,
    customer_text: str | None = None,
) -> AgentTurn:
    """Bot-started hand-over: quiet the bot, open/extend the inbox card, reply once."""
    _, is_new = db.open_handover(line_user_id, reason, detail, customer_text)
    return AgentTurn(
        reply_text=reply_text,
        reason="turn_limit" if reason == "turn_limit" else "hand_over",
        new_card_reason=reason if is_new else None,
        new_card_detail=detail if is_new else None,
    )


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
        "4. ถ้าลูกค้าขอ 'พิเศษ' (รวมถึงพิมพ์ผิด เช่น 'พิเสด') ให้ใส่ option_ids=['special'] "
        "สำหรับจานหลักได้เลยโดยไม่ต้องถามเพิ่ม ถ้าลูกค้าไม่ได้พูดถึงพิเศษ ห้ามใส่ special เอง "
        "คำขอปรับแต่งบนเมนูที่มี เช่น ไม่ใส่ผัก แยกน้ำ ให้เก็บใน note "
        "ถ้าลูกค้าขอเมนู/เส้น/เนื้อสัตว์ที่ไม่มีในเมนู ให้บอกตรงๆ ว่าไม่มี แล้วแนะนำเมนูใกล้เคียงที่มีในเมนู JSON "
        "(ไม่ต้องเรียก hand_over) แต่ถ้าถามเรื่องส่วนผสม/แพ้อาหาร/ผงชูรส หรือขอสิ่งที่บอทตอบเองไม่ได้ "
        "ห้ามเดาหรือรับรองความปลอดภัย ให้เรียก hand_over(reason='off_menu')\n"
        "5. ร้านรับทั้งรับเองที่ร้าน (pickup) และส่ง (delivery) "
        f"กติกาค่าส่ง: {menu.shop.delivery.rule_text_th()} "
        "ถ้ายังไม่รู้ว่ารับเองหรือให้ส่ง ให้ถามครั้งเดียวก่อนเรียก build_order_card "
        "ถ้าลูกค้าเลือกส่งและมีที่อยู่เดิมในระบบ ให้ใช้ที่อยู่เดิมและแจ้งสั้นๆ ครั้งเดียวว่า "
        "'ส่งที่เดิมนะคะ ถ้าเปลี่ยนที่อยู่บอกได้เลยค่ะ' ห้ามพิมพ์ที่อยู่เต็ม (การ์ดแสดงที่อยู่ให้แล้ว) "
        "ถ้ามีที่อยู่เดิม เวลาถามวิธีรับอาหารให้ใช้คำว่า 'ส่งที่เดิม' โดยไม่ต้องพิมพ์ที่อยู่ "
        "ถ้าไม่มีที่อยู่เดิม ให้ถามแค่ 'รับเองที่ร้านหรือให้ส่งคะ ถ้าส่งรบกวนแจ้งที่อยู่ด้วยนะคะ' "
        "ห้ามพูดถึงที่อยู่เดิมหรือ 'ในระบบ' กับลูกค้าที่ไม่มีที่อยู่เดิม "
        "(สถานะระบบแสดง 'ที่อยู่เดิม=มี' เฉพาะเมื่อลูกค้ามีที่อยู่เดิม)\n"
        "6. เมื่อรายการและวิธีรับอาหารครบแล้ว ให้เรียก build_order_card เพื่อให้ระบบส่งการ์ดยืนยัน "
        "ถ้าลูกค้าบอกจานพร้อมตัวเลือกที่จำเป็นครบและวิธีรับอาหาร (หรือให้ส่งโดยมีที่อยู่เดิม) มาในข้อความเดียว "
        "ให้เรียก update_cart, set_fulfillment และ build_order_card พร้อมกันในการตอบครั้งเดียว "
        "(ไม่ต้องรอผลของ update_cart ก่อนเรียก build_order_card และไม่ต้องเรียกทีละครั้ง) "
        "เมื่อ build_order_card สำเร็จ ระบบจะส่งการ์ดสรุปเต็มรูปแบบให้ลูกค้าเอง "
        "ให้ตอบข้อความสั้นเพียง 1 บรรทัด (เช่น 'ตรวจสอบรายการบนการ์ดแล้วกดยืนยันได้เลยนะคะ') "
        "ไม่ต้องทวนรายการอาหารหรือราคาซ้ำในข้อความ ห้ามอ้างว่าสั่งสำเร็จจนกว่าลูกค้าจะกดปุ่มยืนยันบนการ์ด "
        "ถ้าร้านปิดให้แจ้งเวลาเปิดปกติและไม่รับออเดอร์\n"
        "7. ระบบแสดงเวลารอโดยประมาณบนการ์ดและใบเสร็จให้เอง ถ้าลูกค้าถามเสร็จยังหรือรอนานไหม "
        "ให้เรียก get_my_orders แล้วตอบคิวที่เหลือและช่วงเวลาตามผลเครื่องมือ "
        "เช่น 'ยังไม่เสร็จค่ะ ก่อนหน้าอีก 2 คิว ประมาณ 10–15 นาที'\n"
        "8. ถามสถานะออเดอร์เรียก get_my_orders ขอ QR ถามวิธีจ่ายหรือยอดค้างเรียก get_payment_info "
        "ขอยกเลิกออเดอร์ที่ยัง queued เรียก cancel_order "
        "แต่ถ้าขอแก้ไขออเดอร์ที่ยืนยันไปแล้ว ให้เรียก hand_over อย่างเดียว "
        "ถ้าลูกค้าทักทาย ('สวัสดีครับ') หรือคุยเล่น ให้ทักกลับสั้นๆ แล้วถามว่าวันนี้รับอะไรดีคะ "
        "(ไม่ต้องส่งเมนูทั้งหมด และไม่ต้องเรียก hand_over)\n"
        "9. ออเดอร์ที่ลูกค้ากดยืนยันไปแล้วถือว่าเสร็จสิ้นและระบบเคลียร์ตะกร้าให้ใหม่แล้ว "
        "ห้ามนำรายการหรือยอดของออเดอร์เก่ามารวมกับตะกร้าใหม่ และห้ามทักว่ารายการเก่าหายไปจากตะกร้า\n"
        "10. ปกติลูกค้าจ่ายด้วยการโอนตาม QR (ระบบส่ง QR ให้เอง) ถ้าลูกค้าบอกว่าจะจ่ายเงินสด "
        "ให้เรียก set_payment_method(method='cash') แล้วตอบสั้นๆ ว่าชำระเงินสดตอนรับอาหารได้ "
        "ห้ามบอกให้สแกน QR ถ้าลูกค้าเปลี่ยนใจจะโอนหรือขอ QR ให้เรียก set_payment_method(method='transfer') "
        "แล้วเรียก get_payment_info(send_qr=true)\n"
        "11. ถ้าลูกค้าถามว่ามีเมนูอะไรบ้างหรือขอดูเมนู ให้เรียก show_menu (ระบบส่งรายการเมนูให้เอง) "
        "แล้วตอบสั้นๆ 1 บรรทัดชวนสั่ง ห้ามพิมพ์รายการเมนูเอง"
    )
    if menu.shop.address:
        rules += (
            f"\n12. ที่อยู่ร้านคือ {menu.shop.address} "
            "ถ้าลูกค้าถามว่าร้านอยู่ที่ไหนหรือจะมารับที่ร้านยังไง ให้ตอบด้วยที่อยู่นี้ (ไม่ต้องเรียก hand_over)"
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


NOTE_CHARS = 40  # notes are short requests ("ไม่ใส่ผัก"); the live state shows no more


_URL_RE = re.compile(r"https?://\S+|maps\.\S+", re.IGNORECASE)


def _cart_text(menu: Menu, items: list[Any], hide: tuple[str | None, ...] = ()) -> str:
    """Draft lines for the live state, numbered like `remove_index`; never raises.

    `hide`: delivery values (saved or draft address) masked if a note repeats them."""
    if not items:
        return "ว่าง (0 รายการ)"
    lines: list[str] = []
    for i, raw in enumerate(items):
        line = raw if isinstance(raw, dict) else {}
        dish_id = str(line.get("dish_id") or "?")
        item = menu.items_by_id.get(dish_id)
        names = {o.id: o.name_th for g in item.groups for o in g.options} if item else {}
        raw_opts = line.get("option_ids")
        opts = [names.get(str(o), str(o)) for o in raw_opts] if isinstance(raw_opts, list) else []
        text = f"[{i}] {item.name_th if item else dish_id}"
        if opts:
            text += f" ({', '.join(opts)})"
        note = str(line.get("note") or "")
        # Longest first, so one address inside another can't leave part of it; links last
        for secret in sorted({h.strip() for h in hide if h and h.strip()}, key=len, reverse=True):
            note = note.replace(secret, "…")
        note = _URL_RE.sub("…", note)
        note = " ".join(note.replace("|", " ").split())[:NOTE_CHARS]
        if note:
            # Customer-typed text: quoted and escaped, so it reads as data, not an instruction
            text += f" โน้ตลูกค้า {json.dumps(note, ensure_ascii=False)}"
        lines.append(f"{text} ×{line.get('qty', 1)}")
    return ", ".join(lines)


def _fulfillment_text(draft: dict[str, Any]) -> str | None:
    """How the draft is received, without the address or pin themselves."""
    ff = draft.get("fulfillment")
    if ff == "pickup":
        return "รับเอง"
    if ff != "delivery":
        return None
    if not str(draft.get("address") or "").strip():
        return "ส่ง (ยังไม่มีที่อยู่)"
    return "ส่ง (มีที่อยู่+พิกัดแล้ว)" if draft.get("location_url") else "ส่ง (มีที่อยู่แล้ว)"


def _strip_bold(text: str) -> str:
    """LINE shows Markdown literally: drop `**` bold markers, never `#` (queue numbers)."""
    return text.replace("**", "")


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
    """Save a LINE location pin as delivery address (+ Maps URL), building a card if cart ready.

    While the bot is quiet the pin is saved silently and shown on the inbox card.
    """
    loc_url = f"https://maps.google.com/?q={latitude},{longitude}"
    clean_addr = clean_location_address(address, latitude, longitude)
    conv = db.get_or_create_conversation(line_user_id)
    if db.is_handed_over(conv):
        draft = dict(conv.draft)
        draft["fulfillment"] = "delivery"
        draft["address"] = clean_addr
        draft["location_url"] = loc_url
        db.save_draft(conv.id, draft)
        note = f"[ลูกค้าส่งพิกัดจัดส่ง: {clean_addr}]"
        conv = db.get_or_create_conversation(line_user_id)
        db.append_conversation_messages(conv.id, [{"role": "user", "content": note}])
        db.record_quiet_activity(
            line_user_id, f"📍 {clean_addr}", pin={"address": clean_addr, "url": loc_url}
        )
        return AgentTurn(reply_text=None, reason="silent")

    status = db.get_shop_status(menu)
    if not status.is_open:
        return AgentTurn(
            reply_text=f"ตอนนี้ร้านปิดรับออเดอร์แล้วค่ะ (ปกติเปิด {status.hours_text})",
            reason="closed",
        )
    draft = dict(conv.draft)
    draft["fulfillment"] = "delivery"
    draft["address"] = clean_addr
    draft["location_url"] = loc_url
    db.save_draft(conv.id, draft)
    conv = db.get_or_create_conversation(line_user_id)

    if draft.get("items"):
        snapshot, err = build_card_snapshot_for_conv(db, conv, menu)
        if snapshot is not None:
            card_id = str(snapshot["card_id"])
            try:
                flex = build_order_flex(
                    snapshot,
                    shop_name=menu.shop.name,
                    wait_text=card_wait_line(db, menu, snapshot),
                )
                # Send only the card that is still active (a draft write meanwhile clears it)
                if db.get_or_create_conversation(line_user_id).active_card_id == card_id:
                    reply = (
                        f"บันทึกพิกัดจัดส่ง ({clean_addr}) แล้วค่ะ ตรวจสอบรายการบนการ์ดแล้วกดยืนยันได้เลยนะคะ"
                    )
                    db.append_conversation_event(
                        line_user_id, f"[ลูกค้าส่งพิกัดจัดส่ง: {clean_addr}]", reply
                    )
                    return AgentTurn(reply_text=reply, flex_card=flex, reason="ok", card_id=card_id)
            except Exception:
                db.cancel_active_card(line_user_id, card_id)  # saved but never sent
                raise
        reply_err = err or f"บันทึกพิกัดจัดส่ง ({clean_addr}) แล้วค่ะ"
        db.append_conversation_event(line_user_id, f"[ลูกค้าส่งพิกัดจัดส่ง: {clean_addr}]", reply_err)
        return AgentTurn(reply_text=reply_err, reason="ok")

    reply_empty = f"บันทึกพิกัดจัดส่ง ({clean_addr}) แล้วค่ะ ต้องการสั่งเมนูอะไรดีคะ?"
    db.append_conversation_event(line_user_id, f"[ลูกค้าส่งพิกัดจัดส่ง: {clean_addr}]", reply_empty)
    return AgentTurn(reply_text=reply_empty, reason="ok")


def handle_image_message(
    db: ShopDB, line_user_id: str, *, activity_recorded: bool = False
) -> AgentTurn:
    """Skip LLM for images; hand over with reason `image`, or add it silently to the open card.

    `activity_recorded`: the caller already counted this image as quiet-time activity.
    """
    conv = db.get_or_create_conversation(line_user_id)
    if db.is_handed_over(conv):
        db.append_conversation_messages(conv.id, [{"role": "user", "content": "[ลูกค้าส่งรูปภาพ]"}])
        if activity_recorded:
            db.add_reason_to_open_card(line_user_id, "image", None)
        else:
            db.record_quiet_activity(line_user_id, "[รูปภาพ]", reason="image")
        return AgentTurn(reply_text=None, reason="silent")
    db.append_conversation_event(line_user_id, "[ลูกค้าส่งรูปภาพ]", IMAGE_HANDOVER_REPLY)
    return start_handover(db, line_user_id, "image", IMAGE_HANDOVER_REPLY)


def handle_audio_message(db: ShopDB, line_user_id: str) -> AgentTurn:
    """Voice messages: ask the customer to type (no LLM). Silent while the bot is quiet."""
    conv = db.get_or_create_conversation(line_user_id)
    if db.is_handed_over(conv):
        db.record_quiet_activity(line_user_id, "[ข้อความเสียง]")
        return AgentTurn(reply_text=None, reason="silent")
    db.append_conversation_event(line_user_id, "[ลูกค้าส่งข้อความเสียง]", VOICE_REPLY)
    return AgentTurn(reply_text=VOICE_REPLY, reason="voice")


def handle_customer_text(
    db: ShopDB,
    menu: Menu,
    line_user_id: str,
    text: str,
    *,
    llm_client: Any,
    settings: Settings,
    started_at: float | None = None,
    stats: TurnStats | None = None,
) -> AgentTurn:
    """Run one customer turn with tool execution, hand-over rules, and safety guardrails."""
    t_start = time.monotonic() if started_at is None else started_at
    stats = stats or TurnStats()
    status = db.get_shop_status(menu)
    if not status.bot_enabled:
        return AgentTurn(reply_text=None, reason="bot_disabled")

    conv = db.get_or_create_conversation(line_user_id)
    user_msg = {"role": "user", "content": text}

    # Bot quiet: no LLM call and no reply; keep the message and update the inbox card.
    if db.is_handed_over(conv):
        db.append_conversation_messages(conv.id, [user_msg])
        db.record_quiet_activity(line_user_id, text)
        return AgentTurn(reply_text=None, reason="silent")

    user_turns = customer_turn_count(conv.messages) - conv.turn_offset
    if user_turns >= MAX_DAILY_TURNS:
        db.append_conversation_messages(conv.id, [user_msg])
        return start_handover(db, line_user_id, "turn_limit", TURN_LIMIT_REPLY, customer_text=text)

    # A hand-over ended: the note is owed, and cleared only once an LLM call succeeds
    resume_note = bool(conv.handed_over_until)
    resume_conv = conv

    overrides = db.get_overrides()
    system_msgs: list[dict[str, Any]] = [_system_prompt(menu, overrides)]

    # Dynamic live-state note (separate from cached menu prefix) so the LLM never trusts stale chat
    sold_parts = [*status.sold_out_ingredients, *status.sold_out_dishes]
    sold_text = ", ".join(sold_parts) if sold_parts else "ไม่มี (ทุกเมนูและวัตถุดิบพร้อมขาย)"
    customer = db.get_customer(line_user_id)
    cart_text = _cart_text(
        menu,
        list(conv.draft.get("items") or []),
        hide=(customer.last_address if customer else None, conv.draft.get("address")),
    )
    busy_text = "สั่ง 1 กล่องตอนนี้รอ" + range_text(one_box_minutes(db, menu), delivery=False)
    open_text = "เปิดรับออเดอร์" if status.is_open else f"ปิดร้าน (ปกติเปิด {status.hours_text})"
    unpaid_total = db.get_unpaid_balance(line_user_id).total
    # Only whether an address is saved, never the address itself (Rule 5). Shown only when one
    # is saved: "ที่อยู่เดิม=ไม่มี" on every turn led the bot to talk about it to new customers
    has_saved_addr = bool(customer and (customer.last_address or "").strip())
    ff_text = _fulfillment_text(conv.draft)
    system_msgs.append(
        {
            "role": "system",
            "content": (
                f"สถานะระบบล่าสุดตอนนี้ (ยึดข้อมูลนี้เหนือข้อความเก่าในแชท): "
                f"ร้าน={open_text} | {busy_text} | ของหมดตอนนี้={sold_text} | ตะกร้าปัจจุบัน={cart_text} "
                f"| ยอดค้างชำระ={unpaid_total} บาท"
                + (" | ที่อยู่เดิม=มี" if has_saved_addr else "")
                + (f" | วิธีรับ={ff_text}" if ff_text else "")
                + (" | วิธีจ่าย=เงินสด (ไม่ต้องส่ง QR)" if db.pays_cash(line_user_id) else "")
            ),
        }
    )
    if resume_note:
        system_msgs.append({"role": "system", "content": RESUME_NOTE})

    messages: list[dict[str, Any]] = [*conv.messages, user_msg]
    turn_start = len(conv.messages)  # messages[turn_start:] = this turn (user msg, tool calls)

    built_card_id: str | None = None  # an order card saved this turn (sent only on success)

    def save_turn(*final: dict[str, Any]) -> None:
        """Append this turn to the history as stored *now*, so events written meanwhile (an
        owner push, a slip, a confirm) are never overwritten (AGENTS.md)."""
        db.append_conversation_messages(conv.id, [*messages[turn_start:], *final])

    def drop_unsent_card() -> None:
        """A card the customer never saw must not stay confirmable (e.g. by a slip)."""
        if built_card_id is not None:
            db.cancel_active_card(line_user_id, built_card_id)

    def owner_took_over() -> AgentTurn | None:
        """คุยเอง tapped while this turn ran: send nothing, keep the message for the owner."""
        if not db.is_handed_over(db.get_or_create_conversation(line_user_id)):
            return None
        drop_unsent_card()
        db.append_conversation_messages(conv.id, [user_msg])
        db.record_quiet_activity(line_user_id, text)
        return AgentTurn(reply_text=None, reason="silent")

    def control_stop() -> AgentTurn | None:
        """Bot paused or คุยเอง while this turn ran (re-checked after every slow call)."""
        if not db.get_shop_status(menu).bot_enabled:
            # Global pause: no reply and no inbox card, like a message sent while paused
            drop_unsent_card()
            db.append_conversation_messages(conv.id, [user_msg])
            return AgentTurn(reply_text=None, reason="bot_disabled")
        return owner_took_over()

    def fail_turn() -> AgentTurn:
        if (stop := control_stop()) is not None:
            return stop
        drop_unsent_card()
        save_turn({"role": "assistant", "content": ERROR_REPLY})
        return AgentTurn(reply_text=ERROR_REPLY, reason="error")

    tool_calls_made = 0
    built_flex: dict[str, Any] | None = None
    qr_url: str | None = None
    qr_explicit = False  # get_payment_info(send_qr=true): the customer asked for the QR
    qr_amount = 0  # the balance the LLM was told (and will quote) for that QR
    menu_text: str | None = None

    extra_body: dict[str, Any] = {}
    if settings.llm_reasoning_effort:
        extra_body["reasoning"] = {"effort": settings.llm_reasoning_effort}

    def save_draft(draft: dict[str, Any]) -> None:
        """Any draft write invalidates the order card (SQLite clears it): never send it."""
        nonlocal conv, built_flex, built_card_id
        db.save_draft(conv.id, draft)
        conv = db.get_or_create_conversation(line_user_id)
        built_flex = None
        built_card_id = None

    def _run_tool(name: str, args: dict[str, Any]) -> tuple[dict[str, Any], HandOver]:
        """Execute a tool; returns (result_payload, (reason, detail) to hand over now or None)."""
        nonlocal conv, built_flex, built_card_id, qr_url, qr_explicit, qr_amount, menu_text
        if name == "hand_over":
            reason = str(args.get("reason") or "")
            if reason == "greeting":
                return {
                    "ok": False,
                    "error": "คำทักทายให้ทักกลับเองแล้วถามว่าวันนี้รับอะไรดี ไม่ต้องส่งต่อร้าน",
                }, None
            if reason not in LLM_HANDOVER_REASONS:
                # A broken call never hands over (and never blocks the rest of the batch)
                return {"ok": False, "error": "reason ต้องเป็น edit_confirmed หรือ off_menu"}, None
            q = args.get("queue_number")
            detail = f"#{q}" if isinstance(q, int) and not isinstance(q, bool) else None
            return {"ok": True}, (reason, detail if reason == "edit_confirmed" else None)

        if name == "get_shop_status":
            st = db.get_shop_status(menu)
            cust = db.get_customer(line_user_id)
            return {
                "is_open": st.is_open,
                "hours": st.hours_text,
                "busy_mode": st.busy_mode,
                "minimum_wait_minutes": st.wait_minutes,
                "wait_for_one_box_now": range_text(one_box_minutes(db, menu), delivery=False),
                "delivery_rule": st.delivery_rule_text,
                "sold_out_ingredients": st.sold_out_ingredients,
                "sold_out_dishes": st.sold_out_dishes,
                "saved_address": cust.last_address if cust else None,
                "has_saved_location_pin": bool(cust and cust.last_location_url),
                "shop_address": menu.shop.address,
            }, None

        if name == "update_cart":
            action = str(args.get("action", ""))
            st = db.get_shop_status(menu)
            if not st.is_open and action in ("add", "replace"):
                return {
                    "ok": False,
                    "error": f"ร้านปิดรับออเดอร์แล้วค่ะ (ปกติเปิด {st.hours_text})",
                }, None

            draft = dict(conv.draft)
            items: list[dict[str, Any]] = list(draft.get("items") or [])
            if action == "clear":
                items = []
            elif action == "remove":
                raw_idx = args.get("remove_index")
                if not isinstance(raw_idx, int) or isinstance(raw_idx, bool):
                    return {"ok": False, "error": "ลำดับรายการไม่ถูกต้องค่ะ"}, None
                if 0 <= raw_idx < len(items):
                    items.pop(raw_idx)
                else:
                    return {"ok": False, "error": "ไม่พบลำดับรายการที่ต้องการลบค่ะ"}, None
            elif action in ("add", "replace"):
                incoming_raw = args.get("items")
                if not isinstance(incoming_raw, list) or not incoming_raw:
                    return {"ok": False, "error": "ยังไม่มีรายการอาหารที่จะเพิ่มค่ะ"}, None
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
                    return {"ok": False, "error": str(exc)}, None
                items = normalized if action == "replace" else [*items, *normalized]
            else:
                return {"ok": False, "error": "คำสั่งตะกร้าไม่ถูกต้อง"}, None

            draft["items"] = items
            if not items:
                save_draft(draft)  # clearing is allowed even if an item is sold out
                return {"ok": True, "items": [], "food_total": 0}, None
            # Validate the whole cart (sold out since added, too many lines) before saving
            ff: Fulfillment = "delivery" if draft.get("fulfillment") == "delivery" else "pickup"
            try:
                priced = menu.price_cart(items, ff, db.get_overrides())
            except MenuError as exc:
                return {"ok": False, "error": str(exc)}, None
            save_draft(draft)
            cust = db.get_customer(line_user_id)
            return {
                "ok": True,
                "items": [line.to_dict() for line in priced.lines],
                "food_total": priced.food_total,
                "fulfillment": draft.get("fulfillment"),
                "address": draft.get("address"),
                "saved_address": cust.last_address if cust else None,
            }, None

        if name == "set_fulfillment":
            st = db.get_shop_status(menu)
            if not st.is_open:
                return {
                    "ok": False,
                    "error": f"ร้านปิดรับออเดอร์แล้วค่ะ (ปกติเปิด {st.hours_text})",
                }, None
            ff_val = str(args.get("fulfillment", ""))
            if ff_val not in ("pickup", "delivery"):
                return {"ok": False, "error": "กรุณาเลือก pickup หรือ delivery"}, None

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
                    save_draft(draft)
                    return {
                        "ok": False,
                        "fulfillment": "delivery",
                        "error": (
                            "ลูกค้าเลือกให้จัดส่งแล้ว แต่ยังไม่มีที่อยู่จัดส่ง "
                            "กรุณาขอเฉพาะที่อยู่หรือให้ส่งพิกัด (ไม่ต้องถามว่ารับเองหรือส่งอีก)"
                        ),
                    }, None

            if "requested_time" in args and args["requested_time"] is not None:
                draft["requested_time"] = str(args["requested_time"]).strip() or None

            save_draft(draft)
            return {
                "ok": True,
                "fulfillment": draft["fulfillment"],
                "address": draft.get("address"),
                "used_saved_address": used_saved,
                "requested_time": draft.get("requested_time"),
            }, None

        if name == "build_order_card":
            conv = db.get_or_create_conversation(line_user_id)
            snapshot, err = build_card_snapshot_for_conv(db, conv, menu)
            if snapshot is None:
                return {"ok": False, "error": err or "สร้างสรุปออเดอร์ไม่สำเร็จค่ะ"}, None
            # Saved as the active card: track it now so a failure below still cancels it
            built_card_id = str(snapshot["card_id"])
            stats.card_id = built_card_id
            built_flex = None
            wait_line = card_wait_line(db, menu, snapshot)
            built_flex = build_order_flex(snapshot, shop_name=menu.shop.name, wait_text=wait_line)
            return {
                "ok": True,
                "card_id": snapshot["card_id"],
                "food_total": snapshot["food_total"],
                "delivery_fee": snapshot["delivery_fee"],
                "total": snapshot["total"],
                "wait": wait_line,
            }, None

        if name == "show_menu":
            menu_text = menu.menu_text(db.get_overrides())
            return {
                "ok": True,
                "note": "ระบบส่งรายการเมนูให้ลูกค้าแล้ว ตอบสั้นๆ 1 บรรทัดชวนสั่ง ห้ามพิมพ์รายการเมนูซ้ำ",
            }, None

        if name == "get_my_orders":
            # Today's orders, plus this customer's earlier-day orders still waiting (a pre-v2
            # order gets its status but no queue position or estimate)
            orders = [
                *db.get_customer_queued_before_today(line_user_id),
                *db.get_customer_orders_today(line_user_id),
            ]
            out_orders: list[dict[str, Any]] = []
            for o in orders:
                entry: dict[str, Any] = {
                    "order_id": o.id,
                    "shop_date": o.shop_date,
                    "queue_number": o.queue_number,
                    "status": o.status,
                    "status_th": STATUS_TH.get(o.status, o.status),
                    "total": o.total,
                    "fulfillment": o.fulfillment,
                    "cancel_reason": o.cancel_reason,
                    "items": o.items,
                }
                qinfo = queue_info(db, menu, o)
                if qinfo is not None:
                    entry["queue_position"] = qinfo.position
                    entry["orders_ahead"] = qinfo.orders_ahead
                    entry["wait_range_minutes"] = qinfo.range_str()
                    if o.fulfillment == "delivery":
                        entry["wait_note"] = "ไม่รวมเวลาส่ง"
                out_orders.append(entry)
            return {"orders": out_orders}, None

        if name == "get_payment_info":
            qr_url = None
            unpaid = db.get_unpaid_balance(line_user_id)
            info: dict[str, Any] = {
                "unpaid_balance": unpaid.total,
                "unpaid_orders": [
                    {"queue_number": o.queue_number, "shop_date": o.shop_date, "total": o.total}
                    for o in unpaid.orders
                ],
            }
            if unpaid.total <= 0:
                info["note"] = "ไม่มียอดค้างชำระ แจ้งลูกค้าว่าไม่มียอดต้องจ่ายค่ะ"
            elif db.pays_cash(line_user_id) and args.get("send_qr") is not True:
                # Cash customer: no QR and no bank details, whatever the payment settings
                info["qr_sent"] = False
                info["note"] = "ลูกค้าแจ้งจะจ่ายเงินสด บอกยอดและให้ชำระเงินสดตอนรับอาหาร ไม่ต้องส่ง QR"
            elif settings.promptpay_id:
                qr_url = qr_image_url(settings, unpaid.total)
                qr_amount = unpaid.total
                qr_explicit = args.get("send_qr") is True
                info["qr_sent"] = True
                info["note"] = "ระบบจะส่งรูป QR ยอดตรงต่อจากข้อความของคุณเอง ไม่ต้องใส่ลิงก์"
            else:
                info["qr_sent"] = False
                info["payment_info_text"] = settings.payment_info_text
            return info, None

        if name == "set_payment_method":
            method = str(args.get("method") or "")
            if method not in ("cash", "transfer"):
                return {"ok": False, "error": "วิธีจ่ายต้องเป็น cash หรือ transfer"}, None
            orders_now = db.set_pay_cash(line_user_id, method == "cash")
            conv = db.get_or_create_conversation(line_user_id)
            return {
                "ok": True,
                "method": method,
                "unpaid_balance": sum(o.total for o in orders_now),
                "note": (
                    "ไม่ต้องส่ง QR บอกลูกค้าว่าชำระเงินสดตอนรับอาหารได้"
                    if method == "cash"
                    else "ถ้าลูกค้าต้องการ QR ให้เรียก get_payment_info(send_qr=true)"
                ),
            }, None

        if name == "cancel_order":
            raw_oid = args.get("order_id")
            if isinstance(raw_oid, bool) or not isinstance(raw_oid, int):
                return {"ok": False, "error": "เลขออเดอร์ไม่ถูกต้องค่ะ"}, None
            cancelled, paid = db.cancel_customer_order(line_user_id, raw_oid)
            if paid is not None:
                # Paid orders are never cancelled by the bot; the owner handles refunds.
                return {
                    "ok": False,
                    "error": "ออเดอร์นี้จ่ายเงินแล้ว ส่งให้ร้านดูแลต่อ",
                }, ("paid_cancel_request", f"#{paid.queue_number} {paid.total}฿")
            if cancelled is None:
                return {
                    "ok": False,
                    "error": "ยกเลิกไม่ได้ค่ะ ออเดอร์อาจเสร็จแล้ว ถูกยกเลิกแล้ว หรือไม่ใช่ของลูกค้า",
                }, None
            return {
                "ok": True,
                "queue_number": cancelled.queue_number,
                "status": cancelled.status,
            }, None

        return {"ok": False, "error": f"unknown tool: {name}"}, None

    def finish(reply_content: str, llm_call: tuple[int, Any] | None = None) -> AgentTurn:
        """The turn's reply: an LLM reply (`llm_call` is its unlogged call), or a fast-path
        line. No more DB writes after it."""
        nonlocal built_flex, built_card_id, qr_url
        if built_card_id is not None and (
            built_flex is None
            or db.get_or_create_conversation(line_user_id).active_card_id != built_card_id
        ):
            # The card changed or failed to render after it was built: never send it
            drop_unsent_card()
            built_flex = None
            built_card_id = None
        if not reply_content and built_flex is None:
            reply_content = LIMIT_REPLY
        if llm_call is not None:
            log_llm_call(stats, settings.llm_model, *llm_call)
        if qr_url is not None:
            # The QR must show the amount the reply text quotes. If the balance changed
            # during the turn (a later tool, or the owner recording a payment), or the
            # customer switched to cash, send no QR; they can ask again.
            balance_now = db.get_unpaid_balance(line_user_id).total
            if balance_now != qr_amount or (db.pays_cash(line_user_id) and not qr_explicit):
                qr_url = None
        save_turn({"role": "assistant", "content": reply_content})  # last DB read done
        return AgentTurn(
            reply_text=reply_content or None,
            flex_card=built_flex,
            reason="ok",
            image_url=qr_url,
            menu_text=menu_text,
            card_id=built_card_id if built_flex is not None else None,
        )

    def fast_card_line(
        batch: list[tuple[Any, dict[str, Any]]], content: str
    ) -> tuple[str | None, str | None]:
        """(line to send now, line the question word held back) for the first tool batch.

        Spec §3.1: only update_cart / set_fulfillment / build_order_card, each at least once,
        all ok, a card built, and no text from the LLM alongside the tools. The caller checks
        that the card is still active right before replying.
        """
        names = {name if isinstance(name, str) else "invalid" for name, _ in batch}
        if names != _CARD_TOOLS or content.strip():
            return None, None
        if not all(isinstance(r, dict) and r.get("ok") is True for _, r in batch):
            return None, None
        if built_flex is None or built_card_id is None:
            return None, None
        ff = [r for name, r in batch if name == "set_fulfillment"][-1]
        if ff.get("fulfillment") == "pickup":
            line = CARD_LINE_PICKUP
        elif ff.get("used_saved_address") is True:
            line = CARD_LINE_SAVED_ADDRESS
        else:
            line = CARD_LINE_NEW_ADDRESS
        if has_side_question(text):
            return None, line
        return line, None

    first_batch = True
    qword_line: str | None = None  # the fast-path line a question word held back

    while True:
        remaining = TURN_DEADLINE_S - (time.monotonic() - t_start)
        if remaining <= 0:
            if (stop := control_stop()) is not None:
                return stop
            drop_unsent_card()
            save_turn({"role": "assistant", "content": LIMIT_REPLY})
            return AgentTurn(reply_text=LIMIT_REPLY, reason="deadline")

        stats.llm_calls += 1
        t_call = time.monotonic()
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
            http_status = _num(getattr(exc, "status_code", None))
            error = type(exc).__name__ + (f":{http_status}" if http_status != "-" else "")
            log_llm_call(stats, settings.llm_model, _ms_since(t_call), error=error)
            return fail_turn()
        latency_ms = _ms_since(t_call)
        stats.unlogged_call = (settings.llm_model, latency_ms, resp)  # if the turn crashes

        # Paused or คุยเอง while the LLM was thinking: stop before running any tool or replying
        if (stop := control_stop()) is not None:
            log_llm_call(stats, settings.llm_model, latency_ms, resp)
            return stop

        choices = getattr(resp, "choices", None)
        msg = getattr(choices[0], "message", None) if choices else None
        if msg is None:
            log_llm_call(stats, settings.llm_model, latency_ms, resp, error="no_message")
            return fail_turn()
        tool_calls = msg.tool_calls or []
        if any(getattr(tc, "function", None) is None for tc in tool_calls):
            log_llm_call(stats, settings.llm_model, latency_ms, resp, error="bad_tool_call")
            return fail_turn()  # not usable: the resume note stays owed
        if resume_note:
            # First usable response: the note was delivered, so stop owing it
            db.consume_resume_note(resume_conv)
            resume_note = False
        content = msg.content if isinstance(msg.content, str) else ""

        if not tool_calls:
            reply_content = _strip_bold(content).strip()
            if qword_line is not None:
                # Only the question word kept this LLM reply: did it say more than the line?
                said_more = bool(reply_content) and reply_content != qword_line
                stats.card_path = "qword_diff" if said_more else "qword_same"
            return finish(reply_content, llm_call=(latency_ms, resp))

        if tool_calls_made + len(tool_calls) > MAX_TOOL_CALLS:
            log_llm_call(stats, settings.llm_model, latency_ms, resp, error="max_tool_calls")
            drop_unsent_card()
            save_turn({"role": "assistant", "content": LIMIT_REPLY})
            return AgentTurn(reply_text=LIMIT_REPLY, reason="max_tool_calls")

        assistant_tool_msg: dict[str, Any] = {
            "role": "assistant",
            "content": content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name
                        if isinstance(tc.function.name, str)
                        else "invalid",
                        "arguments": tc.function.arguments
                        if isinstance(tc.function.arguments, str)
                        else "{}",
                    },
                }
                for tc in tool_calls
            ],
        }
        parsed: list[dict[str, Any] | None] = []
        for tc in tool_calls:
            try:
                raw_args = tc.function.arguments
                if raw_args is not None and not isinstance(raw_args, str):
                    raise TypeError("arguments must be a JSON string")
                args = json.loads(raw_args or "{}")
            except (ValueError, TypeError):  # incl. JSONDecodeError and huge integers
                args = None
            parsed.append(args if isinstance(args, dict) else None)

        # A hand-over wins over the whole batch: run none of its other tools
        handover_now: HandOver = None
        for tc, args in zip(tool_calls, parsed, strict=True):
            if tc.function.name == "hand_over" and args is not None:
                handover_now = _run_tool("hand_over", args)[1]  # no side effects
                if handover_now is not None:
                    break
        tool_results: list[dict[str, Any]] = []
        marks = ["hand_over:ok"] if handover_now is not None else []
        edit_failed = False  # a cart/fulfillment edit in this batch failed
        # Card builds run after the batch's other tools, whatever order the provider sent
        # them in, so a card always shows the edited cart (results keep the original order)
        run_order = sorted(
            range(len(tool_calls)), key=lambda i: tool_calls[i].function.name == "build_order_card"
        )
        results_by_index: dict[int, dict[str, Any]] = {}
        batch_out: list[tuple[Any, dict[str, Any]]] = []  # (tool name, result) as run
        if handover_now is None:
            for idx in run_order:
                tc, args = tool_calls[idx], parsed[idx]
                tool_calls_made += 1
                had_card = built_card_id is not None
                if args is None:
                    # Broken arguments never run a tool (e.g. a hand_over with no reason)
                    result, trigger_handover = {"ok": False, "error": BAD_ARGS_ERROR}, None
                elif tc.function.name == "build_order_card" and edit_failed:
                    # Batched with an edit that failed: never show a card for the old cart
                    result, trigger_handover = {"ok": False, "error": CARD_AFTER_FAILED_EDIT}, None
                else:
                    try:
                        result, trigger_handover = _run_tool(tc.function.name, args)
                    except Exception as exc:
                        log.warning("Tool %s failed: %s", tc.function.name, type(exc).__name__)
                        result, trigger_handover = (
                            {"ok": False, "error": "ข้อมูลคำสั่งไม่ถูกต้อง กรุณาลองใหม่ค่ะ"},
                            None,
                        )
                ok = isinstance(result, dict) and result.get("ok") is not False
                if not ok and tc.function.name in ("update_cart", "set_fulfillment"):
                    edit_failed = True
                if had_card and built_card_id is None and isinstance(result, dict):
                    # This edit (even one that failed, e.g. delivery with no address yet)
                    # cancelled a card built earlier in the turn: say so, or the LLM would
                    # tell the customer to tap a card that is never sent
                    result = {**result, "card_note": CARD_CLEARED_NOTE}
                batch_out.append((tc.function.name, result))
                marks.append(f"{_tool_label(tc.function.name)}:{'ok' if ok else 'err'}")
                if trigger_handover is not None:
                    handover_now = trigger_handover  # e.g. a paid cancel: stop the batch here
                    break
                results_by_index[idx] = {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(result, ensure_ascii=False),
                }
            tool_results = [results_by_index[i] for i in sorted(results_by_index)]

        if edit_failed:
            # An edit failed: any card built this turn shows a cart the customer is changing,
            # so it is never sent, and the LLM is told this batch's builds did not count
            if built_card_id is not None:
                drop_unsent_card()
            built_flex = None
            built_card_id = None
            for i, (tc, res) in enumerate(zip(tool_calls, tool_results, strict=False)):
                if tc.function.name == "build_order_card":
                    refused = {"ok": False, "error": CARD_AFTER_FAILED_EDIT}
                    tool_results[i] = {**res, "content": json.dumps(refused, ensure_ascii=False)}
        log_llm_call(stats, settings.llm_model, latency_ms, resp, tools=",".join(marks))
        if handover_now is not None:
            if qword_line is not None:
                stats.card_path = "qword_diff"  # the kept LLM call handed over instead
            drop_unsent_card()
            db.append_conversation_messages(
                conv.id, [user_msg, {"role": "assistant", "content": HANDOVER_REPLY}]
            )
            ho_reason, ho_detail = handover_now
            return start_handover(
                db,
                line_user_id,
                ho_reason,
                HANDOVER_REPLY,
                detail=ho_detail,
                customer_text=text,
            )

        messages.append(assistant_tool_msg)
        messages.extend(tool_results)
        if first_batch:
            first_batch = False
            fast_line, qword_line = fast_card_line(batch_out, content)
            if fast_line is not None:
                # No second LLM call, but still a reply path: re-check pause and คุยเอง
                if (stop := control_stop()) is not None:
                    return stop
                card_now = db.get_or_create_conversation(line_user_id).active_card_id
                if card_now == built_card_id and time.monotonic() - t_start < TURN_DEADLINE_S:
                    stats.card_path = "fast"
                    return finish(fast_line)
                # The card changed, or the turn ran out of time: the normal path handles it
