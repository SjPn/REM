from __future__ import annotations

import math
from pathlib import Path

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.db.models import DealHypothesis, Listing, Property, PropertyEvent
from app.domain.market_stats import KYIV_DISTRICTS, compute_all_market_stats

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


_DEAL_TYPE_UA = {"sale": "Продаж", "rent": "Оренда"}
_BUCKET_UA = {
    "likely_deal": "Ймовірна угода",
    "ambiguous": "Невизначено",
    "likely_withdrawn": "Скоріше зняли",
}
_STATUS_UA = {
    "active": "Активне",
    "vanished": "Зникло",
    "sold": "Продано",
    "rented": "Здано",
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


templates.env.globals["fmt_price"] = _fmt_price
templates.env.globals["fmt_psm"] = _fmt_psm
templates.env.globals["fmt_listing_psm"] = _fmt_listing_psm
templates.env.globals["ua_deal_type"] = _ua_deal_type
templates.env.globals["ua_bucket"] = _ua_bucket
templates.env.globals["ua_status"] = _ua_status


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    db: Session = Depends(get_db),
    source: str | None = None,
    deal_type: str | None = None,
    segment: str | None = None,
    q: str | None = None,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=10, le=100),
):
    stats = {
        "properties": db.scalar(select(func.count()).select_from(Property)) or 0,
        "active": db.scalar(
            select(func.count()).select_from(Listing).where(Listing.status == "active")
        )
        or 0,
        "with_price": db.scalar(
            select(func.count()).select_from(Listing).where(Listing.price.is_not(None))
        )
        or 0,
        "with_address": db.scalar(
            select(func.count())
            .select_from(Listing)
            .where(Listing.address_raw.is_not(None), Listing.address_raw != "")
        )
        or 0,
        "likely_deals": db.scalar(
            select(func.count())
            .select_from(DealHypothesis)
            .where(DealHypothesis.bucket == "likely_deal")
        )
        or 0,
    }

    # Catalog: real portal listings with price (core product view)
    filters = [
        Listing.status.in_(["active", "relisted"]),
        Listing.price.is_not(None),
    ]
    if source:
        filters.append(Listing.source == source)
    if deal_type:
        filters.append(Listing.deal_type == deal_type)
    if segment:
        filters.append(Listing.property_type == segment)
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

    total = db.scalar(select(func.count()).select_from(Listing).where(*filters)) or 0
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, pages)
    offset = (page - 1) * per_page

    listings = db.scalars(
        select(Listing)
        .where(*filters)
        .order_by(Listing.last_seen_at.desc())
        .offset(offset)
        .limit(per_page)
    ).all()

    segments = [
        r[0]
        for r in db.execute(
            select(Listing.property_type, func.count())
            .where(Listing.property_type.is_not(None))
            .group_by(Listing.property_type)
            .order_by(func.count().desc())
        ).all()
        if r[0]
    ]
    sources = [
        r[0]
        for r in db.execute(
            select(Listing.source, func.count()).group_by(Listing.source).order_by(func.count().desc())
        ).all()
    ]

    market = compute_all_market_stats(db)
    sale_by = {d.district: d for d in market["sale"].districts}
    rent_by = {d.district: d for d in market["rent"].districts}
    district_rows = []
    for name in KYIV_DISTRICTS:
        s = sale_by.get(name)
        r = rent_by.get(name)
        if not s and not r:
            continue
        district_rows.append(
            {
                "district": name,
                "sale_avg": s.avg_psm if s else None,
                "sale_n": s.count if s else 0,
                "rent_avg": r.avg_psm if r else None,
                "rent_n": r.count if r else 0,
            }
        )
    # sort by sale avg then rent avg
    district_rows.sort(key=lambda x: (x["sale_avg"] is None, -(x["sale_avg"] or 0), -(x["rent_avg"] or 0)))

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "stats": stats,
            "listings": listings,
            "total": total,
            "page": page,
            "pages": pages,
            "per_page": per_page,
            "source": source or "",
            "deal_type": deal_type or "",
            "segment": segment or "",
            "q": q or "",
            "segments": segments,
            "sources": sources,
            "market": market,
            "district_rows": district_rows,
        },
    )


@router.get("/market", response_class=HTMLResponse)
def market_page(request: Request, db: Session = Depends(get_db)):
    market = compute_all_market_stats(db)
    sale_by = {d.district: d for d in market["sale"].districts}
    rent_by = {d.district: d for d in market["rent"].districts}
    district_rows = []
    for name in KYIV_DISTRICTS:
        s = sale_by.get(name)
        r = rent_by.get(name)
        if not s and not r:
            continue
        district_rows.append(
            {
                "district": name,
                "sale_avg": s.avg_psm if s else None,
                "sale_median": s.median_psm if s else None,
                "sale_n": s.count if s else 0,
                "rent_avg": r.avg_psm if r else None,
                "rent_median": r.median_psm if r else None,
                "rent_n": r.count if r else 0,
            }
        )
    district_rows.sort(
        key=lambda x: (x["sale_avg"] is None, -(x["sale_avg"] or 0), -(x["rent_avg"] or 0))
    )
    return templates.TemplateResponse(
        request,
        "market.html",
        {"market": market, "district_rows": district_rows},
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
    return templates.TemplateResponse(
        request,
        "property.html",
        {"property": prop, "listings": listings, "events": events, "hyps": hyps},
    )
