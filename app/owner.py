"""Owner LINE text command handler (free reply tokens, deterministic Thai parsing)."""

from __future__ import annotations

import re

from app.config import Settings
from app.db import ShopDB
from app.menu import Menu

HELP_TEXT = (
    "คำสั่งเจ้าของร้าน:\n"
    "• ของหมด/มาแล้ว: 'หมูกรอบหมด', 'หมูกรอบมาแล้ว', 'เปิดครบ'\n"
    "• โหมดยุ่ง/ปกติ: 'ยุ่ง 25 นาที', 'ยุ่ง', 'โหมดปกติ'\n"
    "• เปิด/ปิดร้าน: 'ปิดร้าน', 'เปิดร้าน'\n"
    "• บอท/แจ้งเตือน: 'หยุดบอท', 'เปิดบอท', 'เปิดแจ้งเตือน', 'ปิดแจ้งเตือน'\n"
    "• ดูงาน: 'คิว', 'สรุปวันนี้'"
)


def handle_owner_text(db: ShopDB, menu: Menu, settings: Settings, text: str) -> str:
    """Execute a natural Thai owner command and return the LINE reply text."""
    raw = text.strip()
    compact = re.sub(r"\s+", "", raw)

    # Clear all sold-out overrides
    if compact in ("เปิดครบ", "ของครบ", "มีครบ", "เปิดหมด", "รีเซ็ตของหมด"):
        db.clear_all_overrides()
        return "เปิดขายทุกรายการครบแล้วค่ะ"

    # Owner push alerts toggle
    if compact in ("เปิดแจ้งเตือน", "เปิดแจ้งเตือนออเดอร์"):
        db.set_notify_owner(True)
        return "เปิดแจ้งเตือนออเดอร์เข้าทาง LINE แล้วค่ะ (ใช้โควตา push 1 ข้อความต่อออเดอร์)"
    if compact in ("ปิดแจ้งเตือน", "ปิดแจ้งเตือนออเดอร์"):
        db.set_notify_owner(False)
        return "ปิดแจ้งเตือนออเดอร์เข้าทาง LINE แล้วค่ะ (ดูคิวบนหน้าจอครัวได้ตามปกติ)"

    # Global bot pause/resume
    if compact in ("หยุดบอท", "ปิดบอท"):
        db.set_bot_enabled(False)
        return "หยุดบอทตอบลูกค้าแล้วค่ะ (พิมพ์ 'เปิดบอท' เมื่อพร้อมให้บอทกลับมาตอบ)"
    if compact in ("เปิดบอท", "เริ่มบอท"):
        db.set_bot_enabled(True)
        return "เปิดบอทตอบลูกค้าแล้วค่ะ"

    # Open / Close same-day override
    if compact in ("ปิดร้าน", "ปิดรับชั่วคราว", "ปิดรับออเดอร์"):
        db.set_open_override("closed")
        return "ปิดรับออเดอร์สำหรับวันนี้แล้วค่ะ (พรุ่งนี้จะกลับมาเปิดตามเวลาปกติอัตโนมัติ)"
    if compact in ("เปิดร้าน", "เปิดรับออเดอร์"):
        db.set_open_override("open")
        return "เปิดรับออเดอร์แล้วค่ะ"

    # Normal / Busy mode
    if compact in ("โหมดปกติ", "หายยุ่งแล้ว", "หายยุ่ง", "ปกติ", "เลิกยุ่ง"):
        db.set_busy_mode(False)
        return "กลับสู่โหมดปกติแล้วค่ะ"
    if compact.startswith(("ยุ่ง", "โหมดยุ่ง", "คิวยุ่ง")) or re.match(
        r"^คิว\d+นาที$", compact
    ):
        match = re.search(r"(\d+)", raw)
        mins = int(match.group(1)) if match else 20
        mins = max(5, min(mins, 180))
        db.set_busy_mode(True, mins)
        return f"เปิดโหมดยุ่งแล้วค่ะ ระบบจะแจ้งลูกค้าว่ารอประมาณ {mins} นาที"

    # Queue count + kitchen link
    if compact in ("คิว", "ออเดอร์", "ดูคิว", "ดูออเดอร์"):
        data = db.get_kitchen_orders_today()
        q_count = int(data["stats"]["queued_count"])
        board_url = f"{settings.public_base_url}/kitchen?key={settings.kitchen_secret}"
        return f"ตอนนี้มีออเดอร์รอทำ {q_count} คิวค่ะ\nหน้าจอครัว: {board_url}"

    # Daily sales summary
    if compact in ("สรุปวันนี้", "ยอดวันนี้", "สรุปยอด", "ยอดขายวันนี้"):
        data = db.get_kitchen_orders_today()
        st = data["stats"]
        return (
            f"สรุปวันนี้ ({data['shop_date']})\n"
            f"• ทำเสร็จแล้ว: {st['done_count']} ออเดอร์\n"
            f"• ยอดขายรวม: {st['done_total']} บาท\n"
            f"• ยกเลิก: {st['cancelled_count']} ออเดอร์\n"
            f"• รอทำอยู่: {st['queued_count']} ออเดอร์"
        )

    # Sold-out toggle: "<name>หมด" or "ปิด<name>"
    sold_match = re.match(r"^(?:ปิดเมนู|ปิด)?(.+?)หมด(?:แล้ว)?$", compact) or re.match(
        r"^ปิด(?:เมนู)?(.+)$", compact
    )
    if sold_match:
        target = menu.find_target(sold_match.group(1))
        if target is not None:
            kind, key, name_th = target
            db.set_override(kind, key, False)
            return f"ปิด '{name_th}' (หมด) แล้วค่ะ"

    # Restock toggle: "<name>มาแล้ว", "มี<name>แล้ว", "เปิด<name>"
    restock_match = (
        re.match(r"^(.+?)(?:มาแล้ว|มีแล้ว|พร้อมขาย)$", compact)
        or re.match(r"^มี(.+?)แล้ว$", compact)
        or re.match(r"^เปิด(?:เมนู|ขาย)?(.+)$", compact)
    )
    if restock_match:
        target = menu.find_target(restock_match.group(1))
        if target is not None:
            kind, key, name_th = target
            db.set_override(kind, key, True)
            return f"เปิดขาย '{name_th}' แล้วค่ะ"

    return HELP_TEXT
