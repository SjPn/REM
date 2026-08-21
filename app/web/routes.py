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
from app.domain.market_stats import (
    KYIV_DISTRICTS,
    compute_all_market_stats,
    count_active_inventory,
    district_label_ru,
)
from app.domain.seller_stress import compute_seller_stress
from app.domain.signals import (
    activity_summary,
    below_market_hint,
    classify_seller,
    recent_events,
)

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
router = APIRouter()


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
    suffix = "/м²" if deal_type == "sale" else "/м²·міс"
    if value >= 100:
        return f"{value:,.0f} $".replace(",", " ") + suffix
    return f"{value:.1f} $".replace(",", " ") + suffix


def _listing_psm_value(
    price: float | None,
    area_sqm: float | None,
    price_per_sqm: float | None = None,
) -> float | None:
    if price_per_sqm is not None:
        try:
            stored = float(price_per_sqm)
            if math.isfinite(stored) and stored > 0:
                return stored
        except (TypeError, ValueError):
            pass
    if price is None or area_sqm is None:
        return None
    try:
        p, a = float(price), float(area_sqm)
        if not math.isfinite(p) or not math.isfinite(a) or p <= 0 or a <= 0:
            return None
        return p / a
    except (TypeError, ValueError):
        return None


def _fmt_listing_psm(
    price: float | None,
    currency: str | None,
    area_sqm: float | None,
    deal_type: str | None = None,
    price_per_sqm: float | None = None,
) -> str:
    value = _listing_psm_value(price, area_sqm, price_per_sqm)
    if value is None:
        return ""
    cur = (currency or "").upper()
    suffix = "/м²·міс" if (deal_type or "").lower() == "rent" else "/м²"
    if value >= 100:
        num = f"{value:,.0f}".replace(",", " ")
    else:
        num = f"{value:.1f}"
    return f"{num} {cur}{suffix}".strip()


_DEAL_TYPE_UA = {"sale": "Продажа", "rent": "Аренда"}
_BUCKET_UA = {
    "likely_deal": "Вероятная сделка",
    "ambiguous": "Неопределённо",
    "likely_withdrawn": "Скорее сняли",
}
_STATUS_UA = {
    "active": "Активно",
    "vanished": "Исчезло",
    "sold": "Продано",
    "rented": "Сдано",
    "sold_marked": "Продано (метка)",
    "rented_marked": "Сдано (метка)",
    "relisted": "Повторная публикация",
}
_EVENT_UA = {
    "appeared": "Новое",
    "price_changed": "Цена",
    "status_changed": "Статус",
    "vanished": "Исчезло",
    "relisted": "Снова выставили",
    "content_changed": "Обновлено",
}
_SELLER_UA = {"owner": "Собственник", "agency": "Агент", "unknown": "Продавец ?"}


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


templates.env.globals["fmt_price"] = _fmt_price
templates.env.globals["fmt_psm"] = _fmt_psm
templates.env.globals["fmt_listing_psm"] = _fmt_listing_psm
templates.env.globals["ua_deal_type"] = _ua_deal_type
templates.env.globals["ua_bucket"] = _ua_bucket
templates.env.globals["ua_status"] = _ua_status
templates.env.globals["ua_event"] = _ua_event
templates.env.globals["ua_seller"] = _ua_seller
templates.env.globals["district_ru"] = district_label_ru


def _period_since(period: str | None) -> datetime | None:
    if period == "24h":
        return datetime.now(timezone.utc) - timedelta(hours=24)
    if period == "7d":
        return datetime.now(timezone.utc) - timedelta(days=7)
    return None


def _phone_freq(db: Session) -> Counter[str]:
    counts: Counter[str] = Counter()
    for (phone,) in db.execute(select(Listing.phone).where(Listing.phone.is_not(None))):
        digits = phone_digits(phone)
        if digits:
            counts[digits] += 1
    return counts


def _portal_counts(db: Session, property_ids: list[int]) -> dict[int, int]:
    if not property_ids:
        return {}
    rows = db.execute(
        select(Listing.property_id, func.count())
        .where(Listing.property_id.in_(property_ids))
        .group_by(Listing.property_id)
    ).all()
    return {int(pid): int(n) for pid, n in rows if pid is not None}


def _annotate_listings(
    db: Session,
    listings: list[Listing],
    market: dict,
) -> dict[int, dict]:
    sale_med = {d.district: d.median_psm for d in market["sale"].districts}
    rent_med = {d.district: d.median_psm for d in market["rent"].districts}
    phones = _phone_freq(db)
    portals = _portal_counts(
        db, [x.property_id for x in listings if x.property_id is not None]
    )
    out: dict[int, dict] = {}
    for x in listings:
        med_map = sale_med if x.deal_type == "sale" else rent_med
        city_med = (
            market["sale"].city_median_psm
            if x.deal_type == "sale"
            else market["rent"].city_median_psm
        )
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
        )
        digits = phone_digits(x.phone)
        seller = classify_seller(
            agency=x.agency,
            phone=x.phone,
            title=x.title,
            description=x.description,
            phone_listing_count=phones.get(digits, 1) if digits else 1,
        )
        extra = x.raw_extra or {}
        out[x.id] = {
            "below_market": hint.below_market,
            "discount_pct": hint.discount_pct,
            "seller": seller,
            "portals": portals.get(x.property_id, 1) if x.property_id else 1,
            "cap_rate_pct": extra.get("cap_rate_pct"),
            "noi": extra.get("noi"),
        }
    return out


def _district_rows(market: dict, *, with_median: bool = False) -> list[dict]:
    sale_by = {d.district: d for d in market["sale"].districts}
    rent_by = {d.district: d for d in market["rent"].districts}
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


def _mode_district_rows(
    market: dict,
    inventory: dict,
    stress_map: dict,
    *,
    mode: str,
) -> list[dict]:
    """Rows for one deal mode: inventory + $/m² sample + seller pressure."""
    slice_ = market["sale"] if mode == "sale" else market["rent"]
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
    rows.sort(key=lambda x: (-x["active"], x["avg_psm"] is None, -(x["avg_psm"] or 0)))
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
    below_market: int = Query(0, ge=0, le=1),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=10, le=100),
):
    activity = activity_summary(db, hours=24)
    activity_7d = activity_summary(db, hours=168)
    events = recent_events(db, hours=24, limit=25)
    watches = db.scalars(select(WatchFilter).order_by(WatchFilter.created_at.desc()).limit(20)).all()
    inventory = count_active_inventory(db)

    filters = [
        Listing.status.in_(["active", "relisted"]),
        Listing.price.is_not(None),
        Listing.deal_type == deal_type,
    ]
    if source:
        filters.append(Listing.source == source)
    if segment:
        filters.append(Listing.property_type == segment)
    since = _period_since(period)
    if since is not None:
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

    market = compute_all_market_stats(db)
    sale_med = {d.district: d.median_psm for d in market["sale"].districts}
    rent_med = {d.district: d.median_psm for d in market["rent"].districts}
    stress_map = {s.district: s for s in compute_seller_stress(db, deal_type=deal_type)}
    mode_rows = _mode_district_rows(market, inventory, stress_map, mode=deal_type)
    market_slice = market[deal_type]

    if below_market:
        candidates = db.scalars(
            select(Listing).where(*filters).order_by(Listing.first_seen_at.desc()).limit(2000)
        ).all()
        kept: list[Listing] = []
        for x in candidates:
            med_map = sale_med if x.deal_type == "sale" else rent_med
            city_med = (
                market["sale"].city_median_psm
                if x.deal_type == "sale"
                else market["rent"].city_median_psm
            )
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
            )
            if hint.below_market:
                kept.append(x)
        total = len(kept)
        pages = max(1, (total + per_page - 1) // per_page)
        page = min(page, pages)
        offset = (page - 1) * per_page
        listings = kept[offset : offset + per_page]
    else:
        total = db.scalar(select(func.count()).select_from(Listing).where(*filters)) or 0
        pages = max(1, (total + per_page - 1) // per_page)
        page = min(page, pages)
        offset = (page - 1) * per_page
        order = Listing.first_seen_at.desc() if period else Listing.last_seen_at.desc()
        listings = db.scalars(
            select(Listing).where(*filters).order_by(order).offset(offset).limit(per_page)
        ).all()

    signals = _annotate_listings(db, listings, market)

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

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "activity": activity,
            "activity_7d": activity_7d,
            "events": events,
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
            "below_market": below_market,
            "segments": segments,
            "sources": sources,
            "market": market,
            "market_slice": market_slice,
            "inventory": inventory,
            "mode_rows": mode_rows,
        },
    )


@router.post("/watches")
def create_watch(
    name: str = Form(...),
    q: str = Form(""),
    source: str = Form(""),
    deal_type: str = Form(""),
    segment: str = Form(""),
    period: str = Form(""),
    below_market: int = Form(0),
    db: Session = Depends(get_db),
):
    name = (name or "").strip()[:120] or "Фильтр"
    watch = WatchFilter(
        name=name,
        q=(q or "").strip() or None,
        source=(source or "").strip() or None,
        deal_type=(deal_type or "").strip() or None,
        segment=(segment or "").strip() or None,
        period=(period or "").strip() or None,
        below_market=bool(below_market),
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
):
    market = compute_all_market_stats(db)
    inventory = count_active_inventory(db)
    stress_map = {s.district: s for s in compute_seller_stress(db, deal_type=mode)}
    mode_rows = _mode_district_rows(market, inventory, stress_map, mode=mode)
    market_slice = market[mode]
    compare_rows = _district_rows(market, with_median=True)
    compare_by = {r["district"]: r for r in compare_rows}
    return templates.TemplateResponse(
        request,
        "market.html",
        {
            "mode": mode,
            "market": market,
            "market_slice": market_slice,
            "inventory": inventory,
            "mode_rows": mode_rows,
            "compare_by": compare_by,
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
):
    q = select(DealHypothesis).order_by(
        DealHypothesis.score.desc(), DealHypothesis.created_at.desc()
    )
    if bucket:
        q = q.where(DealHypothesis.bucket == bucket)
    hyps = db.scalars(q.limit(100)).all()
    cards = [_deal_card(db, h) for h in hyps]
    counts = {
        "likely_deal": db.scalar(
            select(func.count())
            .select_from(DealHypothesis)
            .where(DealHypothesis.bucket == "likely_deal")
        )
        or 0,
        "ambiguous": db.scalar(
            select(func.count())
            .select_from(DealHypothesis)
            .where(DealHypothesis.bucket == "ambiguous")
        )
        or 0,
        "likely_withdrawn": db.scalar(
            select(func.count())
            .select_from(DealHypothesis)
            .where(DealHypothesis.bucket == "likely_withdrawn")
        )
        or 0,
        "all": db.scalar(select(func.count()).select_from(DealHypothesis)) or 0,
    }
    return templates.TemplateResponse(
        request,
        "deals.html",
        {"cards": cards, "bucket": bucket or "", "counts": counts},
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
    return templates.TemplateResponse(
        request,
        "property.html",
        {
            "property": prop,
            "listings": listings,
            "events": events,
            "hyps": hyps,
            "signals": signals,
        },
    )
