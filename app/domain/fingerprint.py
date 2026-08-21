from __future__ import annotations

import hashlib
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
    text = text.replace("’", "'").replace("`", "'")
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


@dataclass(frozen=True)
class FingerprintInput:
    address: str | None = None
    area_sqm: float | None = None
    floor: int | None = None
    property_type: str | None = None
    deal_type: str | None = None
    lat: float | None = None
    lon: float | None = None
    phone: str | None = None


def build_fingerprint(data: FingerprintInput) -> str:
    """Stable identity for cross-source property matching.

    Phone is a soft signal: included only together with address/area to avoid
    collapsing unrelated listings of the same agent.
    """
    addr = normalize_address(data.address)
    area = str(round_area(data.area_sqm) or "")
    phone = phone_digits(data.phone) or ""
    parts = [
        addr,
        area,
        str(data.floor if data.floor is not None else ""),
        normalize_text(data.property_type),
        normalize_text(data.deal_type),
        str(round_coord(data.lat) or ""),
        str(round_coord(data.lon) or ""),
    ]
    # Prefer geo+address identity; append phone only when location is weak.
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
