"""Price / $/m² normalization — never invent, only repair obvious misreads."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from app.domain.market_stats import to_usd

# Rent $/m²·мес: below floor → likely the "price" is already a rate; above ceiling → outlier.
_RENT_PSM_FLOOR_USD = 3.0
_RENT_PSM_CEILING_USD = 50.0
# Sale: below this implied $/m² the stored figure is almost certainly already $/m².
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
    suspicious_psm: bool = False
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


def rent_psm_suspicious(psm_usd: float | None) -> bool:
    if psm_usd is None or not math.isfinite(psm_usd):
        return False
    return psm_usd > _RENT_PSM_CEILING_USD


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
    Repair only when the stored price is almost certainly already $/m².

    Rent: expand only if implied $/m² < $3 AND the rate itself is in a plausible
    band (≤ $50/м²) or an explicit N $/м² in the text matches the stored price.
    Never multiply a full monthly total that already yields a sane $/m².
    """
    from app.scrapers.http_utils import parse_price, parse_price_per_sqm

    cur = (currency or "USD").upper() if currency else "USD"
    psm_field = price_per_sqm
    text = " ".join(x for x in (title, description) if x)
    explicit = extract_explicit_psm(text)
    is_rent = (deal_type or "").lower() == "rent"

    # Prefer structured parse from card text when both total and rate are present
    parsed_total, parsed_cur = parse_price(text) if text else (None, None)
    parsed_psm, _ = parse_price_per_sqm(text) if text else (None, None)
    if parsed_psm is not None:
        psm_field = psm_field or parsed_psm
        if explicit is None:
            explicit = parsed_psm

    if (
        parsed_total is not None
        and parsed_psm is not None
        and area_sqm is not None
        and float(area_sqm) > 0
    ):
        # Text wins: "25 000 $/міс 15 $/м²"
        total_usd = to_usd(float(parsed_total), parsed_cur or cur)
        psm_usd = (total_usd / float(area_sqm)) if total_usd else None
        return PriceNorm(
            float(parsed_total),
            parsed_cur or currency or cur,
            float(parsed_psm),
            False,
            suspicious_psm=is_rent and rent_psm_suspicious(psm_usd),
            detail="text_total_and_psm",
        )

    if price is None or area_sqm is None:
        if explicit is not None and psm_field is None:
            return PriceNorm(None, currency, explicit, False, False, "explicit_psm_only")
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
    suspicious = is_rent and rent_psm_suspicious(implied)

    # Explicit N $/м² equals stored price, and implied total/area is nonsense-low
    if (
        explicit is not None
        and abs(explicit - p) / max(p, 1.0) < 0.05
        and implied < floor
    ):
        total = round(p * a, 2)
        return PriceNorm(
            total,
            currency or cur,
            p if psm_field is None else psm_field,
            True,
            False,
            "explicit_psm_matches_price",
        )

    # Expand only when implied $/m² is absurdly low AND rate looks like a real $/m²
    if implied < floor:
        rate_ok = True
        if is_rent and usd > _RENT_PSM_CEILING_USD:
            # e.g. 31 196 mistaken as rate — do not multiply by area
            rate_ok = False
        if rate_ok:
            total = round(p * a, 2)
            return PriceNorm(
                total,
                currency or cur,
                p if psm_field is None else psm_field,
                True,
                False,
                f"implied_psm_usd={implied:.4f}<{floor}",
            )
        return PriceNorm(
            price,
            currency,
            psm_field or explicit,
            False,
            True,
            "skip_expand_rate_above_rent_ceiling",
        )

    if psm_field is None and explicit is not None:
        psm_field = explicit
    return PriceNorm(price, currency, psm_field, False, suspicious, "")
