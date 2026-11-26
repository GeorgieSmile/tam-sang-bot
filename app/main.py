"""FastAPI application: health check, kitchen board, and LINE webhook."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.types import Receive, Scope, Send

from app.agent import (
    AgentTurn,
    TurnStats,
    flush_llm_call,
    handle_audio_message,
    handle_customer_text,
    handle_image_message,
    handle_location_message,
    make_openai_client,
)
from app.batch import batch_groups
from app.config import Settings, load_settings
from app.db import PAID_STATUSES, Order, ShopDB
from app.handover import on_new_card, reason_label
from app.line import (
    HttpLineClient,
    LineClient,
    NoopLineClient,
    format_order_receipt,
    format_owner_order_alert,
    format_payment_lines,
    image_message,
    parse_postback_data,
    text_message,
    verify_signature,
)
from app.menu import Menu, load_menu
from app.owner import handle_owner_text
from app.promptpay import (
    build_payload,
    parse_qr_name,
    qr_image_url,
    render_png,
    validate_promptpay_settings,
)
from app.slip_intake import SlipTurn, handle_image
from app.slips import (
    QUOTA_WARN_BELOW,
    HttpSlipOkClient,
    QuotaCache,
    SlipClient,
    validate_slipok_settings,
)
from app.wait import receipt_queue_line

log = logging.getLogger("app.main")

KITCHEN_HTML_PATH = Path(__file__).resolve().parent / "kitchen.html"
KITCHEN_COOKIE = "kitchen_key"


class ShopUpdateBody(BaseModel):
    open_override: Literal["open", "closed"] | None = None
    clear_open_override: bool = False
    busy_mode: bool | None = None
    wait_minutes: int | None = None
    bot_enabled: bool | None = None
    notify_owner_on_order: bool | None = None
    notify_owner_on_inbox: bool | None = None


class OverrideUpdateBody(BaseModel):
    kind: Literal["dish", "ingredient"]
    key: str
    is_available: bool


class DoneOrderBody(BaseModel):
    notify: bool = False
    # The owner answered "รับเงินสดแล้ว" on the done prompt (unpaid orders only)
    paid_cash: bool = False


class CancelOrderBody(BaseModel):
    reason: str = Field(min_length=1)


class PaymentBody(BaseModel):
    status: Literal["cash", "transfer_manual", "unpaid"]


class MarkPaidBody(BaseModel):
    order_ids: list[int]


def undelivered_note(reply_text: str | None) -> str:
    """History note after a reply that never reached the customer (a `[` note, so it is not
    a customer message). Quotes that reply, since an owner push may come after it."""
    start = " ".join((reply_text or "").split())[:30]
    which = f'ข้อความตอบกลับของบอท "{start}…"' if start else "การ์ดสรุปออเดอร์ของบอท"
    return f"[ระบบ: {which} ส่งไม่ถึงลูกค้า ลูกค้าไม่เห็นข้อความและการ์ดนั้น]"


REFUND_PUSH_LINE = "ร้านจะติดต่อคืนเงินค่ะ"
# Receipt payment line for a cash customer when there's no PromptPay QR (never bank details)
CASH_PAY_TEXT = "ชำระเงินสดตอนรับอาหารได้เลยค่ะ"


_SECRET_QUERY_RE = re.compile(r"((?:^|[?&])key=)[^&\s]*")


class _RedactKitchenKey(logging.Filter):
    """Uvicorn's access log prints the full path; never let `?key=` (the kitchen login) in."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            record.args = (*args[:2], _SECRET_QUERY_RE.sub(r"\1***", args[2]), *args[3:])
        return True


def _install_log_redaction() -> None:
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _RedactKitchenKey) for f in access.filters):
        access.addFilter(_RedactKitchenKey())


_EVENT_TYPES = frozenset({"message", "postback", "follow", "unfollow", "join", "leave"})
_MESSAGE_TYPES = frozenset({"text", "image", "video", "audio", "file", "location", "sticker"})


def _event_label(event: dict[str, Any]) -> str:
    """LINE event type for the log, from fixed lists only (no free text in logs)."""
    ev = event.get("type")
    label = ev if isinstance(ev, str) and ev in _EVENT_TYPES else "other"
    message = event.get("message")
    if isinstance(message, dict):
        mt = message.get("type")
        label += "." + (mt if isinstance(mt, str) and mt in _MESSAGE_TYPES else "other")
    return label


def _install_app_logging() -> None:
    """Uvicorn configures only its own loggers: show `app.*` INFO telemetry too (§3.4).

    Skipped when the root logger already has a handler, so no line is printed twice."""
    app_log = logging.getLogger("app")
    if app_log.level == logging.NOTSET:
        app_log.setLevel(logging.INFO)  # telemetry is INFO, whatever the root level
    httpx_log = logging.getLogger("httpx")
    if httpx_log.level == logging.NOTSET:
        httpx_log.setLevel(logging.WARNING)  # its INFO lines print URLs with LINE user IDs
    if not app_log.handlers and not logging.getLogger().handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        app_log.addHandler(handler)
        app_log.propagate = False  # one printer, even if a root handler is added later


def format_done_push(order: Order, unpaid_balance: int = 0) -> str:
    if order.fulfillment == "pickup":
        text = f"ออเดอร์ #{order.queue_number} เสร็จแล้ว มารับได้เลยค่ะ"
    else:
        text = f"ออเดอร์ #{order.queue_number} กำลังออกไปส่งแล้วค่ะ"
    if unpaid_balance > 0:
        text += f" · ยอดค้าง {unpaid_balance}฿"
    return text


def format_cancel_push(order: Order) -> str:
    reason = order.cancel_reason or "ร้านไม่สะดวกรับออเดอร์"
    text = f"ขออภัยค่ะ ออเดอร์ #{order.queue_number} ถูกยกเลิก ({reason})"
    if order.payment_status in PAID_STATUSES:
        text += f" {REFUND_PUSH_LINE}"
    return text


def _check_kitchen_auth(request: Request, secret: str) -> bool:
    candidate = request.query_params.get("key") or request.cookies.get(KITCHEN_COOKIE) or ""
    if not secret or not candidate:
        return False
    return hmac.compare_digest(candidate, secret)


def create_app(
    *,
    settings: Settings | None = None,
    db: ShopDB | None = None,
    menu: Menu | None = None,
    line_client: LineClient | None = None,
    llm_client: Any = None,
    slip_client: SlipClient | None = None,
) -> FastAPI:
    cfg = settings or load_settings()
    _install_log_redaction()
    _install_app_logging()
    validate_promptpay_settings(cfg)
    validate_slipok_settings(cfg.slipok_api_key, cfg.slipok_branch_id)
    loaded_menu = menu or load_menu(cfg.menu_path)
    shop_db = db or ShopDB(cfg.db_path)
    # A LINE client created here is closed here, on shutdown; an injected one is the caller's
    own_line = (
        HttpLineClient(cfg.line_channel_access_token)
        if line_client is None and cfg.line_channel_access_token
        else None
    )
    line: LineClient = line_client if line_client is not None else own_line or NoopLineClient()
    llm = llm_client if llm_client is not None else make_openai_client(cfg)
    slips: SlipClient | None = slip_client
    if slips is None and cfg.slipok_api_key and cfg.slipok_branch_id:
        slips = HttpSlipOkClient(cfg.slipok_api_key, cfg.slipok_branch_id)
    quota = QuotaCache(slips, shop_db.now_bkk)
    user_locks: dict[str, asyncio.Lock] = {}

    def _lock_for(user_id: str) -> asyncio.Lock:
        lock = user_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            user_locks[user_id] = lock
        return lock

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if own_line is not None:
            own_line.close()

    app = FastAPI(title="tam-sang-bot", lifespan=lifespan)
    app.state.settings = cfg
    app.state.menu = loaded_menu
    app.state.db = shop_db
    app.state.line = line
    app.state.llm = llm

    @app.get("/health")
    def health() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/qr/{name}.png")
    def promptpay_qr(name: str) -> Response:
        """PromptPay QR image. The path holds only the amount and an HMAC signature."""
        satang = parse_qr_name(cfg, name) if cfg.promptpay_id else None
        if satang is None:
            raise HTTPException(status_code=404, detail="Not found")
        png = render_png(build_payload(cfg.promptpay_id, satang / 100))
        return Response(
            content=png, media_type="image/png", headers={"Cache-Control": "public, max-age=86400"}
        )

    @app.get("/kitchen", response_class=HTMLResponse)
    def kitchen_page(request: Request, response: Response) -> HTMLResponse:
        if not _check_kitchen_auth(request, cfg.kitchen_secret):
            raise HTTPException(status_code=403, detail="Invalid kitchen key")
        html = KITCHEN_HTML_PATH.read_text(encoding="utf-8")
        resp = HTMLResponse(content=html)
        key_q = request.query_params.get("key")
        if key_q:
            resp.set_cookie(KITCHEN_COOKIE, key_q, httponly=True, samesite="lax")
        return resp

    @app.get("/api/kitchen/state")
    def kitchen_state(request: Request) -> JSONResponse:
        if not _check_kitchen_auth(request, cfg.kitchen_secret):
            raise HTTPException(status_code=403, detail="Invalid kitchen key")
        status = shop_db.get_shop_status(loaded_menu)
        overrides = shop_db.get_overrides()
        orders = shop_db.get_kitchen_orders_today()
        orders["unpaid"] = shop_db.get_unpaid_done_orders()
        # รวมจาน: what to cook now; pre-v2 leftovers are shown but not batched
        orders["batch"] = batch_groups(
            [o for o in orders["queued"] if o["payment_status"] != "legacy"]
        )
        inbox = shop_db.get_open_handovers()
        for card in inbox:
            card["reason_lines"] = [
                reason_label(str(d["reason"]), d.get("detail"))
                for d in card["details"]
                if d.get("kind") == "reason"
            ]
            card["has_slip_reason"] = any(str(r).startswith("slip_") for r in card["reasons"])
            if card["has_slip_reason"]:
                # Queue numbers repeat across days: label earlier days so the picker is unambiguous
                card["unpaid_orders"] = [
                    {
                        "id": o.id,
                        "queue_number": o.queue_number,
                        "total": o.total,
                        "ref": f"#{o.queue_number}"
                        if o.shop_date == status.shop_date
                        else f"#{o.queue_number} ({shop_db.day_label(o.shop_date)})",
                    }
                    for o in shop_db.get_unpaid_balance(card["line_user_id"]).orders
                ]
        quota_left = quota.get()
        payload = {
            "inbox": inbox,
            "shop": {
                "name": loaded_menu.shop.name,
                "shop_date": status.shop_date,
                "is_open": status.is_open,
                "scheduled_open": status.scheduled_open,
                "open_override": status.open_override,
                "bot_enabled": status.bot_enabled,
                "busy_mode": status.busy_mode,
                "wait_minutes": status.wait_minutes,
                "notify_owner_on_order": status.notify_owner_on_order,
                "notify_owner_on_inbox": status.notify_owner_on_inbox,
                "hours_text": status.hours_text,
            },
            "sold_out": {
                "ingredients": [
                    {
                        "key": k,
                        "name_th": v,
                        "available": loaded_menu.is_ingredient_available(k, overrides),
                    }
                    for k, v in loaded_menu.ingredients.items()
                ],
                "dishes": [
                    {
                        "key": item.id,
                        "name_th": item.name_th,
                        "available": loaded_menu.is_dish_available(item.id, overrides),
                    }
                    for item in loaded_menu.items
                ],
            },
            "orders": orders,
            "slip_quota": None
            if slips is None
            else {
                "left": quota_left,
                "warn": quota_left is not None and quota_left < QUOTA_WARN_BELOW,
            },
        }
        return JSONResponse(content=payload, headers={"Cache-Control": "no-store"})

    @app.post("/api/kitchen/shop")
    def kitchen_update_shop(request: Request, body: ShopUpdateBody) -> dict[str, bool]:
        if not _check_kitchen_auth(request, cfg.kitchen_secret):
            raise HTTPException(status_code=403, detail="Invalid kitchen key")
        if body.clear_open_override:
            shop_db.set_open_override(None)
        elif body.open_override is not None:
            shop_db.set_open_override(body.open_override)
        if body.busy_mode is not None:
            shop_db.set_busy_mode(body.busy_mode, body.wait_minutes)
        if body.bot_enabled is not None:
            shop_db.set_bot_enabled(body.bot_enabled)
        if body.notify_owner_on_order is not None:
            shop_db.set_notify_owner(body.notify_owner_on_order)
        if body.notify_owner_on_inbox is not None:
            shop_db.set_notify_owner_on_inbox(body.notify_owner_on_inbox)
        return {"ok": True}

    @app.post("/api/kitchen/overrides")
    def kitchen_update_override(request: Request, body: OverrideUpdateBody) -> dict[str, bool]:
        if not _check_kitchen_auth(request, cfg.kitchen_secret):
            raise HTTPException(status_code=403, detail="Invalid kitchen key")
        shop_db.set_override(body.kind, body.key, body.is_available)
        return {"ok": True}

    @app.post("/api/kitchen/overrides/clear")
    def kitchen_clear_overrides(request: Request) -> dict[str, bool]:
        if not _check_kitchen_auth(request, cfg.kitchen_secret):
            raise HTTPException(status_code=403, detail="Invalid kitchen key")
        shop_db.clear_all_overrides()
        return {"ok": True}

    @app.post("/api/kitchen/orders/{order_id}/done")
    def kitchen_mark_done(order_id: int, request: Request, body: DoneOrderBody) -> dict[str, Any]:
        if not _check_kitchen_auth(request, cfg.kitchen_secret):
            raise HTTPException(status_code=403, detail="Invalid kitchen key")
        order = shop_db.mark_order_done(order_id, paid_cash=body.paid_cash)
        if order is None:
            raise HTTPException(status_code=404, detail="Order not found or not queued")
        notified: bool | None = None
        if body.notify:
            try:
                balance = shop_db.get_unpaid_balance(order.line_user_id).total
                push = format_done_push(order, balance)
                line.push_text(order.line_user_id, push)
                notified = True
                # The customer saw this push: the bot's history must too (AGENTS.md)
                shop_db.append_conversation_event(
                    order.line_user_id, f"[ร้านแจ้งว่าออเดอร์ #{order.queue_number} เสร็จแล้ว]", push
                )
            except Exception as exc:
                log.warning("Done push failed for order %s: %s", order_id, type(exc).__name__)
                notified = False
        return {"ok": True, "order": order.to_dict(), "notified": notified}

    @app.post("/api/kitchen/orders/{order_id}/cancel")
    def kitchen_cancel_order(
        order_id: int, request: Request, body: CancelOrderBody
    ) -> dict[str, Any]:
        if not _check_kitchen_auth(request, cfg.kitchen_secret):
            raise HTTPException(status_code=403, detail="Invalid kitchen key")
        clean_reason = body.reason.strip()
        if not clean_reason:
            raise HTTPException(status_code=422, detail="Cancel reason is required")
        order = shop_db.cancel_order_by_owner(order_id, clean_reason)
        if order is None:
            raise HTTPException(status_code=404, detail="Order not found or not queued")
        notified = True
        try:
            push = format_cancel_push(order)
            line.push_text(order.line_user_id, push)
            shop_db.append_conversation_event(
                order.line_user_id, f"[ร้านยกเลิกออเดอร์ #{order.queue_number}]", push
            )
        except Exception as exc:
            log.warning("Cancel push failed for order %s: %s", order_id, type(exc).__name__)
            notified = False
        return {"ok": True, "order": order.to_dict(), "notified": notified}

    @app.post("/api/kitchen/orders/{order_id}/payment")
    def kitchen_set_payment(order_id: int, request: Request, body: PaymentBody) -> dict[str, Any]:
        if not _check_kitchen_auth(request, cfg.kitchen_secret):
            raise HTTPException(status_code=403, detail="Invalid kitchen key")
        order, err = shop_db.set_payment_status(order_id, body.status)
        if err == "not_found":
            raise HTTPException(status_code=404, detail="Order not found")
        if order is None:
            raise HTTPException(status_code=409, detail="Payment status cannot change")
        return {"ok": True, "order": order.to_dict()}

    @app.post("/api/kitchen/handovers/{card_id}/resolve")
    def kitchen_resolve_handover(card_id: int, request: Request) -> dict[str, bool]:
        if not _check_kitchen_auth(request, cfg.kitchen_secret):
            raise HTTPException(status_code=403, detail="Invalid kitchen key")
        if not shop_db.resolve_handover(card_id):
            raise HTTPException(status_code=404, detail="Inbox card not found or resolved")
        return {"ok": True}

    @app.post("/api/kitchen/handovers/{card_id}/mark-paid")
    def kitchen_mark_paid(card_id: int, request: Request, body: MarkPaidBody) -> dict[str, Any]:
        """จ่ายแล้ว ✓ on a slip card: the ticked unpaid orders of that customer become
        `transfer_manual` (the owner checked the transfer themselves)."""
        if not _check_kitchen_auth(request, cfg.kitchen_secret):
            raise HTTPException(status_code=403, detail="Invalid kitchen key")
        result = shop_db.mark_transfer_manual(card_id, body.order_ids)
        if result is None:
            raise HTTPException(status_code=409, detail="Not an open slip card")
        marked, skipped = result
        return {"ok": True, "marked": [o.queue_number for o in marked], "skipped": skipped}

    @app.post("/api/kitchen/orders/{order_id}/takeover")
    def kitchen_takeover(order_id: int, request: Request) -> dict[str, Any]:
        """คุยเอง: the owner takes over this customer's chat. Sends the customer nothing."""
        if not _check_kitchen_auth(request, cfg.kitchen_secret):
            raise HTTPException(status_code=403, detail="Invalid kitchen key")
        order = shop_db.get_order(order_id)
        if order is None:
            raise HTTPException(status_code=404, detail="Order not found")
        card_id, is_new = shop_db.open_handover(
            order.line_user_id, "owner_takeover", f"#{order.queue_number}"
        )
        if is_new:
            on_new_card(
                shop_db,
                line,
                cfg,
                order.line_user_id,
                "owner_takeover",
                f"#{order.queue_number}",
                owner_alerts_on=shop_db.get_shop_status(loaded_menu).notify_owner_on_inbox,
            )
        return {"ok": True, "card_id": card_id}

    def _alert_owner_new_order(order: Order) -> None:
        if not shop_db.get_shop_status(loaded_menu).notify_owner_on_order:
            return
        if not cfg.owner_line_user_id:
            return
        try:
            line.push_text(cfg.owner_line_user_id, format_owner_order_alert(order))
        except Exception as exc:
            log.warning("Owner alert push failed for order %s: %s", order.id, type(exc).__name__)

    def _after_turn(user_id: str, turn: AgentTurn | SlipTurn) -> None:
        if turn.new_card_reason is None:
            return
        on_new_card(
            shop_db,
            line,
            cfg,
            user_id,
            turn.new_card_reason,
            turn.new_card_detail,
            owner_alerts_on=shop_db.get_shop_status(loaded_menu).notify_owner_on_inbox,
        )

    def _safe_reply(reply_token: str, messages: list[dict[str, Any]], stats: TurnStats) -> bool:
        if not messages:
            return True  # nothing to send: reply_ok stays unset
        if not reply_token or isinstance(line, NoopLineClient):
            # Can't reply (no token, or no LINE access token configured): nothing is sent
            return False
        try:
            line.reply_messages(reply_token, messages)
            stats.reply_ok = stats.reply_ok is not False
            return True
        except Exception as exc:
            log.warning("LINE reply failed turn_id=%s: %s", stats.turn_id, type(exc).__name__)
            stats.reply_ok = False
            return False

    def _reply_not_delivered(user_id: str, card_id: str | None, reply_text: str | None) -> None:
        """The reply never reached the customer. Its text is already in history, so tell the
        next LLM turn which reply they never saw (AGENTS.md), and cancel any card it carried."""
        if card_id:
            # The customer never got the order card: don't leave it confirmable by a slip
            shop_db.cancel_active_card(user_id, card_id)
        conv = shop_db.get_or_create_conversation(user_id)
        shop_db.append_conversation_messages(
            conv.id, [{"role": "user", "content": undelivered_note(reply_text)}]
        )

    def _process_event_sync(event: dict[str, Any], started_at: float, stats: TurnStats) -> None:
        source = event.get("source")
        user_id = str(source.get("userId") or "") if isinstance(source, dict) else ""
        reply_token = str(event.get("replyToken") or "")
        if not user_id:
            return

        ev_type = event.get("type")

        # Postback buttons (Confirm / Cancel order card) — tapping a button ends bot quiet
        if ev_type == "postback":
            pb = event.get("postback")
            data_str = str(pb.get("data") or "") if isinstance(pb, dict) else ""
            parsed = parse_postback_data(data_str)
            action = parsed.get("action")
            card_id = parsed.get("card_id", "")
            stats.reason = f"postback_{action}" if action in ("confirm", "cancel") else "ignored"
            if action in ("confirm", "cancel") and card_id:
                shop_db.end_quiet_on_button_tap(user_id)
            if action == "confirm" and card_id:
                res = shop_db.confirm_card(user_id, card_id, loaded_menu)
                if res.ok and res.order is not None:
                    st = shop_db.get_shop_status(loaded_menu)
                    # Current intent and current unpaid orders, also for a replayed Confirm tap
                    cash = shop_db.pays_cash(user_id)
                    owed = shop_db.get_unpaid_balance(user_id).total
                    pay_text = (
                        ""
                        if cfg.promptpay_id or owed <= 0  # nothing owed: no payment details
                        else (CASH_PAY_TEXT if cash else cfg.payment_info_text)
                    )
                    receipt = format_order_receipt(
                        res.order,
                        pay_text,
                        already_confirmed=res.already_confirmed,
                        queue_line=receipt_queue_line(shop_db, loaded_menu, res.order),
                    )
                    reply_msgs: list[dict[str, Any]] = []
                    if cfg.promptpay_id:
                        unpaid = shop_db.get_unpaid_balance(user_id)
                        if unpaid.total > 0:
                            today = shop_db.today_str()
                            this_order = res.order if res.order.shop_date == today else None
                            receipt += "\n\n" + format_payment_lines(
                                unpaid.orders, this_order, today, cash=cash
                            )
                            if not cash:
                                reply_msgs.append(image_message(qr_image_url(cfg, unpaid.total)))
                    reply_msgs.insert(0, text_message(receipt))
                    if not res.already_confirmed:
                        confirm_note = (
                            f"[ลูกค้ากดยืนยันออเดอร์ #{res.order.queue_number} บนการ์ดแล้ว ตะกร้าใหม่ว่างแล้ว]"
                        )
                        shop_db.append_conversation_event(
                            user_id,
                            confirm_note,
                            receipt,
                            compact_for_confirmed_order=True,
                        )
                    _safe_reply(reply_token, reply_msgs, stats)
                    if (
                        not res.already_confirmed
                        and st.notify_owner_on_order
                        and cfg.owner_line_user_id
                    ):
                        try:
                            line.push_text(
                                cfg.owner_line_user_id,
                                format_owner_order_alert(res.order),
                            )
                        except Exception as exc:
                            log.warning(
                                "Owner alert push failed for order %s: %s",
                                res.order.id,
                                type(exc).__name__,
                            )
                else:
                    _safe_reply(
                        reply_token,
                        [text_message(res.error or "ไม่สามารถยืนยันออเดอร์ได้ค่ะ")],
                        stats,
                    )
            elif action == "cancel" and card_id:
                cancelled = shop_db.cancel_card_and_clear_items(user_id, card_id)
                msg = (
                    "ยกเลิกสรุปออเดอร์นี้แล้วค่ะ พิมพ์สั่งใหม่ได้เลยนะคะ"
                    if cancelled
                    else "การ์ดนี้ถูกยกเลิกหรือยืนยันไปแล้วค่ะ"
                )
                if cancelled:
                    shop_db.append_conversation_event(
                        user_id, "[ลูกค้ากดปุ่มยกเลิกการ์ดออเดอร์ ตะกร้าว่างแล้ว]", msg
                    )
                if not _safe_reply(reply_token, [text_message(msg)], stats) and cancelled:
                    _reply_not_delivered(user_id, None, msg)  # the cart is still cleared
            return

        if ev_type != "message":
            return

        message = event.get("message")
        if not isinstance(message, dict):
            return
        msg_type = str(message.get("type") or "")

        # Owner LINE text commands (always active, even when bot_enabled=0)
        if cfg.owner_line_user_id and user_id == cfg.owner_line_user_id:
            stats.reason = "owner"
            if msg_type == "text":
                reply_text = handle_owner_text(
                    shop_db, loaded_menu, cfg, str(message.get("text") or "")
                )
                _safe_reply(reply_token, [text_message(reply_text)], stats)
            return

        # Global bot pause stops all automated customer replies (text, image, location)
        st = shop_db.get_shop_status(loaded_menu)
        if not st.bot_enabled:
            stats.reason = "bot_disabled"
            return

        # Ignore stickers and video silently
        if msg_type in ("sticker", "video"):
            return

        # Voice: ask the customer to type (no LLM), silent while the bot is quiet
        if msg_type == "audio":
            turn = handle_audio_message(shop_db, user_id)
            stats.reason = turn.reason
            if turn.reply_text:
                _safe_reply(reply_token, [text_message(turn.reply_text)], stats)
            return

        # Image: slip intake (SlipOK + slip match), otherwise reply once + hand over
        if msg_type == "image":
            stats.reason = "image"
            try:
                slip_turn = handle_image(
                    shop_db,
                    loaded_menu,
                    cfg,
                    line,
                    slips,
                    quota,
                    user_id,
                    str(message.get("id") or ""),
                    started_at=started_at,
                )
            except Exception as exc:
                # Never leave the customer without a reply: fall back to a plain image hand-over
                log.warning("Slip intake failed: %s", type(exc).__name__)
                fallback = handle_image_message(shop_db, user_id)
                slip_turn = SlipTurn(
                    texts=[fallback.reply_text] if fallback.reply_text else [],
                    new_card_reason=fallback.new_card_reason,
                    new_card_detail=fallback.new_card_detail,
                )
            _safe_reply(reply_token, [text_message(t) for t in slip_turn.texts], stats)
            _after_turn(user_id, slip_turn)
            if slip_turn.confirmed_order is not None:
                _alert_owner_new_order(slip_turn.confirmed_order)
            return

        # Location pin -> delivery address + Google Maps URL
        if msg_type == "location":
            lat = float(message.get("latitude", 0.0))
            lng = float(message.get("longitude", 0.0))
            addr = str(message.get("address") or message.get("title") or "")
            turn = handle_location_message(shop_db, loaded_menu, user_id, addr, lat, lng)
            stats.reason = turn.reason
            out: list[dict[str, Any]] = []
            if turn.reply_text:
                out.append(text_message(turn.reply_text))
            if turn.flex_card:
                out.append(turn.flex_card)
            if not _safe_reply(reply_token, out, stats):
                _reply_not_delivered(user_id, turn.card_id, turn.reply_text)
            return

        # Customer text message -> LLM ordering agent
        if msg_type == "text":
            conv_now = shop_db.get_or_create_conversation(user_id)
            if not shop_db.is_handed_over(conv_now):
                line.show_loading(user_id, seconds=20)
            turn = handle_customer_text(
                shop_db,
                loaded_menu,
                user_id,
                str(message.get("text") or ""),
                llm_client=llm,
                settings=cfg,
                started_at=started_at,
                stats=stats,
            )
            stats.card_id = None  # handed back: the reply path below owns the card now
            stats.reason = turn.reason
            out_msgs: list[dict[str, Any]] = []
            if turn.menu_text:
                out_msgs.append(text_message(turn.menu_text))
            if turn.reply_text:
                out_msgs.append(text_message(turn.reply_text))
            if turn.flex_card:
                out_msgs.append(turn.flex_card)
            if turn.image_url:
                out_msgs.append(image_message(turn.image_url))
            if not _safe_reply(reply_token, out_msgs, stats):
                _reply_not_delivered(user_id, turn.card_id, turn.reply_text)
            _after_turn(user_id, turn)

    @app.post("/line/webhook")
    async def line_webhook(request: Request) -> dict[str, bool]:
        # Deadlines count from receipt: earlier events and lock waits use up the reply window
        started_at = time.monotonic()
        if not cfg.line_channel_secret:
            raise HTTPException(status_code=503, detail="LINE_CHANNEL_SECRET is not configured")
        body = await request.body()
        sig = request.headers.get("x-line-signature", "")
        if not verify_signature(body, sig, cfg.line_channel_secret):
            raise HTTPException(status_code=400, detail="Invalid signature")
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON body") from exc

        events = payload.get("events") if isinstance(payload, dict) else None
        if not isinstance(events, list):
            raise HTTPException(status_code=400, detail="Missing events list")

        for event in events:
            if not isinstance(event, dict):
                continue
            stats = TurnStats()
            lock_wait_ms = 0
            try:
                raw_eid = event.get("webhookEventId")
                event_id = str(raw_eid) if isinstance(raw_eid, str) and raw_eid else ""
                if event_id:
                    is_new = await run_in_threadpool(shop_db.record_line_event, event_id)
                    if not is_new:
                        stats.reason = "duplicate"
                        continue
                source = event.get("source")
                user_id = str(source.get("userId") or "") if isinstance(source, dict) else ""
                if not user_id:
                    stats.reason = "no_user"
                    continue
                t_lock = time.monotonic()
                async with _lock_for(user_id):
                    lock_wait_ms = round((time.monotonic() - t_lock) * 1000)
                    try:
                        await run_in_threadpool(_process_event_sync, event, started_at, stats)
                    except Exception as exc:
                        stats.reason = "exception"
                        flush_llm_call(stats, type(exc).__name__)
                        if stats.card_id:  # the turn crashed after saving a card: unsent
                            await run_in_threadpool(
                                shop_db.cancel_active_card, user_id, stats.card_id
                            )
                        log.warning(
                            "Webhook event failed turn_id=%s: %s",
                            stats.turn_id,
                            type(exc).__name__,
                        )
            finally:
                log.info(
                    "webhook_event turn_id=%s event_type=%s reason=%s llm_calls=%d "
                    "lock_wait_ms=%d total_ms=%d reply_ok=%s",
                    stats.turn_id,
                    _event_label(event),
                    stats.reason,
                    stats.llm_calls,
                    lock_wait_ms,
                    round((time.monotonic() - started_at) * 1000),
                    "-" if stats.reply_ok is None else str(stats.reply_ok).lower(),
                )

        return {"ok": True}

    return app


class _LazyASGIApp:
    """Defer create_app() until first ASGI call so importing app.main has no DB side effects."""

    def __init__(self) -> None:
        self._inner: FastAPI | None = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self._inner is None:
            self._inner = create_app()
        await self._inner(scope, receive, send)


app = _LazyASGIApp()
