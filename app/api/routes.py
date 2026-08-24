from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.db.models import DealHypothesis, Listing, Property, PropertyEvent
from app.pipeline.demo import seed_demo_dataset
from app.pipeline.reconcile import rescore_all_vanished
from app.pipeline.runner import run_crawl
from app.scrapers import SCRAPERS

router = APIRouter(prefix="/api")


class LabelBody(BaseModel):
    human_label: str  # deal | withdrawn | unknown


@router.get("/health")
def health():
    return {"status": "ok"}


@router.get("/stats")
def stats(db: Session = Depends(get_db)):
    return {
        "properties": db.scalar(select(func.count()).select_from(Property)) or 0,
        "listings_active": db.scalar(
            select(func.count()).select_from(Listing).where(Listing.status == "active")
        )
        or 0,
        "listings_vanished": db.scalar(
            select(func.count()).select_from(Listing).where(Listing.status == "vanished")
        )
        or 0,
        "deal_hypotheses": db.scalar(select(func.count()).select_from(DealHypothesis)) or 0,
        "events": db.scalar(select(func.count()).select_from(PropertyEvent)) or 0,
        "sources": list(SCRAPERS.keys()),
    }


@router.get("/listings")
def list_listings(
    status: str | None = None,
    source: str | None = None,
    deal_type: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    q = select(Listing).order_by(Listing.last_seen_at.desc())
    if status:
        q = q.where(Listing.status == status)
    if source:
        q = q.where(Listing.source == source)
    if deal_type:
        q = q.where(Listing.deal_type == deal_type)
    rows = db.scalars(q.offset(offset).limit(limit)).all()
    return [_listing_dict(x) for x in rows]


@router.get("/properties")
def list_properties(
    active: bool | None = None,
    deal_type: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    q = select(Property).order_by(Property.last_seen_at.desc())
    if active is not None:
        q = q.where(Property.is_active.is_(active))
    if deal_type:
        q = q.where(Property.deal_type == deal_type)
    rows = db.scalars(q.offset(offset).limit(limit)).all()
    return [_property_dict(x) for x in rows]


@router.get("/properties/{property_id}")
def property_detail(property_id: int, db: Session = Depends(get_db)):
    prop = db.get(Property, property_id)
    if not prop:
        raise HTTPException(404, "Property not found")
    listings = db.scalars(
        select(Listing).where(Listing.property_id == property_id)
    ).all()
    events = db.scalars(
        select(PropertyEvent)
        .where(PropertyEvent.property_id == property_id)
        .order_by(PropertyEvent.occurred_at.desc())
        .limit(100)
    ).all()
    hyps = db.scalars(
        select(DealHypothesis)
        .where(DealHypothesis.property_id == property_id)
        .order_by(DealHypothesis.created_at.desc())
    ).all()
    return {
        "property": _property_dict(prop),
        "listings": [_listing_dict(x) for x in listings],
        "events": [_event_dict(x) for x in events],
        "deal_hypotheses": [_hyp_dict(x) for x in hyps],
    }


@router.get("/deals")
def list_deals(
    bucket: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    q = select(DealHypothesis).order_by(DealHypothesis.score.desc(), DealHypothesis.created_at.desc())
    if bucket:
        q = q.where(DealHypothesis.bucket == bucket)
    rows = db.scalars(q.limit(limit)).all()
    return [_hyp_dict(x, include_property=True, db=db) for x in rows]


@router.post("/deals/{hyp_id}/label")
def label_deal(hyp_id: int, body: LabelBody, db: Session = Depends(get_db)):
    if body.human_label not in {"deal", "withdrawn", "unknown"}:
        raise HTTPException(400, "human_label must be deal|withdrawn|unknown")
    hyp = db.get(DealHypothesis, hyp_id)
    if not hyp:
        raise HTTPException(404, "Hypothesis not found")
    hyp.human_label = body.human_label
    db.commit()
    return _hyp_dict(hyp)


@router.get("/events")
def list_events(
    event_type: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    q = select(PropertyEvent).order_by(PropertyEvent.occurred_at.desc())
    if event_type:
        q = q.where(PropertyEvent.event_type == event_type)
    rows = db.scalars(q.limit(limit)).all()
    return [_event_dict(x) for x in rows]


@router.get("/crawls")
def list_crawls(limit: int = 20, db: Session = Depends(get_db)):
    from app.domain.coverage import recent_crawls

    return recent_crawls(db, limit=limit)


@router.get("/coverage")
def coverage(db: Session = Depends(get_db)):
    from app.domain.coverage import coverage_report, recent_crawls

    report = coverage_report(db)
    report["recent_crawls"] = recent_crawls(db, limit=20)
    return report


@router.post("/crawl")
def trigger_crawl(
    sources: str | None = Query(None, description="comma-separated: lun,olx,domria,rieltor"),
    max_pages: int = Query(2, ge=1, le=20),
    db: Session = Depends(get_db),
):
    src = [s.strip() for s in sources.split(",")] if sources else None
    return run_crawl(db, sources=src, max_pages=max_pages)


@router.post("/demo/seed")
def demo_seed(db: Session = Depends(get_db)):
    return seed_demo_dataset(db)


@router.post("/deals/rescore")
def rescore(db: Session = Depends(get_db)):
    return {"rescored": rescore_all_vanished(db)}


def _listing_dict(x: Listing) -> dict:
    return {
        "id": x.id,
        "property_id": x.property_id,
        "source": x.source,
        "external_id": x.external_id,
        "url": x.url,
        "title": x.title,
        "deal_type": x.deal_type,
        "property_type": x.property_type,
        "price": x.price,
        "currency": x.currency,
        "area_sqm": x.area_sqm,
        "floor": x.floor,
        "address_raw": x.address_raw,
        "district": x.district,
        "city": x.city,
        "status": x.status,
        "price_drop_count": x.price_drop_count,
        "first_seen_at": x.first_seen_at,
        "last_seen_at": x.last_seen_at,
        "vanished_at": x.vanished_at,
    }


def _property_dict(x: Property) -> dict:
    return {
        "id": x.id,
        "fingerprint": x.fingerprint,
        "title": x.title,
        "address_norm": x.address_norm,
        "district": x.district,
        "city": x.city,
        "property_type": x.property_type,
        "deal_type": x.deal_type,
        "area_sqm": x.area_sqm,
        "floor": x.floor,
        "is_active": x.is_active,
        "first_seen_at": x.first_seen_at,
        "last_seen_at": x.last_seen_at,
    }


def _event_dict(x: PropertyEvent) -> dict:
    return {
        "id": x.id,
        "property_id": x.property_id,
        "listing_id": x.listing_id,
        "event_type": x.event_type,
        "occurred_at": x.occurred_at,
        "payload": x.payload,
    }


def _hyp_dict(
    x: DealHypothesis,
    include_property: bool = False,
    db: Session | None = None,
) -> dict:
    data = {
        "id": x.id,
        "property_id": x.property_id,
        "listing_id": x.listing_id,
        "score": x.score,
        "bucket": x.bucket,
        "features": x.features,
        "human_label": x.human_label,
        "created_at": x.created_at,
    }
    if include_property and db is not None:
        prop = db.get(Property, x.property_id)
        if prop:
            data["property"] = _property_dict(prop)
    return data
