"""PromptPay (Thai QR, EMVCo merchant-presented) payload, CRC and PNG, plus signed QR URLs."""

from __future__ import annotations

import hashlib
import hmac
import io
import re
from urllib.parse import urlparse

import segno

from app.config import Settings

PROMPTPAY_AID = "A000000677010111"


def _tlv(tag: str, value: str) -> str:
    return f"{tag}{len(value):02d}{value}"


def crc16_ccitt_false(data: str) -> str:
    """CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF), as 4 upper-case hex digits."""
    crc = 0xFFFF
    for byte in data.encode("ascii"):
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else (crc << 1)
            crc &= 0xFFFF
    return f"{crc:04X}"


def _target(promptpay_id: str) -> tuple[str, str]:
    """(sub-tag, value): `01` mobile as 0066 + 9 digits, or `02` 13-digit national/tax ID."""
    if not re.fullmatch(r"[0-9 -]+", promptpay_id, re.ASCII):
        raise ValueError("PROMPTPAY_ID may contain only digits, spaces and dashes")
    digits = re.sub(r"[^0-9]", "", promptpay_id)
    if len(digits) == 13:
        return "02", digits
    if len(digits) == 10 and digits.startswith("0"):
        return "01", "0066" + digits[1:]
    raise ValueError("PROMPTPAY_ID must be a 10-digit phone number or a 13-digit ID")


def build_payload(promptpay_id: str, amount_baht: float | None = None) -> str:
    """Field order matches the established generators (00, 01, 29, 58, 53, 54, 63)."""
    sub_tag, value = _target(promptpay_id)
    merchant = _tlv("00", PROMPTPAY_AID) + _tlv(sub_tag, value)
    body = (
        _tlv("00", "01")
        + _tlv("01", "12" if amount_baht is not None else "11")
        + _tlv("29", merchant)
        + _tlv("58", "TH")
        + _tlv("53", "764")
    )
    if amount_baht is not None:
        body += _tlv("54", f"{amount_baht:.2f}")
    body += "6304"
    return body + crc16_ccitt_false(body)


def render_png(payload: str) -> bytes:
    buf = io.BytesIO()
    segno.make(payload, error="m", micro=False).save(buf, kind="png", scale=8, border=4)
    return buf.getvalue()


def _sign(settings: Settings, amount_satang: int) -> str:
    key = f"qr|{settings.kitchen_secret}|{settings.line_channel_secret}".encode()
    return hmac.new(key, f"{amount_satang}".encode(), hashlib.sha256).hexdigest()[:24]


def qr_image_url(settings: Settings, amount_baht: int) -> str:
    """Unguessable URL with no customer data: only the amount and an HMAC signature."""
    satang = amount_baht * 100
    return f"{settings.public_base_url}/qr/{satang}-{_sign(settings, satang)}.png"


_QR_NAME_RE = re.compile(r"^([0-9]{1,9})-([0-9a-f]{24})$", re.ASCII)


def parse_qr_name(settings: Settings, name: str) -> int | None:
    """Amount in satang from a signed `/qr/{satang}-{sig}.png` name, or None if invalid."""
    m = _QR_NAME_RE.match(name)
    if m is None:
        return None
    satang = int(m.group(1))
    if satang <= 0 or not hmac.compare_digest(_sign(settings, satang), m.group(2)):
        return None
    return satang


def validate_promptpay_settings(settings: Settings) -> None:
    """Fail fast: LINE needs HTTPS image URLs, and the QR signature needs a real secret."""
    if not settings.promptpay_id:
        return
    _target(settings.promptpay_id)
    url = urlparse(settings.public_base_url)
    if url.scheme != "https" or not url.hostname or url.query or url.fragment:
        raise ValueError(
            "PROMPTPAY_ID needs PUBLIC_BASE_URL to be an https:// URL with a host "
            "and no query or fragment"
        )
    if len(settings.kitchen_secret) < 8:
        raise ValueError("PROMPTPAY_ID needs KITCHEN_SECRET (8+ characters) to sign QR URLs")
