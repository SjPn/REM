from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from dataclasses import dataclass


_ADDR_NOISE = re.compile(
    r"\b(вул\.?|улица|просп\.?|пр-т|бульвар|б-р|провулок|пер\.?|площа|пл\.?|"
    r"київ|киев|kyiv|київська\s+обл\.?|область)\b",
    re.IGNORECASE,
)
_WS = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^0-9a-zа-яіїєґ]+", re.IGNORECASE)


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", value).lower().strip()
    text = text.replace("'", "'").replace("`", "'")
    text = _WS.sub(" ", text)
    return text


def normalize_address(address: str | None) -> str:
    text = normalize_text(address)
    text = _ADDR_NOISE.sub(" ", text)
    text = _NON_ALNUM.sub(" ", text)
    return _WS.sub(" ", text).strip()


def round_area(area: float | None, step: float = 1.0) -> float | None:
    if area is None:
        return None
    return round(round(area / step) * step, 1)


def round_coord(value: float | None, digits: int = 4) -> float | None:
    if value is None:
        return None
    return round(value, digits)


def round_price_band(
    price: float | None,
    deal_type: str | None,
    *,
    currency: str | None = None,
) -> str:
    """Bucket price in USD so ±5–8% still matches; raw UAH vs USD won't split."""
    if price is None:
        return ""
    try:
        p = float(price)
    except (TypeError, ValueError):
        return ""
    if p <= 0:
        return ""
    from app.domain.market_stats import to_usd

    usd = to_usd(p, currency)
    if usd is None or usd <= 0:
        return ""
    pct = 0.08 if (deal_type or "").lower() == "rent" else 0.05
    log_tol = math.log(1 + pct)
    bucket = round(math.log(usd) / log_tol) * log_tol
    band = round(math.exp(bucket))
    return str(int(band)) if band == int(band) else str(band)


@dataclass(frozen=True)
class FingerprintInput:
    address: str | None = None
    area_sqm: float | None = None
    floor: int | None = None
    price: float | None = None
    currency: str | None = None
    property_type: str | None = None  # ignored in hash — portals disagree on segment
    deal_type: str | None = None
    lat: float | None = None  # ignored — appears after detail enrich and splits identity
    lon: float | None = None
    phone: str | None = None


def build_fingerprint(data: FingerprintInput) -> str:
    """Stable cross-source id: address + area + floor + USD price band + deal; phone if weak."""
    addr = normalize_address(data.address)
    area = str(round_area(data.area_sqm) or "")
    floor = str(data.floor if data.floor is not None else "")
    price_band = round_price_band(data.price, data.deal_type, currency=data.currency)
    phone = phone_digits(data.phone) or ""

    # Do NOT include property_type or lat/lon: they flip between list and detail / sources.
    parts = [
        addr,
        area,
        floor,
        price_band,
        normalize_text(data.deal_type),
    ]

    # Phone only when address/area weak — never toggle when geo appears later.
    if phone and (not addr or not area):
        parts.append(phone)

    raw = "|".join(parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def phone_digits(phone: str | None) -> str | None:
    if not phone:
        return None
    digits = re.sub(r"\D+", "", phone)
    if len(digits) < 9:
        return None
    return digits[-10:]
