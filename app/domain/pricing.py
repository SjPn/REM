"""Price / $/m² normalization — never invent, only repair obvious misreads."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from app.domain.market_stats import to_usd

# If total/area yields below this (USD), the stored "price" is almost certainly already $/m².
_RENT_PSM_FLOOR_USD = 3.0
_SALE_PSM_FLOOR_USD = 50.0

_EXPLICIT_PSM_RE = re.compile(
    r"(\d{1,3}(?:[ \u00a0]?\d{3})*(?:[.,]\d+)?|\d+(?:[.,]\d+)?)\s*"
    r"(?:USD|\$|UAH|₴|грн|EUR|€)?\s*/\s*м(?:²|2)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class PriceNorm:
    price: float | None
    currency: str | None
    price_per_sqm: float | None
    reinterpreted_as_psm: bool
    detail: str = ""


def _num(raw: str) -> float | None:
    cleaned = raw.replace("\xa0", "").replace(" ", "").replace(",", ".")
    if re.fullmatch(r"\d{1,3}(\.\d{3})+", cleaned):
        cleaned = cleaned.replace(".", "")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return value


def extract_explicit_psm(text: str | None) -> float | None:
    """First explicit N $/м² (or грн/м²) from listing text."""
    if not text:
        return None
    m = _EXPLICIT_PSM_RE.search(text)
    if not m:
        return None
    return _num(m.group(1))


def psm_floor_usd(deal_type: str | None) -> float:
    if (deal_type or "").lower() == "rent":
        return _RENT_PSM_FLOOR_USD
    return _SALE_PSM_FLOOR_USD


def normalize_listing_price(
    *,
    price: float | None,
    currency: str | None,
    area_sqm: float | None,
    deal_type: str | None,
    price_per_sqm: float | None = None,
    title: str | None = None,
    description: str | None = None,
) -> PriceNorm:
    """
    If price/area is absurdly low in USD, treat `price` as already $/m² and
    expand to total = price * area. Prefer explicit N $/м² from text when present.
    """
    cur = (currency or "USD").upper() if currency else "USD"
    psm_field = price_per_sqm
    text = " ".join(x for x in (title, description) if x)
    explicit = extract_explicit_psm(text)

    if price is None or area_sqm is None:
        if explicit is not None and psm_field is None:
            return PriceNorm(None, currency, explicit, False, "explicit_psm_only")
        return PriceNorm(price, currency, psm_field, False)

    try:
        p = float(price)
        a = float(area_sqm)
    except (TypeError, ValueError):
        return PriceNorm(price, currency, psm_field, False)
    if not math.isfinite(p) or not math.isfinite(a) or p <= 0 or a <= 0:
        return PriceNorm(price, currency, psm_field, False)

    usd = to_usd(p, cur)
    if usd is None or not math.isfinite(usd):
        return PriceNorm(price, currency, psm_field, False)

    implied = usd / a
    floor = psm_floor_usd(deal_type)

    # Explicit marker in title matches stored price → definitely $/m²
    if explicit is not None and abs(explicit - p) / max(p, 1) < 0.02 and implied < floor * 5:
        total = round(p * a, 2)
        return PriceNorm(
            total,
            currency or cur,
            p if psm_field is None else psm_field,
            True,
            "explicit_psm_matches_price",
        )

    if implied < floor:
        total = round(p * a, 2)
        return PriceNorm(
            total,
            currency or cur,
            p if psm_field is None else psm_field,
            True,
            f"implied_psm_usd={implied:.4f}<{floor}",
        )

    if psm_field is None and explicit is not None:
        psm_field = explicit
    return PriceNorm(price, currency, psm_field, False)
