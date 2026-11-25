"""Slip intake helpers: QR decoding, slip mini-QR parsing, SlipOK client, quota cache, slip match.

Run `python -m app.slips <dir>` (via `./scripts/dev.sh slips <dir>`) to check real slip images
locally. It makes no API calls.
"""

from __future__ import annotations

import io
import logging
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import httpx
import zxingcpp
from PIL import Image

from app.promptpay import crc16_ccitt_false

log = logging.getLogger("app.slips")
BKK = ZoneInfo("Asia/Bangkok")

SLIPOK_CHECK_URL = "https://api.slipok.com/api/line/apikey/{branch_id}"
SLIPOK_QUOTA_URL = "https://api.slipok.com/api/line/apikey/{branch_id}/quota"

# SlipOK error codes (SPEC §3.7; docs v1.8: top-level numeric `code`, HTTP 400)
SLIPOK_DUPLICATE = 1012
SLIPOK_WRONG_RECEIVER = 1014
SLIPOK_PACKAGE_OVER_QUOTA = 1004

QUOTA_WARN_BELOW = 20
QUOTA_TTL = timedelta(hours=1)


# --- Slip-verification mini-QR (EMV-style TLV) ---


@dataclass(frozen=True)
class SlipQR:
    raw: str
    bank_code: str
    trans_ref: str
    crc_ok: bool


def _parse_tlv(data: str) -> list[tuple[str, str]] | None:
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(data):
        if i + 4 > len(data) or not data[i : i + 4].isdigit():
            return None
        tag, length = data[i : i + 2], int(data[i + 2 : i + 4])
        value = data[i + 4 : i + 4 + length]
        if len(value) != length:
            return None
        out.append((tag, value))
        i += 4 + length
    return out


def parse_slip_qr(text: str) -> SlipQR | None:
    """Tag 00 nested (00 API ID 000001, 01 sending bank, 02 transaction ref), 51 TH, 91 CRC.

    The CRC is reported, not enforced: SlipOK verifies the slip itself.
    """
    if not text.isascii():
        return None
    fields = _parse_tlv(text.strip())
    if not fields:
        return None
    top = dict(fields)
    nested = _parse_tlv(top.get("00", ""))
    if not nested or top.get("51") != "TH" or "91" not in top:
        return None
    sub = dict(nested)
    if sub.get("00") != "000001" or not sub.get("02"):
        return None
    raw = text.strip()
    crc_ok = raw[-4:].upper() == crc16_ccitt_false(raw[:-4])
    return SlipQR(raw=raw, bank_code=sub.get("01", ""), trans_ref=sub["02"], crc_ok=crc_ok)


def build_slip_qr(bank_code: str, trans_ref: str) -> str:
    """Build a synthetic slip mini-QR (tests and fixtures only)."""

    def tlv(tag: str, value: str) -> str:
        return f"{tag}{len(value):02d}{value}"

    body = tlv("00", tlv("00", "000001") + tlv("01", bank_code) + tlv("02", trans_ref))
    body += tlv("51", "TH") + "9104"
    return body + crc16_ccitt_false(body)


def decode_qr_texts(image_bytes: bytes) -> list[str]:
    """All QR code texts found in an image; [] if none or the image can't be read."""
    try:
        image = Image.open(io.BytesIO(image_bytes))
        image.load()
    except Exception:
        return []
    try:
        results = zxingcpp.read_barcodes(image)
    except Exception:
        return []
    return [r.text for r in results if r.text]


def find_slip_qr(image_bytes: bytes) -> tuple[bool, SlipQR | None]:
    """(image had any QR, parsed slip QR or None). With several slip QRs, the first one wins."""
    texts = decode_qr_texts(image_bytes)
    for t in texts:
        parsed = parse_slip_qr(t)
        if parsed is not None:
            return True, parsed
    return bool(texts), None


# --- SlipOK ---


@dataclass(frozen=True)
class SlipCheck:
    ok: bool
    amount_satang: int | None = None
    transfer_at: datetime | None = None
    trans_ref: str | None = None
    error_code: int | None = None


class SlipClient(Protocol):
    def check(self, qr_data: str) -> SlipCheck: ...
    def quota(self) -> int | None: ...


def _parse_transfer_at(data: dict[str, Any]) -> datetime | None:
    ts = data.get("transTimestamp")
    if isinstance(ts, str) and ts:
        try:
            parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=BKK)
        except ValueError:
            pass
    date_raw, time_raw = data.get("transDate"), data.get("transTime")
    if isinstance(date_raw, str) and isinstance(time_raw, str):
        try:
            return datetime.strptime(f"{date_raw} {time_raw}", "%Y%m%d %H:%M:%S").replace(
                tzinfo=BKK
            )
        except ValueError:
            return None
    return None


def _exact_satang(amount: Any) -> int | None:
    """Positive amount with at most 2 decimals, in satang; None for anything else."""
    if isinstance(amount, bool) or not isinstance(amount, int | float | str):
        return None
    try:
        value = Decimal(str(amount))
    except InvalidOperation:
        return None
    if not value.is_finite() or value <= 0:
        return None
    satang = value * 100
    if satang != satang.to_integral_value():
        return None
    return int(satang)


def parse_slipok_response(status_code: int, body: Any) -> SlipCheck:
    if not isinstance(body, dict):
        return SlipCheck(ok=False)
    data = body.get("data")
    if status_code == 200 and body.get("success") and isinstance(data, dict):
        satang = _exact_satang(data.get("amount"))
        return SlipCheck(
            ok=satang is not None,
            amount_satang=satang,
            transfer_at=_parse_transfer_at(data),
            trans_ref=str(data.get("transRef") or "") or None,
        )
    code = body.get("code")
    if isinstance(code, str) and code.isdigit():
        code = int(code)
    # Errors such as 1014 (wrong receiver) may still carry the slip data: keep it for the
    # owner's card, but `ok=False` keeps the slip ineligible for payment.
    return SlipCheck(
        ok=False,
        amount_satang=_exact_satang(data.get("amount")) if isinstance(data, dict) else None,
        transfer_at=_parse_transfer_at(data) if isinstance(data, dict) else None,
        error_code=code if isinstance(code, int) else None,
    )


class HttpSlipOkClient:
    def __init__(self, api_key: str, branch_id: str, timeout_s: float = 10.0) -> None:
        self._api_key = api_key
        self._branch_id = branch_id
        self._timeout_s = timeout_s

    def check(self, qr_data: str) -> SlipCheck:
        with httpx.Client(timeout=self._timeout_s) as client:
            resp = client.post(
                SLIPOK_CHECK_URL.format(branch_id=self._branch_id),
                headers={"x-authorization": self._api_key},
                json={"data": qr_data, "log": True},
            )
        try:
            body = resp.json()
        except ValueError:
            body = None
        return parse_slipok_response(resp.status_code, body)

    def quota(self) -> int | None:
        with httpx.Client(timeout=self._timeout_s) as client:
            resp = client.get(
                SLIPOK_QUOTA_URL.format(branch_id=self._branch_id),
                headers={"x-authorization": self._api_key},
            )
        body = resp.json()
        if resp.status_code != 200:
            code = body.get("code") if isinstance(body, dict) else None
            log.warning("SlipOK quota read refused: HTTP %s, code=%s", resp.status_code, code)
        data = body.get("data") if isinstance(body, dict) else None
        quota = data.get("quota") if isinstance(data, dict) else None
        return int(quota) if isinstance(quota, int | float) else None


def validate_slipok_settings(api_key: str, branch_id: str) -> None:
    """Fail fast on a half-set or swapped SlipOK config (the branch ID is a number)."""
    if not api_key and not branch_id:
        return
    if not api_key or not branch_id:
        raise ValueError("Set both SLIPOK_API_KEY and SLIPOK_BRANCH_ID, or neither")
    if not (branch_id.isascii() and branch_id.isdigit()):
        raise ValueError(
            "SLIPOK_BRANCH_ID must be the numeric branch ID "
            "(are SLIPOK_API_KEY and SLIPOK_BRANCH_ID swapped?)"
        )


class QuotaCache:
    """SlipOK quota, read at most once an hour (SPEC §3.1.9)."""

    def __init__(self, client: SlipClient | None, clock: Callable[[], datetime]) -> None:
        self._client = client
        self._clock = clock
        self._value: int | None = None
        self._read_at: datetime | None = None
        # Serializes refresh with intake updates, so a slow refresh can't overwrite a newer 0
        self._lock = threading.Lock()

    def get(self) -> int | None:
        if self._client is None:
            return None
        with self._lock:
            now = self._clock()
            if self._read_at is None or now - self._read_at >= QUOTA_TTL:
                self._read_at = now
                try:
                    self._value = self._client.quota()
                except Exception as exc:
                    log.warning("SlipOK quota read failed: %s", type(exc).__name__)
            return self._value

    def used_one(self) -> None:
        with self._lock:
            if self._value is not None and self._value > 0:
                self._value -= 1

    def mark_exhausted(self) -> None:
        with self._lock:
            self._value = 0
            self._read_at = self._clock()


# --- Slip match (SPEC §3.1.6) ---


def match_slip[K](items: Sequence[tuple[K, int]], amount_satang: int) -> list[K] | None:
    """`items` are (key, total in satang), oldest first, with the open order card last.

    Pays the first run of oldest items adding up exactly to the amount, otherwise the oldest
    single item equal to it; None when neither exists. A slip is never split.
    """
    running = 0
    for i, (_, total) in enumerate(items):
        running += total
        if running == amount_satang:
            return [k for k, _ in items[: i + 1]]
        if running > amount_satang:
            break
    for key, total in items:
        if total == amount_satang:
            return [key]
    return None


def format_baht(satang: int) -> str:
    return str(satang // 100) if satang % 100 == 0 else f"{satang / 100:.2f}"


def _cli(folder: str) -> int:
    paths = sorted(p for p in Path(folder).iterdir() if p.is_file() and not p.name.startswith("."))
    if not paths:
        print(f"No images in {folder}")
        return 1
    for path in paths:
        texts = decode_qr_texts(path.read_bytes())
        parsed = next((q for q in (parse_slip_qr(t) for t in texts) if q is not None), None)
        if parsed is None:
            print(f"{path.name}: qr_found={bool(texts)} parsed=no")
        else:
            ref = parsed.trans_ref
            short = f"{ref[:4]}…{ref[-4:]}" if len(ref) > 8 else ref
            print(
                f"{path.name}: parsed=yes bank={parsed.bank_code} ref={short} "
                f"crc_ok={parsed.crc_ok}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(_cli(sys.argv[1] if len(sys.argv) > 1 else "local/slips"))
