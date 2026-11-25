"""Wait estimate and queue position (SPEC §3.3).

Wait estimate = (meal boxes in queued orders ahead + this order's meal boxes) x minutes_per_box,
never below min_wait, and never below the owner's minimum wait while busy mode is on today.
Shown as a 5-minute range. Never pushed to customers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.db import Order, ShopDB
from app.menu import Menu

DELIVERY_SUFFIX = " (ไม่รวมเวลาส่ง)"


def estimate_minutes(menu: Menu, boxes_ahead: int, own_boxes: int, minimum: int | None) -> int:
    auto = max((boxes_ahead + own_boxes) * menu.wait.minutes_per_box, menu.wait.min_wait)
    return max(auto, minimum or 0)


def wait_range(minutes: int) -> tuple[int, int]:
    lower = minutes // 5 * 5
    return lower, lower + 5


def range_text(minutes: int, *, delivery: bool) -> str:
    lower, upper = wait_range(minutes)
    return f"ประมาณ {lower}–{upper} นาที" + (DELIVERY_SUFFIX if delivery else "")


def _minimum(db: ShopDB, menu: Menu) -> int | None:
    status = db.get_shop_status(menu)
    return status.wait_minutes if status.busy_mode else None


def _queue(db: ShopDB) -> list[Order]:
    """Every queued order, any shop day, oldest first (GLOSSARY: queue)."""
    return db.get_queued_orders()


def new_order_minutes(db: ShopDB, menu: Menu, lines: list[dict[str, Any]]) -> int:
    """Estimate for an order card that isn't confirmed yet: every queued order is ahead."""
    ahead = sum(menu.meal_boxes(o.items) for o in _queue(db))
    return estimate_minutes(menu, ahead, menu.meal_boxes(lines), _minimum(db, menu))


def one_box_minutes(db: ShopDB, menu: Menu) -> int:
    """Estimate for one meal box ordered now (for the LLM's live-state message)."""
    ahead = sum(menu.meal_boxes(o.items) for o in _queue(db))
    return estimate_minutes(menu, ahead, 1, _minimum(db, menu))


@dataclass(frozen=True)
class QueueInfo:
    position: int
    orders_ahead: int
    minutes: int

    def range_str(self) -> str:
        lower, upper = wait_range(self.minutes)
        return f"{lower}–{upper}"


def queue_info(db: ShopDB, menu: Menu, order: Order) -> QueueInfo | None:
    """Queue position (orders ahead + 1) and wait estimate for a queued order.

    None for pre-v2 (`legacy`) orders: they are not part of the queue (SPEC §3.9 item 1).
    """
    if order.status != "queued" or order.payment_status == "legacy":
        return None
    ahead = [o for o in _queue(db) if (o.created_at, o.id) < (order.created_at, order.id)]
    boxes_ahead = sum(menu.meal_boxes(o.items) for o in ahead)
    minutes = estimate_minutes(menu, boxes_ahead, menu.meal_boxes(order.items), _minimum(db, menu))
    return QueueInfo(position=len(ahead) + 1, orders_ahead=len(ahead), minutes=minutes)


def receipt_queue_line(db: ShopDB, menu: Menu, order: Order) -> str | None:
    info = queue_info(db, menu, order)
    if info is None:
        return None
    return f"คิวที่ {info.position} · " + range_text(
        info.minutes, delivery=order.fulfillment == "delivery"
    )


def card_wait_line(db: ShopDB, menu: Menu, snapshot: dict[str, Any]) -> str:
    minutes = new_order_minutes(db, menu, list(snapshot["priced_lines"]))
    return "ตอนนี้รอ" + range_text(minutes, delivery=snapshot["fulfillment"] == "delivery")
