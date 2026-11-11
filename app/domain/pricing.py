"""Price / $/m² normalization — never invent, only repair obvious misreads."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from app.domain.market_stats import to_usd

# Rent $/m²·мес: under $2 with no price in the text the number is a rate.
# A sum written in the title or the ad stays the month down to $1, even under $2.
_RENT_PSM_FLOOR_USD = 3.0
_RENT_BARE_RATE_USD = 2.0
_RENT_TEXT_KEEP_USD = 1.0
_RENT_PSM_CEILING_USD = 70.0
# Sale: expand only when implied $/m² is absurdly low (price field is clearly a rate).
_SALE_PSM_FLOOR_USD = 80.0
# Plausible Kyiv commercial sale band ($/м²). Keep in sync with market_stats._SALE_PSM_*.
_SALE_PSM_MIN_USD = 450.0
_SALE_PSM_MAX_USD = 10_000.0


@dataclass(frozen=True)
class PriceNorm:
    price: float | None
    currency: str | None
    price_per_sqm: float | None
    reinterpreted_as_psm: bool
    suspicious_psm: bool = False
    detail: str = ""


_PER_MONTH_RE = re.compile(
    r"(?P<lead>\$|usd|€|євро)?\s*"
    r"(?P<num>\d{1,3}(?:[ \u00a0]\d{3})+|\d+(?:[.,]\d+)?)\s*"
    r"(?P<tail>грн|₴|uah|\$|usd|дол(?:ар\w*)?|євро|€)?\s*"
    r"/\s*(?:міс|мес)\w*",
    re.IGNORECASE,
)
_NAMED_PRICE_RE = re.compile(
    r"(?:ціна|цена|орендна\s+плата|арендная\s+плата)\s*[:\-]?\s*"
    r"(?P<lead>\$|usd|€|євро)?\s*"
    r"(?P<num>\d{1,3}(?:[ \u00a0]\d{3})+|\d+(?:[.,]\d+)?)\s*"
    r"(?P<tail>грн|₴|uah|\$|usd|дол(?:ар\w*)?|євро|€)?",
    re.IGNORECASE,
)
_BARE_HRYVNIA_RE = re.compile(
    r"(\d{1,3}(?:[ \u00a0]\d{3})+|\d+)\s*₴",
)
_HOURLY_RE = re.compile(
    r"погодин|почасов|посуточ|за\s+годину|за\s+час\b|на\s+день|/\s*день",
    re.IGNORECASE,
)


def _money_token(raw: str) -> float | None:
    cleaned = raw.replace("\xa0", " ").strip()
    if re.fullmatch(r"\d{1,3}(?: \d{3})+", cleaned):
        cleaned = cleaned.replace(" ", "")
    else:
        cleaned = cleaned.replace(" ", "").replace(",", ".")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return value


def _currency_token(token: str | None) -> str:
    raw = (token or "грн").lower()
    if raw in ("$", "usd") or raw.startswith("дол"):
        return "USD"
    if raw in ("€", "євро"):
        return "EUR"
    return "UAH"


def _first_money(pattern: re.Pattern[str], text: str | None) -> tuple[float, str] | None:
    if not text or _HOURLY_RE.search(text):
        return None
    match = pattern.search(text)
    if not match:
        return None
    value = _money_token(match.group("num"))
    if value is None:
        return None
    token = match.group("lead") or match.group("tail")
    return value, _currency_token(token)


def _amount_is_rate(title: str | None, description: str | None, amount: float) -> bool:
    text = " ".join(part for part in (title, description) if part)
    rate = extract_explicit_psm(text)
    if rate is None:
        return False
    return abs(rate - amount) / max(amount, 1.0) <= 0.05


def stated_monthly_rent(
    title: str | None,
    description: str | None,
    area_sqm: float | None,
) -> tuple[float, str, str] | None:
    """Month's rent written in the title or the ad, if the wording is explicit.

    «13 000 грн/міс» and «ціна 600 дол» are the month even when the result is
    under $2/m². A bare «500 ₴» on a large room is not: that still goes through
    the $2 line. A bare «3 000 ₴» is the month when it lands between $1 and $70.
    """
    for part in (description, title):
        found = _first_money(_PER_MONTH_RE, part)
        if found is not None:
            sane = _sane_stated_month(found, area_sqm, bare=False)
            if sane is not None and not _amount_is_rate(title, description, sane[0]):
                return sane[0], sane[1], "month"
    for part in (title, description):
        found = _first_money(_NAMED_PRICE_RE, part)
        if found is not None:
            sane = _sane_stated_month(found, area_sqm, bare=False)
            if sane is not None and not _amount_is_rate(title, description, sane[0]):
                return sane[0], sane[1], "month"
    if description and not _HOURLY_RE.search(description):
        match = _BARE_HRYVNIA_RE.search(description)
        if match:
            value = _money_token(match.group(1))
            if value is not None:
                sane = _sane_stated_month((value, "UAH"), area_sqm, bare=True)
                if sane is not None and not _amount_is_rate(title, description, sane[0]):
                    return sane[0], sane[1], "hryvnia"
    return None


def _sane_stated_month(
    found: tuple[float, str],
    area_sqm: float | None,
    *,
    bare: bool,
) -> tuple[float, str] | None:
    amount, currency = found
    if area_sqm is None:
        return None if bare else found
    try:
        area = float(area_sqm)
    except (TypeError, ValueError):
        return None
    if area <= 0:
        return None
    usd = to_usd(amount, currency)
    if usd is None:
        return None
    implied = usd / area
    if implied > _RENT_PSM_CEILING_USD:
        return None
    if bare and implied < _RENT_TEXT_KEEP_USD:
        return None
    return found


_PLAIN_AMOUNT_RE = re.compile(
    r"(\d{1,3}(?:[ \u00a0]\d{3})+|\d{2,7}(?:[.,]\d+)?)\s*"
    r"(грн|₴|uah|\$|usd|дол(?:ар\w*)?)",
    re.IGNORECASE,
)
_RATE_TAIL_RE = re.compile(
    r"\s*(?:/|\s)\s*(?:м(?:²|2)|кв)",
    re.IGNORECASE,
)


def _same_usd(left: float, left_cur: str | None, right: float, right_cur: str | None) -> bool:
    left_usd = to_usd(left, left_cur)
    right_usd = to_usd(right, right_cur)
    if left_usd is None or right_usd is None or right_usd <= 0:
        return False
    return abs(left_usd - right_usd) / right_usd <= 0.05


def _fee_was_multiplied(
    title: str | None,
    description: str | None,
    stored: float,
    stored_cur: str | None,
    area: float,
    month: float,
) -> bool:
    """Stored total is some other sum from the text (a service fee) times the area."""
    text = " ".join(part for part in (title, description) if part)
    for match in _PLAIN_AMOUNT_RE.finditer(text):
        if _RATE_TAIL_RE.match(text[match.end() : match.end() + 12]):
            continue
        value = _money_token(match.group(1))
        if value is None or abs(value - month) / max(month, 1.0) <= 0.05:
            continue
        if _same_usd(stored, stored_cur, value * area, _currency_token(match.group(2))):
            return True
    return False


def rent_text_correction(
    price: float | None,
    currency: str | None,
    area_sqm: float | None,
    title: str | None,
    description: str | None,
) -> tuple[float, str] | None:
    """Stored rent to replace, when the title or the ad names the month.

    Replaces a price only when it is that month times the area, a fee times
    the area, or itself under $2/m². A sane stored month that merely disagrees
    with another number in the text is left alone.
    """
    if price is None or area_sqm is None:
        return None
    try:
        stored = float(price)
        area = float(area_sqm)
    except (TypeError, ValueError):
        return None
    if stored <= 0 or area < 10:
        return None
    stated = stated_monthly_rent(title, description, area)
    if stated is None:
        return None
    amount, scur, kind = stated
    month_usd = to_usd(amount, scur)
    if month_usd is None:
        return None
    month_psm = month_usd / area
    if month_psm < _RENT_TEXT_KEEP_USD or month_psm > _RENT_PSM_CEILING_USD:
        return None
    if (scur or "").upper() != (currency or "UAH").upper():
        return None
    if _same_usd(stored, currency, amount, scur):
        return None
    stored_usd = to_usd(stored, currency)
    cheap_field = (
        kind == "month"
        and stored_usd is not None
        and stored_usd / area < _RENT_BARE_RATE_USD
    )
    multiplied = _same_usd(stored, currency, amount * area, scur)
    fee = kind == "month" and _fee_was_multiplied(
        title, description, stored, currency, area, amount
    )
    if multiplied or fee or cheap_field:
        return round(amount, 2), scur
    return None


def extract_explicit_psm(text: str | None) -> float | None:
    """First explicit N $/м² (or грн/м², грн./кв.м) from listing text."""
    if not text:
        return None
    from app.scrapers.http_utils import parse_price_per_sqm

    value, _cur = parse_price_per_sqm(text)
    return value


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

    Rent: 3 000 грн for 27 m² ($2.5/m²) is still the month. Without a price in
    the title or the ad, multiply a rent only under $2/m². A sum written in
    the title or the text stays the month down to $1/m².
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
    parsed_psm, parsed_psm_cur = parse_price_per_sqm(text) if text else (None, None)
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
        floor = psm_floor_usd(deal_type)
        psm_cur_u = (parsed_psm_cur or "USD").upper()
        rate_usd = to_usd(psm_f, psm_cur_u)
        ceiling = _RENT_PSM_CEILING_USD if is_rent else _SALE_PSM_MAX_USD
        rate_sane = rate_usd is not None and floor <= rate_usd <= ceiling
        # 279 грн next to «15 $/м²» is the service fee, not the monthly rent.
        below_floor = implied_usd is not None and implied_usd < floor and rate_sane
        total_ok = (
            implied_usd is not None
            and not psm_suspicious(deal_type, implied_usd)
            and not below_floor
        )

        if not total_ok:
            rebuild_cur = psm_cur_u if below_floor else total_cur
            rebuilt = round(psm_f * area_f, 2)
            rebuilt_usd = to_usd(rebuilt, rebuild_cur)
            rebuilt_implied = (rebuilt_usd / area_f) if rebuilt_usd else None
            if rebuilt_implied is not None and not psm_suspicious(deal_type, rebuilt_implied):
                total_f = rebuilt
                total_cur = rebuild_cur
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

    # A month written in the title or the ad wins over the $2 line when the
    # stored figure is that month, that month times the area, or a fee times
    # the area. Any other disagreement with the text is left as stored.
    stated_matches = False
    if is_rent:
        stated = stated_monthly_rent(title, description, a)
        if stated is not None and _same_usd(p, cur, stated[0], stated[1]):
            stated_matches = True
        elif rent_text_correction(p, cur, a, title, description) is not None:
            amount, scur = rent_text_correction(p, cur, a, title, description)
            psm_out = round(amount / a, 4)
            rate = extract_explicit_psm(text)
            stated_usd = to_usd(amount, scur)
            rate_usd = to_usd(rate, "USD") if rate is not None else None
            if (
                stated_usd
                and rate_usd
                and abs(rate_usd - stated_usd / a) / max(stated_usd / a, 0.5) <= 0.12
            ):
                psm_out = rate
            return PriceNorm(amount, scur, psm_out, False, False, "stated_monthly")

    # Under $2/m² an unlabeled rent figure is a rate. The written month above
    # already returned. Sale totals of a few thousand dollars stay a $/m² rate.
    bare_rate = (
        not stated_matches
        and implied < floor
        and (not is_rent or implied < _RENT_BARE_RATE_USD)
    )
    if bare_rate:
        if is_rent:
            rate_ok = usd <= _RENT_PSM_CEILING_USD
        else:
            rate_ok = usd >= _SALE_PSM_MIN_USD and usd <= _SALE_PSM_MAX_USD
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
