"""FastAPI application: health check, kitchen board, and LINE webhook."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.types import Receive, Scope, Send

from app.agent import (
    handle_customer_text,
    handle_image_message,
    handle_location_message,
    make_openai_client,
)
from app.config import Settings, load_settings
from app.db import Order, ShopDB
from app.line import (
    HttpLineClient,
    LineClient,
    NoopLineClient,
    format_order_receipt,
    format_owner_order_alert,
    parse_postback_data,
    text_message,
    verify_signature,
)
from app.menu import Menu, load_menu
from app.owner import handle_owner_text

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


class OverrideUpdateBody(BaseModel):
    kind: Literal["dish", "ingredient"]
    key: str
    is_available: bool


class DoneOrderBody(BaseModel):
    notify: bool = False


class CancelOrderBody(BaseModel):
    reason: str = Field(min_length=1)


def format_done_push(order: Order) -> str:
    if order.fulfillment == "pickup":
        return f"ออเดอร์ #{order.queue_number} เสร็จแล้ว มารับได้เลยค่ะ"
    return f"ออเดอร์ #{order.queue_number} กำลังออกไปส่งแล้วค่ะ"


def format_cancel_push(order: Order) -> str:
    reason = order.cancel_reason or "ร้านไม่สะดวกรับออเดอร์"
    return f"ขออภัยค่ะ ออเดอร์ #{order.queue_number} ถูกยกเลิก ({reason})"


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
) -> FastAPI:
    cfg = settings or load_settings()
    loaded_menu = menu or load_menu(cfg.menu_path)
    shop_db = db or ShopDB(cfg.db_path)
    line: LineClient = (
        line_client
        if line_client is not None
        else (
            HttpLineClient(cfg.line_channel_access_token)
            if cfg.line_channel_access_token
            else NoopLineClient()
        )
    )
    llm = llm_client if llm_client is not None else make_openai_client(cfg)
    user_locks: dict[str, asyncio.Lock] = {}

    def _lock_for(user_id: str) -> asyncio.Lock:
        lock = user_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            user_locks[user_id] = lock
        return lock

    app = FastAPI(title="tam-sang-bot")
    app.state.settings = cfg
    app.state.menu = loaded_menu
    app.state.db = shop_db
    app.state.line = line
    app.state.llm = llm

    @app.get("/health")
    def health() -> dict[str, bool]:
        return {"ok": True}

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
        payload = {
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
        order = shop_db.mark_order_done(order_id)
        if order is None:
            raise HTTPException(status_code=404, detail="Order not found or not queued")
        notified: bool | None = None
        if body.notify:
            try:
                line.push_text(order.line_user_id, format_done_push(order))
                notified = True
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
            line.push_text(order.line_user_id, format_cancel_push(order))
        except Exception as exc:
            log.warning("Cancel push failed for order %s: %s", order_id, type(exc).__name__)
            notified = False
        return {"ok": True, "order": order.to_dict(), "notified": notified}

    def _safe_reply(reply_token: str, messages: list[dict[str, Any]], event_id: str) -> None:
        if not messages:
            return
        try:
            line.reply_messages(reply_token, messages)
        except Exception as exc:
            log.warning("LINE reply failed for event %s: %s", event_id, type(exc).__name__)

    def _process_event_sync(event: dict[str, Any], started_at: float, event_id: str) -> None:
        source = event.get("source")
        user_id = str(source.get("userId") or "") if isinstance(source, dict) else ""
        reply_token = str(event.get("replyToken") or "")
        if not user_id:
            return

        ev_type = event.get("type")

        # Postback buttons (Confirm / Cancel order card) — tapping a button clears hand-over
        if ev_type == "postback":
            conv_pb = shop_db.get_or_create_conversation(user_id)
            if shop_db.is_handed_over(conv_pb):
                shop_db.clear_handed_over(conv_pb.id)
            pb = event.get("postback")
            data_str = str(pb.get("data") or "") if isinstance(pb, dict) else ""
            parsed = parse_postback_data(data_str)
            action = parsed.get("action")
            card_id = parsed.get("card_id", "")
            if action == "confirm" and card_id:
                res = shop_db.confirm_card(user_id, card_id, loaded_menu)
                if res.ok and res.order is not None:
                    st = shop_db.get_shop_status(loaded_menu)
                    receipt = format_order_receipt(
                        res.order,
                        cfg.payment_info_text,
                        already_confirmed=res.already_confirmed,
                        wait_minutes=st.wait_minutes if st.busy_mode else None,
                    )
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
                    _safe_reply(reply_token, [text_message(receipt)], event_id)
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
                        event_id,
                    )
            elif action == "cancel" and card_id:
                cancelled = shop_db.cancel_active_card(user_id, card_id)
                msg = (
                    "ยกเลิกสรุปออเดอร์นี้แล้วค่ะ พิมพ์สั่งใหม่ได้เลยนะคะ"
                    if cancelled
                    else "การ์ดนี้ถูกยกเลิกหรือยืนยันไปแล้วค่ะ"
                )
                if cancelled:
                    shop_db.append_conversation_event(user_id, "[ลูกค้ากดปุ่มยกเลิกการ์ดออเดอร์]", msg)
                _safe_reply(reply_token, [text_message(msg)], event_id)
            return

        if ev_type != "message":
            return

        message = event.get("message")
        if not isinstance(message, dict):
            return
        msg_type = str(message.get("type") or "")

        # Owner LINE text commands (always active, even when bot_enabled=0)
        if cfg.owner_line_user_id and user_id == cfg.owner_line_user_id:
            if msg_type == "text":
                reply_text = handle_owner_text(
                    shop_db, loaded_menu, cfg, str(message.get("text") or "")
                )
                _safe_reply(reply_token, [text_message(reply_text)], event_id)
            return

        # Global bot pause stops all automated customer replies (text, image, location)
        st = shop_db.get_shop_status(loaded_menu)
        if not st.bot_enabled:
            return

        # Ignore stickers, audio, video silently
        if msg_type in ("sticker", "audio", "video"):
            return

        # Image (usually payment slip): skip LLM, reply once + hand over
        if msg_type == "image":
            turn = handle_image_message(shop_db, user_id)
            if turn.reply_text:
                _safe_reply(reply_token, [text_message(turn.reply_text)], event_id)
            return

        # Location pin -> delivery address + Google Maps URL
        if msg_type == "location":
            lat = float(message.get("latitude", 0.0))
            lng = float(message.get("longitude", 0.0))
            addr = str(message.get("address") or message.get("title") or "")
            turn = handle_location_message(shop_db, loaded_menu, user_id, addr, lat, lng)
            out: list[dict[str, Any]] = []
            if turn.reply_text:
                out.append(text_message(turn.reply_text))
            if turn.flex_card:
                out.append(turn.flex_card)
            _safe_reply(reply_token, out, event_id)
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
            )
            out_msgs: list[dict[str, Any]] = []
            if turn.reply_text:
                out_msgs.append(text_message(turn.reply_text))
            if turn.flex_card:
                out_msgs.append(turn.flex_card)
            _safe_reply(reply_token, out_msgs, event_id)

    @app.post("/line/webhook")
    async def line_webhook(request: Request) -> dict[str, bool]:
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
            raw_eid = event.get("webhookEventId")
            event_id = str(raw_eid) if isinstance(raw_eid, str) and raw_eid else ""
            if event_id:
                is_new = await run_in_threadpool(shop_db.record_line_event, event_id)
                if not is_new:
                    continue
            source = event.get("source")
            user_id = str(source.get("userId") or "") if isinstance(source, dict) else ""
            if not user_id:
                continue
            started_at = time.monotonic()
            async with _lock_for(user_id):
                try:
                    await run_in_threadpool(_process_event_sync, event, started_at, event_id)
                except Exception as exc:
                    log.warning(
                        "Webhook event %s failed: %s",
                        event_id or "no-id",
                        type(exc).__name__,
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
