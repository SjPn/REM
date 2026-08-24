"""Price / $/m² normalization — never invent, only repair obvious misreads."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from app.domain.market_stats import to_usd

# Rent $/m²·мес: below floor → likely the "price" is already a rate; above ceiling → outlier.
_RENT_PSM_FLOOR_USD = 3.0
_RENT_PSM_CEILING_USD = 70.0
# Sale: expand only when implied $/m² is absurdly low (price field is clearly a rate).
_SALE_PSM_FLOOR_USD = 80.0
# Plausible Kyiv commercial sale band ($/м²). Keep in sync with market_stats._SALE_PSM_*.
_SALE_PSM_MIN_USD = 450.0
_SALE_PSM_MAX_USD = 10_000.0

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


def sale_psm_suspicious(psm_usd: float | None) -> bool:
    """Commercial sale: <~$450/м² or >~$10k/м² is almost certainly a bad parse."""
    if psm_usd is None or not math.isfinite(psm_usd):
        return False
    return psm_usd < _SALE_PSM_MIN_USD or psm_usd > _SALE_PSM_MAX_USD


def psm_suspicious(deal_type: str | None, psm_usd: float | None) -> bool:
    if (deal_type or "").lower() == "rent":
        return rent_psm_suspicious(psm_usd)
    if (deal_type or "").lower() == "sale":
        return sale_psm_suspicious(psm_usd)
    return False


def implied_psm_native(
    price: float | None,
    currency: str | None,
    area_sqm: float | None,
) -> float | None:
    if price is None or area_sqm is None:
        return None
    try:
        p, a = float(price), float(area_sqm)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(p) or not math.isfinite(a) or p <= 0 or a <= 0:
        return None
    return round(p / a, 4)


def implied_psm_usd(
    price: float | None,
    currency: str | None,
    area_sqm: float | None,
) -> float | None:
    native = implied_psm_native(price, currency, area_sqm)
    if native is None:
        return None
    return to_usd(native, currency)


def maybe_fix_rent_currency(
    price: float | None,
    currency: str | None,
    area_sqm: float | None,
    deal_type: str | None,
    *,
    title: str | None = None,
    description: str | None = None,
) -> tuple[float | None, str | None]:
    """
    LUN/DOM.RIA cards often store UAH totals with currency=USD.
    If USD $/m² is absurd but the same number as UAH yields sane rent, relabel.
    """
    if (deal_type or "").lower() != "rent" or price is None or area_sqm is None:
        return price, currency
    cur = (currency or "USD").upper()
    try:
        p, a = float(price), float(area_sqm)
    except (TypeError, ValueError):
        return price, currency
    if not math.isfinite(p) or not math.isfinite(a) or p <= 0 or a <= 0:
        return price, currency

    text = " ".join(x for x in (title, description) if x).lower()
    if any(tok in text for tok in ("грн", "uah", "₴")):
        return p, "UAH"

    usd_psm = implied_psm_usd(p, cur, a)
    if usd_psm is None or usd_psm <= _RENT_PSM_CEILING_USD:
        return p, cur
    if cur != "USD":
        return p, cur

    uah_psm_usd = implied_psm_usd(p, "UAH", a)
    if uah_psm_usd is not None and _RENT_PSM_FLOOR_USD <= uah_psm_usd <= _RENT_PSM_CEILING_USD:
        return p, "UAH"
    return p, cur


def sanitize_price_per_sqm(
    *,
    price: float | None,
    currency: str | None,
    area_sqm: float | None,
    deal_type: str | None,
    price_per_sqm: float | None,
) -> float | None:
    """
    Drop mislabeled totals in price_per_sqm; prefer total/area when consistent.
    Common bug: monthly rent copied into price_per_sqm (781 $/мес shown as 781 $/м²).
    """
    implied = implied_psm_native(price, currency, area_sqm)
    if implied is None:
        return price_per_sqm
    if price_per_sqm is None:
        return implied
    try:
        psm = float(price_per_sqm)
        p = float(price) if price is not None else None
    except (TypeError, ValueError):
        return price_per_sqm
    if p is not None and abs(psm - p) / max(p, 1.0) < 0.02:
        return implied
    implied_usd = to_usd(implied, currency)
    stored_usd = to_usd(psm, currency)
    if implied_usd is None:
        return price_per_sqm
    if stored_usd is None:
        return implied
    if (deal_type or "").lower() == "rent" and stored_usd > _RENT_PSM_CEILING_USD:
        if implied_usd <= _RENT_PSM_CEILING_USD:
            return implied
    if abs(stored_usd - implied_usd) / max(implied_usd, 0.5) > 0.35:
        return implied
    return price_per_sqm


def effective_listing_psm_usd(
    price: float | None,
    currency: str | None,
    area: float | None,
    *,
    deal_type: str | None = None,
    price_per_sqm: float | None = None,
) -> float | None:
    price, currency = maybe_fix_rent_currency(price, currency, area, deal_type)
    native = sanitize_price_per_sqm(
        price=price,
        currency=currency,
        area_sqm=area,
        deal_type=deal_type,
        price_per_sqm=price_per_sqm,
    )
    if native is None:
        return None
    usd = to_usd(native, currency)
    if usd is None or not math.isfinite(usd) or usd <= 0:
        return None
    return usd


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

    price, cur = maybe_fix_rent_currency(
        price, cur, area_sqm, deal_type, title=title, description=description
    )
    currency = cur

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
        area_f = float(area_sqm)
        psm_f = float(parsed_psm)
        total_cur = (parsed_cur or currency or cur or "USD").upper()
        total_f = float(parsed_total)
        # Prefer an already-plausible total over reconstructing from $/м².
        # Description often has an alternative offer ("весь поверх 18$/м²") that
        # must not override the card total (e.g. 316 769 ₴).
        total_usd = to_usd(total_f, total_cur)
        implied_usd = (total_usd / area_f) if total_usd else None
        total_ok = implied_usd is not None and not psm_suspicious(deal_type, implied_usd)

        if not total_ok:
            rebuilt = round(psm_f * area_f, 2)
            rebuilt_usd = to_usd(rebuilt, total_cur)
            rebuilt_implied = (rebuilt_usd / area_f) if rebuilt_usd else None
            if rebuilt_implied is not None and not psm_suspicious(deal_type, rebuilt_implied):
                total_f = rebuilt
                total_usd = rebuilt_usd
                implied_usd = rebuilt_implied
            elif price is not None:
                stored = float(price)
                stored_usd = to_usd(stored, total_cur)
                stored_implied = (stored_usd / area_f) if stored_usd else None
                if stored_implied is not None and not psm_suspicious(
                    deal_type, stored_implied
                ):
                    total_f = stored
                    total_usd = stored_usd
                    implied_usd = stored_implied

        # Keep explicit $/м² only when it agrees with the chosen total (USD ballpark).
        # "18$/м²" is USD even when the card total is UAH — alt offers must not stick.
        out_psm = round(total_f / area_f, 4)
        psm_as_usd = to_usd(psm_f, "USD")
        if (
            implied_usd is not None
            and psm_as_usd is not None
            and abs(psm_as_usd - implied_usd) / max(implied_usd, 0.5) <= 0.12
        ):
            out_psm = psm_f

        return PriceNorm(
            total_f,
            total_cur,
            out_psm,
            False,
            suspicious_psm=psm_suspicious(deal_type, implied_usd),
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
    suspicious = psm_suspicious(deal_type, implied)

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
        if not is_rent and (usd < _SALE_PSM_MIN_USD or usd > _SALE_PSM_MAX_USD):
            # Stored "price" as putative $/м² must itself look like a sale rate
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
            "skip_expand_implausible_rate",
        )

    if psm_field is None and explicit is not None:
        psm_field = explicit
    psm_field = sanitize_price_per_sqm(
        price=price,
        currency=currency or cur,
        area_sqm=area_sqm,
        deal_type=deal_type,
        price_per_sqm=psm_field,
    )
    suspicious = psm_suspicious(deal_type, to_usd(implied, cur))

    if suspicious and text:
        reparsed_psm, _ = parse_price_per_sqm(text)
        if reparsed_psm is not None:
            reparsed_usd = to_usd(float(reparsed_psm), cur)
            if reparsed_usd is not None and not psm_suspicious(deal_type, reparsed_usd):
                fixed_total = round(float(reparsed_psm) * a, 2)
                return PriceNorm(
                    fixed_total,
                    currency or cur,
                    float(reparsed_psm),
                    False,
                    False,
                    "repair_absurd_from_text_psm",
                )

    return PriceNorm(price, currency, psm_field, False, suspicious, "")
