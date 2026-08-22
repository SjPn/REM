from __future__ import annotations

import math
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.db.models import DealHypothesis, Listing, Property, PropertyEvent, WatchFilter
from app.domain.fingerprint import phone_digits
from app.domain.listing_stats import is_excluded_from_stats, set_stats_exclusion
from app.domain.market_history import ensure_today_snapshot, series_for_charts
from app.domain.market_stats import (
    KYIV_DISTRICTS,
    compute_all_market_stats,
    count_active_inventory,
    district_label_ru,
    normalize_district,
    pick_rent_market_slice,
    rough_yield_by_district,
    to_usd,
)
from app.domain.deals_preview import deal_bucket_counts, recent_deal_hypotheses
from app.domain.pricing import effective_listing_psm_usd, sanitize_price_per_sqm
from app.domain.ttl_cache import cache_clear
from app.domain.seller_stress import compute_seller_stress
from app.domain.signals import (
    OPEX_UNKNOWN,
    OPEX_WITH,
    OPEX_WITHOUT,
    activity_summary,
    below_market_hint,
    MarketHint,
    classify_seller,
    listing_ids_for_price_drops,
    listing_ids_for_vanished,
    listing_psm_usd,
    resolve_listing_opex,
)

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
router = APIRouter()

_SORT_VALUES = frozenset(
    {
        "newest",
        "oldest",
        "price_asc",
        "price_desc",
        "area_asc",
        "area_desc",
        "psm_asc",
        "psm_desc",
        "dom_asc",
        "dom_desc",
    }
)


def _days_on_market(first_seen_at: datetime | None) -> int | None:
    if first_seen_at is None:
        return None
    fs = first_seen_at
    if fs.tzinfo is None:
        fs = fs.replace(tzinfo=timezone.utc)
    return max(0, (datetime.now(timezone.utc) - fs).days)


def _parse_optional_float(value: float | None) -> float | None:
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v) or v < 0:
        return None
    return v


def _listing_price_usd(lst: Listing) -> float | None:
    if lst.price is None:
        return None
    try:
        return to_usd(float(lst.price), lst.currency)
    except (TypeError, ValueError):
        return None


def _sql_order(sort: str, *, period: str | None):
    if sort == "price_asc":
        return Listing.price.asc()
    if sort == "price_desc":
        return Listing.price.desc()
    if sort == "area_asc":
        return Listing.area_sqm.asc()
    if sort == "area_desc":
        return Listing.area_sqm.desc()
    if sort == "psm_asc":
        return Listing.price_per_sqm.asc()
    if sort == "psm_desc":
        return Listing.price_per_sqm.desc()
    if sort == "oldest":
        return Listing.first_seen_at.asc()
    # newest
    if period:
        return Listing.first_seen_at.desc()
    return Listing.last_seen_at.desc()


def _sort_listings_in_memory(rows: list[Listing], sort: str) -> list[Listing]:
    """Stable-ish sorts; USD for price/psm so UAH and USD don't mix wrongly."""
    reverse = sort.endswith("_desc") or sort == "newest"

    def key_price(x: Listing):
        v = _listing_price_usd(x)
        return (v is None, v if v is not None else 0.0)

    def key_area(x: Listing):
        v = float(x.area_sqm) if x.area_sqm is not None else None
        return (v is None, v if v is not None else 0.0)

    def key_psm(x: Listing):
        v = listing_psm_usd(
            x.price, x.currency, x.area_sqm, deal_type=x.deal_type, price_per_sqm=x.price_per_sqm
        )
        return (v is None, v if v is not None else 0.0)

    def key_seen(x: Listing):
        ts = x.first_seen_at if sort in ("newest", "oldest") else x.last_seen_at
        return ts or datetime.min.replace(tzinfo=timezone.utc)

    def key_dom(x: Listing):
        d = _days_on_market(x.first_seen_at)
        return (d is None, d if d is not None else 0)

    if sort in ("price_asc", "price_desc"):
        rows = sorted(rows, key=key_price, reverse=reverse)
    elif sort in ("area_asc", "area_desc"):
        rows = sorted(rows, key=key_area, reverse=reverse)
    elif sort in ("psm_asc", "psm_desc"):
        rows = sorted(rows, key=key_psm, reverse=reverse)
    elif sort in ("dom_asc", "dom_desc"):
        rows = sorted(rows, key=key_dom, reverse=reverse)
    elif sort == "oldest":
        rows = sorted(rows, key=key_seen, reverse=False)
    elif sort == "newest":
        rows = sorted(rows, key=key_seen, reverse=True)
    return rows


def _apply_range_filters(
    rows: list[Listing],
    *,
    price_min: float | None,
    price_max: float | None,
    area_min: float | None,
    area_max: float | None,
) -> list[Listing]:
    out: list[Listing] = []
    for x in rows:
        if area_min is not None:
            if x.area_sqm is None or float(x.area_sqm) < area_min:
                continue
        if area_max is not None:
            if x.area_sqm is None or float(x.area_sqm) > area_max:
                continue
        if price_min is not None or price_max is not None:
            usd = _listing_price_usd(x)
            if usd is None:
                continue
            if price_min is not None and usd < price_min:
                continue
            if price_max is not None and usd > price_max:
                continue
        out.append(x)
    return out


def _fmt_price(price: float | None, currency: str | None) -> str:
    if price is None:
        return "—"
    try:
        if not math.isfinite(float(price)) or float(price) <= 0:
            return "—"
    except (TypeError, ValueError):
        return "—"
    cur = (currency or "").upper() or ""
    value = float(price)
    if value >= 1000:
        return f"{value:,.0f} {cur}".replace(",", " ")
    return f"{value:g} {cur}".strip()


def _fmt_psm(value: float | None, deal_type: str = "sale") -> str:
    if value is None:
        return "—"
    suffix = "/м²" if deal_type == "sale" else "/м²·мес"
    if value >= 100:
        return f"{value:,.0f} $".replace(",", " ") + suffix
    return f"{value:.1f} $".replace(",", " ") + suffix


def _listing_psm_value(
    price: float | None,
    area_sqm: float | None,
    price_per_sqm: float | None = None,
    deal_type: str | None = None,
    currency: str | None = None,
) -> float | None:
    native = sanitize_price_per_sqm(
        price=price,
        currency=currency,
        area_sqm=area_sqm,
        deal_type=deal_type,
        price_per_sqm=price_per_sqm,
    )
    if native is None:
        return None
    cur = (currency or "USD").upper()
    if cur == "USD":
        return native
    usd = to_usd(native, currency)
    return usd if usd is not None else native


def _fmt_listing_psm(
    price: float | None,
    currency: str | None,
    area_sqm: float | None,
    deal_type: str | None = None,
    price_per_sqm: float | None = None,
) -> str:
    usd = effective_listing_psm_usd(
        price,
        currency,
        area_sqm,
        deal_type=deal_type,
        price_per_sqm=price_per_sqm,
    )
    if usd is None:
        return ""
    suffix = "/м²·мес" if (deal_type or "").lower() == "rent" else "/м²"
    if usd >= 100:
        num = f"{usd:,.0f}".replace(",", " ")
    else:
        num = f"{usd:.1f}"
    return f"{num} ${suffix}".strip()


_DEAL_TYPE_UA = {"sale": "Продажа", "rent": "Аренда"}
_BUCKET_UA = {
    "likely_deal": "Похоже на сделку",
    "ambiguous": "Неясно",
    "likely_withdrawn": "Скорее просто сняли",
}
_STATUS_UA = {
    "active": "В сети",
    "vanished": "Снято",
    "sold": "Продано",
    "rented": "Сдано",
    "sold_marked": "Продано (с сайта)",
    "rented_marked": "Сдано (с сайта)",
    "relisted": "Снова в сети",
}
_EVENT_UA = {
    "appeared": "Появилось",
    "price_changed": "Цена изменилась",
    "status_changed": "Статус изменился",
    "vanished": "Снято с сайта",
    "relisted": "Снова выставили",
    "content_changed": "Текст обновили",
}
_SELLER_UA = {"owner": "Собственник", "agency": "Агентство", "unknown": "Неизвестно"}
_OPEX_RU = {
    OPEX_WITH: "С OPEX",
    OPEX_WITHOUT: "Без OPEX",
    OPEX_UNKNOWN: "OPEX не указан",
}


def _ua_deal_type(value: str | None) -> str:
    if not value:
        return ""
    return _DEAL_TYPE_UA.get(value, value)


def _ua_bucket(value: str | None) -> str:
    if not value:
        return ""
    return _BUCKET_UA.get(value, value)


def _ua_status(value: str | None) -> str:
    if not value:
        return ""
    return _STATUS_UA.get(value, value)


def _ua_event(value: str | None) -> str:
    if not value:
        return ""
    return _EVENT_UA.get(value, value)


def _ua_seller(value: str | None) -> str:
    if not value:
        return ""
    return _SELLER_UA.get(value, value)


def _ua_opex(value: str | None) -> str:
    if not value:
        return ""
    return _OPEX_RU.get(value, value)


templates.env.globals["fmt_price"] = _fmt_price
templates.env.globals["fmt_psm"] = _fmt_psm
templates.env.globals["fmt_listing_psm"] = _fmt_listing_psm
templates.env.globals["ua_deal_type"] = _ua_deal_type
templates.env.globals["ua_bucket"] = _ua_bucket
templates.env.globals["ua_status"] = _ua_status
templates.env.globals["ua_event"] = _ua_event
templates.env.globals["ua_seller"] = _ua_seller
templates.env.globals["ua_opex"] = _ua_opex
templates.env.globals["district_ru"] = district_label_ru


def _period_since(period: str | None) -> datetime | None:
    if period == "24h":
        return datetime.now(timezone.utc) - timedelta(hours=24)
    if period == "7d":
        return datetime.now(timezone.utc) - timedelta(days=7)
    return None


def _phone_freq(db: Session) -> Counter[str]:
    from app.domain.ttl_cache import cache_get

    def _build() -> Counter[str]:
        counts: Counter[str] = Counter()
        for (phone,) in db.execute(select(Listing.phone).where(Listing.phone.is_not(None))):
            digits = phone_digits(phone)
            if digits:
                counts[digits] += 1
        return counts

    return cache_get("phone_freq", 120.0, _build)


def _portal_counts(db: Session, property_ids: list[int]) -> dict[int, int]:
    if not property_ids:
        return {}
    rows = db.execute(
        select(Listing.property_id, func.count())
        .where(Listing.property_id.in_(property_ids))
        .group_by(Listing.property_id)
    ).all()
    return {int(pid): int(n) for pid, n in rows if pid is not None}


def _portal_spreads(db: Session, property_ids: list[int]) -> dict[int, dict]:
    """For multi-portal properties: count, sources, USD price min/max and spread %."""
    if not property_ids:
        return {}
    peers = db.scalars(
        select(Listing).where(
            Listing.property_id.in_(property_ids),
            Listing.status.in_(["active", "relisted"]),
            Listing.price.is_not(None),
        )
    ).all()
    by_pid: dict[int, list[Listing]] = {}
    for lst in peers:
        if lst.property_id is None:
            continue
        by_pid.setdefault(int(lst.property_id), []).append(lst)

    out: dict[int, dict] = {}
    for pid, items in by_pid.items():
        if len(items) < 2:
            continue
        usd_vals: list[float] = []
        sources: list[str] = []
        for lst in items:
            usd = to_usd(float(lst.price), lst.currency) if lst.price is not None else None
            if usd is not None and math.isfinite(usd) and usd > 0:
                usd_vals.append(usd)
            if lst.source and lst.source not in sources:
                sources.append(lst.source)
        if len(usd_vals) < 2:
            out[pid] = {
                "count": len(items),
                "sources": sources,
                "spread_pct": None,
            }
            continue
        lo, hi = min(usd_vals), max(usd_vals)
        spread = round((hi - lo) / lo * 100.0, 1) if lo > 0 else None
        out[pid] = {
            "count": len(items),
            "sources": sources,
            "price_min_usd": round(lo, 0),
            "price_max_usd": round(hi, 0),
            "spread_pct": spread,
        }
    return out


def _annotate_listings(
    db: Session,
    listings: list[Listing],
    market: dict,
) -> dict[int, dict]:
    sale_med = {d.district: d.median_psm for d in market["sale"].districts}
    rent_net_med = {
        d.district: d.median_psm for d in market["rent_without_opex"].districts
    }
    rent_gross_med = {
        d.district: d.median_psm for d in market["rent_with_opex"].districts
    }
    rent_all_med = {d.district: d.median_psm for d in market["rent"].districts}
    phones = _phone_freq(db)
    pids = [x.property_id for x in listings if x.property_id is not None]
    portals = _portal_counts(db, pids)
    spreads = _portal_spreads(db, pids)
    out: dict[int, dict] = {}
    for x in listings:
        opex = resolve_listing_opex(x) if x.deal_type == "rent" else None
        if x.deal_type == "sale":
            med_map = sale_med
            city_med = market["sale"].city_median_psm
        elif opex == OPEX_WITHOUT and market["rent_without_opex"].city_count:
            med_map = rent_net_med
            city_med = market["rent_without_opex"].city_median_psm
        elif opex == OPEX_WITH and market["rent_with_opex"].city_count:
            med_map = rent_gross_med
            city_med = market["rent_with_opex"].city_median_psm
        else:
            med_map = rent_all_med
            city_med = market["rent"].city_median_psm
        hint = below_market_hint(
            price=x.price,
            currency=x.currency,
            area=x.area_sqm,
            deal_type=x.deal_type,
            district=x.district,
            address=x.address_raw,
            title=x.title,
            city=x.city,
            median_by_district=med_map,
            city_median=city_med,
            price_per_sqm=x.price_per_sqm,
        )
        if is_excluded_from_stats(x):
            hint = MarketHint(False, None, hint.ref_median_psm, hint.district)
        digits = phone_digits(x.phone)
        seller = classify_seller(
            agency=x.agency,
            phone=x.phone,
            title=x.title,
            description=x.description,
            phone_listing_count=phones.get(digits, 1) if digits else 1,
        )
        extra = x.raw_extra or {}
        spread = spreads.get(x.property_id) if x.property_id else None
        out[x.id] = {
            "below_market": hint.below_market,
            "discount_pct": hint.discount_pct,
            "seller": seller,
            "portals": portals.get(x.property_id, 1) if x.property_id else 1,
            "portal_spread_pct": spread.get("spread_pct") if spread else None,
            "portal_sources": spread.get("sources") if spread else None,
            "cap_rate_pct": extra.get("cap_rate_pct"),
            "noi": extra.get("noi"),
            "opex": opex,
            "price_suspicious": bool(extra.get("price_suspicious")),
            "excluded_from_stats": is_excluded_from_stats(x),
            "dom_days": _days_on_market(x.first_seen_at),
        }
    return out


def _mode_district_rows(
    market: dict,
    inventory: dict,
    stress_map: dict,
    *,
    mode: str,
    opex: str | None = None,
) -> list[dict]:
    """Rows for one deal mode: inventory + $/m² sample + seller pressure."""
    if mode == "sale":
        slice_ = market["sale"]
    else:
        slice_ = pick_rent_market_slice(market, opex)
    by_price = {d.district: d for d in slice_.districts}
    inv_by = {d["district"]: d for d in inventory["districts"]}
    rows = []
    names = set(by_price) | set(inv_by) | set(stress_map)
    for name in KYIV_DISTRICTS:
        if name not in names:
            continue
        price = by_price.get(name)
        inv = inv_by.get(name, {})
        st = stress_map.get(name)
        active_count = inv.get(mode, 0)
        rows.append(
            {
                "district": name,
                "active": active_count,
                "avg_psm": price.avg_psm if price else None,
                "median_psm": price.median_psm if price else None,
                "sample_n": price.count if price else 0,
                "stress": st.score if st else None,
                "stress_detail": st.detail if st else "",
            }
        )
    rows.sort(key=lambda x: (-x["active"], x["median_psm"] is None, -(x["median_psm"] or 0)))
    return rows


def _district_rows(market: dict, *, with_median: bool = False) -> list[dict]:
    sale_by = {d.district: d for d in market["sale"].districts}
    # Compare table uses net rent when available enough, else mixed
    rent_slice = pick_rent_market_slice(market)
    rent_by = {d.district: d for d in rent_slice.districts}
    rows = []
    for name in KYIV_DISTRICTS:
        s = sale_by.get(name)
        r = rent_by.get(name)
        if not s and not r:
            continue
        row = {
            "district": name,
            "sale_avg": s.avg_psm if s else None,
            "sale_n": s.count if s else 0,
            "rent_avg": r.avg_psm if r else None,
            "rent_n": r.count if r else 0,
        }
        if with_median:
            row["sale_median"] = s.median_psm if s else None
            row["rent_median"] = r.median_psm if r else None
        rows.append(row)
    rows.sort(key=lambda x: (x["sale_avg"] is None, -(x["sale_avg"] or 0), -(x["rent_avg"] or 0)))
    return rows


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    db: Session = Depends(get_db),
    source: str | None = None,
    deal_type: str = Query("sale", pattern="^(sale|rent)$"),
    segment: str | None = None,
    q: str | None = None,
    period: str | None = None,
    district: str | None = None,
    activity: str | None = Query(None, pattern="^(vanished|price_drop)$"),
    stats_excluded: int = Query(0, ge=0, le=1),
    opex: str | None = Query(None, pattern="^(with|without|unknown)$"),
    below_market: int = Query(0, ge=0, le=1),
    sort: str = Query("newest"),
    price_min: float | None = Query(None),
    price_max: float | None = Query(None),
    area_min: float | None = Query(None),
    area_max: float | None = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=10, le=100),
):
    if sort not in _SORT_VALUES:
        sort = "newest"
    if district:
        district = normalize_district(district) or (
            district if district in KYIV_DISTRICTS else None
        )
        if district not in KYIV_DISTRICTS:
            district = None
    price_min = _parse_optional_float(price_min)
    price_max = _parse_optional_float(price_max)
    area_min = _parse_optional_float(area_min)
    area_max = _parse_optional_float(area_max)
    if price_min is not None and price_max is not None and price_min > price_max:
        price_min, price_max = price_max, price_min
    if area_min is not None and area_max is not None and area_min > area_max:
        area_min, area_max = area_max, area_min

    activity_filter = activity
    activity_stats = activity_summary(db, hours=24, deal_type=deal_type)
    watches = db.scalars(select(WatchFilter).order_by(WatchFilter.created_at.desc()).limit(20)).all()
    inventory = count_active_inventory(db)
    stats_excluded_n = (
        db.scalar(
            select(func.count())
            .select_from(Listing)
            .where(
                Listing.status.in_(["active", "relisted"]),
                Listing.deal_type == deal_type,
                Listing.exclude_from_stats.is_(True),
            )
        )
        or 0
    )
    stats_excluded_filter = bool(stats_excluded)

    activity_since = datetime.now(timezone.utc) - timedelta(hours=24)
    activity_ids: list[int] | None = None
    if activity_filter == "vanished":
        activity_ids = listing_ids_for_vanished(
            db, since=activity_since, deal_type=deal_type
        )
    elif activity_filter == "price_drop":
        activity_ids = listing_ids_for_price_drops(
            db, since=activity_since, deal_type=deal_type
        )

    if activity_filter == "vanished":
        filters = [
            Listing.deal_type == deal_type,
            Listing.status == "vanished",
        ]
        if activity_ids is not None:
            if activity_ids:
                filters.append(Listing.id.in_(activity_ids))
            else:
                filters.append(Listing.id == -1)  # empty result
    elif activity_filter == "price_drop":
        filters = [
            Listing.deal_type == deal_type,
            Listing.status.in_(["active", "relisted", "vanished"]),
        ]
        if activity_ids is not None:
            if activity_ids:
                filters.append(Listing.id.in_(activity_ids))
            else:
                filters.append(Listing.id == -1)
    else:
        filters = [
            Listing.status.in_(["active", "relisted"]),
            Listing.price.is_not(None),
            Listing.deal_type == deal_type,
        ]
    if stats_excluded_filter:
        filters.append(Listing.exclude_from_stats.is_(True))
    if source:
        filters.append(Listing.source == source)
    if segment:
        filters.append(Listing.property_type == segment)
    if district and district in KYIV_DISTRICTS:
        ru = district_label_ru(district)
        filters.append(
            or_(
                Listing.district == district,
                Listing.district.ilike(f"%{district}%"),
                Listing.district.ilike(f"%{ru}%") if ru else Listing.district == district,
            )
        )
    since = _period_since(period)
    if since is not None and activity_filter is None:
        filters.append(Listing.first_seen_at >= since)
    if q:
        like = f"%{q.strip()}%"
        filters.append(
            or_(
                Listing.title.ilike(like),
                Listing.address_raw.ilike(like),
                Listing.district.ilike(like),
                Listing.city.ilike(like),
            )
        )
    # Area can be narrowed in SQL; price ranges use USD in Python (mixed currencies).
    if area_min is not None:
        filters.append(Listing.area_sqm >= area_min)
    if area_max is not None:
        filters.append(Listing.area_sqm <= area_max)

    market = compute_all_market_stats(db)
    opex_mode = opex if deal_type == "rent" else None
    stress_map = {s.district: s for s in compute_seller_stress(db, deal_type=deal_type)}
    mode_rows = _mode_district_rows(
        market, inventory, stress_map, mode=deal_type, opex=opex_mode
    )
    if deal_type == "rent":
        market_slice = pick_rent_market_slice(market, opex_mode)
        rent_slice_label = {
            None: (
                "без OPEX"
                if (market["rent_without_opex"].city_count or 0) >= 15
                else "все объявления (OPEX смешан)"
            ),
            "without": "без OPEX",
            "with": "с OPEX",
            "unknown": "OPEX не указан",
        }.get(opex_mode, "аренда")
    else:
        market_slice = market["sale"]
        rent_slice_label = ""

    needs_memory = bool(
        below_market
        or opex_mode
        or price_min is not None
        or price_max is not None
        or activity_ids is not None
        or sort in ("price_asc", "price_desc", "psm_asc", "psm_desc", "dom_asc", "dom_desc")
    )
    order = _sql_order(sort, period=period)

    if needs_memory:
        candidates = list(
            db.scalars(
                select(Listing).where(*filters).order_by(order).limit(2500)
            ).all()
        )
        candidates = _apply_range_filters(
            candidates,
            price_min=price_min,
            price_max=price_max,
            area_min=None,  # already in SQL
            area_max=None,
        )
        signals_tmp = _annotate_listings(db, candidates, market)
        if opex_mode:
            candidates = [
                x for x in candidates if signals_tmp.get(x.id, {}).get("opex") == opex_mode
            ]
        if below_market:
            candidates = [
                x for x in candidates if signals_tmp.get(x.id, {}).get("below_market")
            ]
        if activity_ids is not None and sort == "newest":
            rank = {lid: i for i, lid in enumerate(activity_ids)}
            candidates.sort(key=lambda x: rank.get(x.id, 10**9))
        else:
            candidates = _sort_listings_in_memory(candidates, sort)
        total = len(candidates)
        pages = max(1, (total + per_page - 1) // per_page)
        page = min(page, pages)
        offset = (page - 1) * per_page
        listings = candidates[offset : offset + per_page]
        signals = {i.id: signals_tmp[i.id] for i in listings if i.id in signals_tmp}
        # annotate page if somehow missing
        missing = [x for x in listings if x.id not in signals]
        if missing:
            signals.update(_annotate_listings(db, missing, market))
    else:
        total = db.scalar(select(func.count()).select_from(Listing).where(*filters)) or 0
        pages = max(1, (total + per_page - 1) // per_page)
        page = min(page, pages)
        offset = (page - 1) * per_page
        listings = list(
            db.scalars(
                select(Listing).where(*filters).order_by(order).offset(offset).limit(per_page)
            ).all()
        )
        signals = _annotate_listings(db, listings, market)

    deal_preview = [
        _deal_card(db, h)
        for h in recent_deal_hypotheses(
            db, deal_type=deal_type, bucket="likely_deal", hours=168, limit=5
        )
    ]

    segments = [
        r[0]
        for r in db.execute(
            select(Listing.property_type, func.count())
            .where(Listing.property_type.is_not(None), Listing.deal_type == deal_type)
            .group_by(Listing.property_type)
            .order_by(func.count().desc())
        ).all()
        if r[0]
    ]
    sources = [
        r[0]
        for r in db.execute(
            select(Listing.source, func.count())
            .where(Listing.deal_type == deal_type)
            .group_by(Listing.source)
            .order_by(func.count().desc())
        ).all()
    ]

    list_params = {
        "q": q or None,
        "source": source or None,
        "deal_type": deal_type,
        "segment": segment or None,
        "period": period or None,
        "district": district if district in KYIV_DISTRICTS else None,
        "activity": activity_filter or None,
        "stats_excluded": stats_excluded_filter or None,
        "opex": opex_mode or None,
        "below_market": below_market or None,
        "sort": sort if sort != "newest" else None,
        "price_min": price_min,
        "price_max": price_max,
        "area_min": area_min,
        "area_max": area_max,
    }
    list_qs = urlencode({k: v for k, v in list_params.items() if v not in (None, "", 0)})

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "activity_stats": activity_stats,
            "activity_filter": activity_filter or "",
            "watches": watches,
            "signals": signals,
            "listings": listings,
            "total": total,
            "page": page,
            "pages": pages,
            "per_page": per_page,
            "source": source or "",
            "deal_type": deal_type,
            "segment": segment or "",
            "q": q or "",
            "period": period or "",
            "district": district if district in KYIV_DISTRICTS else "",
            "opex": opex_mode or "",
            "below_market": below_market,
            "sort": sort,
            "price_min": "" if price_min is None else price_min,
            "price_max": "" if price_max is None else price_max,
            "area_min": "" if area_min is None else area_min,
            "area_max": "" if area_max is None else area_max,
            "list_qs": list_qs,
            "segments": segments,
            "sources": sources,
            "districts": KYIV_DISTRICTS,
            "market": market,
            "market_slice": market_slice,
            "rent_slice_label": rent_slice_label,
            "inventory": inventory,
            "stats_excluded_n": stats_excluded_n,
            "stats_excluded_filter": stats_excluded_filter,
            "deal_preview": deal_preview,
            "mode_rows": mode_rows,
        },
    )


@router.post("/listings/{listing_id}/stats-exclude")
def toggle_stats_exclude(
    listing_id: int,
    request: Request,
    exclude: int = Form(0),
    db: Session = Depends(get_db),
):
    listing = db.get(Listing, listing_id)
    if listing is None:
        return RedirectResponse("/", status_code=303)
    set_stats_exclusion(listing, excluded=bool(exclude), user_action=True)
    db.commit()
    cache_clear()
    target = request.headers.get("referer") or "/"
    return RedirectResponse(target, status_code=303)


@router.post("/watches")
def create_watch(
    name: str = Form(...),
    q: str = Form(""),
    source: str = Form(""),
    deal_type: str = Form(""),
    segment: str = Form(""),
    period: str = Form(""),
    below_market: int = Form(0),
    price_min: str = Form(""),
    price_max: str = Form(""),
    db: Session = Depends(get_db),
):
    name = (name or "").strip()[:120] or "Подборка"

    def _f(raw: str) -> float | None:
        raw = (raw or "").strip().replace(" ", "").replace(",", ".")
        if not raw:
            return None
        try:
            v = float(raw)
            return v if math.isfinite(v) and v >= 0 else None
        except ValueError:
            return None

    watch = WatchFilter(
        name=name,
        q=(q or "").strip() or None,
        source=(source or "").strip() or None,
        deal_type=(deal_type or "").strip() or None,
        segment=(segment or "").strip() or None,
        period=(period or "").strip() or None,
        below_market=bool(below_market),
        price_min=_f(price_min),
        price_max=_f(price_max),
    )
    db.add(watch)
    db.commit()
    params = {
        "q": watch.q or "",
        "source": watch.source or "",
        "deal_type": watch.deal_type or "",
        "segment": watch.segment or "",
        "period": watch.period or "",
        "below_market": "1" if watch.below_market else "0",
    }
    if watch.price_min is not None:
        params["price_min"] = str(watch.price_min)
    if watch.price_max is not None:
        params["price_max"] = str(watch.price_max)
    return RedirectResponse("/?" + urlencode(params), status_code=303)


@router.post("/watches/{watch_id}/delete")
def delete_watch(watch_id: int, db: Session = Depends(get_db)):
    watch = db.get(WatchFilter, watch_id)
    if watch:
        db.delete(watch)
        db.commit()
    return RedirectResponse("/", status_code=303)


@router.get("/market", response_class=HTMLResponse)
def market_page(
    request: Request,
    db: Session = Depends(get_db),
    mode: str = Query("sale", pattern="^(sale|rent)$"),
    opex: str | None = Query(None, pattern="^(with|without|unknown)$"),
):
    market = compute_all_market_stats(db)
    inventory = count_active_inventory(db)
    stats_excluded_n = (
        db.scalar(
            select(func.count())
            .select_from(Listing)
            .where(
                Listing.status.in_(["active", "relisted"]),
                Listing.deal_type == mode,
                Listing.exclude_from_stats.is_(True),
            )
        )
        or 0
    )
    stress_map = {s.district: s for s in compute_seller_stress(db, deal_type=mode)}
    opex_mode = opex if mode == "rent" else None
    mode_rows = _mode_district_rows(
        market, inventory, stress_map, mode=mode, opex=opex_mode
    )
    if mode == "rent":
        market_slice = pick_rent_market_slice(market, opex_mode)
        rent_slice_label = {
            None: (
                "без OPEX"
                if (market["rent_without_opex"].city_count or 0) >= 15
                else "все (OPEX смешан)"
            ),
            "without": "без OPEX",
            "with": "с OPEX",
            "unknown": "OPEX не указан",
        }.get(opex_mode, "")
    else:
        market_slice = market["sale"]
        rent_slice_label = ""
    compare_rows = _district_rows(market, with_median=True)
    compare_by = {r["district"]: r for r in compare_rows}
    yields = rough_yield_by_district(market)
    yield_by = {r["district"]: r for r in yields["rows"]}
    activity_stats = activity_summary(db, hours=24, deal_type=mode)
    deal_counts = deal_bucket_counts(db, deal_type=mode, hours=168)
    deal_preview = [
        _deal_card(db, h)
        for h in recent_deal_hypotheses(
            db, deal_type=mode, bucket="likely_deal", hours=168, limit=4
        )
    ]
    return templates.TemplateResponse(
        request,
        "market.html",
        {
            "mode": mode,
            "opex": opex_mode or "",
            "market": market,
            "market_slice": market_slice,
            "rent_slice_label": rent_slice_label,
            "inventory": inventory,
            "mode_rows": mode_rows,
            "compare_by": compare_by,
            "yields": yields,
            "yield_by": yield_by,
            "stats_excluded_n": stats_excluded_n,
            "activity_stats": activity_stats,
            "deal_counts": deal_counts,
            "deal_preview": deal_preview,
        },
    )


@router.get("/stats", response_class=HTMLResponse)
def stats_page(
    request: Request,
    db: Session = Depends(get_db),
    mode: str = Query("sale", pattern="^(sale|rent)$"),
):
    import json

    ensure_today_snapshot(db)
    series = series_for_charts(db, limit=90)
    market = compute_all_market_stats(db)
    inventory = count_active_inventory(db)
    market_slice = market["sale"] if mode == "sale" else pick_rent_market_slice(market)
    stress_map = {s.district: s for s in compute_seller_stress(db, deal_type=mode)}
    mode_rows = _mode_district_rows(
        market, inventory, stress_map, mode=mode, opex=None
    )
    district_labels = [district_label_ru(r["district"]) for r in mode_rows]
    district_medians = [r["median_psm"] for r in mode_rows]
    chart_payload = {
        "labels": series["labels"],
        "sale_median": series["sale_median"],
        "sale_avg": series["sale_avg"],
        "rent_median": series["rent_median"],
        "rent_avg": series["rent_avg"],
        "sale_active": series["sale_active"],
        "rent_active": series["rent_active"],
        "district_labels": district_labels,
        "district_medians": district_medians,
        "mode": mode,
    }
    latest = series["latest"]
    return templates.TemplateResponse(
        request,
        "stats.html",
        {
            "mode": mode,
            "market_slice": market_slice,
            "inventory": inventory,
            "series_n": series["n"],
            "latest": latest,
            "mode_rows": mode_rows,
            "chart_json": json.dumps(chart_payload, ensure_ascii=False),
        },
    )


def _hyp_features(hyp: DealHypothesis) -> list[dict]:
    raw = hyp.features or {}
    if isinstance(raw, dict) and isinstance(raw.get("features"), list):
        return raw["features"]
    if isinstance(raw, list):
        return raw
    return []


def _deal_card(db: Session, hyp: DealHypothesis) -> dict:
    prop = db.get(Property, hyp.property_id) if hyp.property_id else None
    listing = db.get(Listing, hyp.listing_id) if hyp.listing_id else None
    if listing is None and prop is not None:
        listing = db.scalars(
            select(Listing)
            .where(Listing.property_id == prop.id)
            .order_by(Listing.last_seen_at.desc())
        ).first()
    return {
        "hyp": hyp,
        "property": prop,
        "listing": listing,
        "features": _hyp_features(hyp),
    }


@router.get("/deals", response_class=HTMLResponse)
def deals_page(
    request: Request,
    db: Session = Depends(get_db),
    bucket: str | None = Query("likely_deal"),
    deal_type: str = Query("sale", pattern="^(sale|rent)$"),
    hours: int = Query(168, ge=0, le=8760),
):
    since = None
    if hours > 0:
        since = datetime.now(timezone.utc) - timedelta(hours=hours)

    q = (
        select(DealHypothesis)
        .join(Listing, DealHypothesis.listing_id == Listing.id)
        .where(Listing.deal_type == deal_type)
        .order_by(DealHypothesis.score.desc(), DealHypothesis.created_at.desc())
    )
    if bucket:
        q = q.where(DealHypothesis.bucket == bucket)
    if since is not None:
        q = q.where(DealHypothesis.created_at >= since)
    hyps = db.scalars(q.limit(100)).all()
    cards = [_deal_card(db, h) for h in hyps]

    counts = deal_bucket_counts(db, deal_type=deal_type, hours=hours if hours > 0 else None)
    period_label = {0: "всё время", 24: "за сутки", 168: "за неделю"}.get(
        hours, f"за {hours} ч"
    )
    return templates.TemplateResponse(
        request,
        "deals.html",
        {
            "cards": cards,
            "bucket": bucket or "",
            "counts": counts,
            "deal_type": deal_type,
            "hours": hours,
            "period_label": period_label,
        },
    )


@router.get("/deals/{hyp_id}", response_class=HTMLResponse)
def deal_detail(hyp_id: int, request: Request, db: Session = Depends(get_db)):
    hyp = db.get(DealHypothesis, hyp_id)
    if not hyp:
        return HTMLResponse("Not found", status_code=404)
    card = _deal_card(db, hyp)
    events = []
    if hyp.property_id:
        events = db.scalars(
            select(PropertyEvent)
            .where(PropertyEvent.property_id == hyp.property_id)
            .order_by(PropertyEvent.occurred_at.desc())
            .limit(50)
        ).all()
    listings = []
    if hyp.property_id:
        listings = db.scalars(
            select(Listing).where(Listing.property_id == hyp.property_id)
        ).all()
    return templates.TemplateResponse(
        request,
        "deal.html",
        {**card, "events": events, "listings": listings},
    )


@router.get("/properties/{property_id}", response_class=HTMLResponse)
def property_page(property_id: int, request: Request, db: Session = Depends(get_db)):
    prop = db.get(Property, property_id)
    if not prop:
        return HTMLResponse("Not found", status_code=404)
    listings = db.scalars(select(Listing).where(Listing.property_id == property_id)).all()
    events = db.scalars(
        select(PropertyEvent)
        .where(PropertyEvent.property_id == property_id)
        .order_by(PropertyEvent.occurred_at.desc())
    ).all()
    hyps = db.scalars(
        select(DealHypothesis)
        .where(DealHypothesis.property_id == property_id)
        .order_by(DealHypothesis.created_at.desc())
    ).all()
    market = compute_all_market_stats(db)
    signals = _annotate_listings(db, list(listings), market)
    spreads = _portal_spreads(db, [property_id])
    portal_compare = spreads.get(property_id) or {
        "count": len(listings),
        "sources": list({x.source for x in listings if x.source}),
        "spread_pct": None,
    }
    return templates.TemplateResponse(
        request,
        "property.html",
        {
            "property": prop,
            "listings": listings,
            "events": events,
            "hyps": hyps,
            "signals": signals,
            "portal_compare": portal_compare,
        },
    )
