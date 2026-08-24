from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import DealHypothesis, Listing, Property, PropertyEvent, utcnow
from app.domain.deal_score import DealScoreInput, score_deal
from app.domain.enums import DealBucket, DealType, EventType, ListingStatus

logger = logging.getLogger(__name__)

_ACTIVE_STATUSES = (ListingStatus.ACTIVE.value, ListingStatus.RELISTED.value)


def _count_active_listings(db: Session, property_id: int) -> int:
    return (
        db.scalar(
            select(func.count())
            .select_from(Listing)
            .where(
                Listing.property_id == property_id,
                Listing.status.in_(_ACTIVE_STATUSES),
            )
        )
        or 0
    )


def _last_property_reactivation(db: Session, property_id: int) -> datetime | None:
    return db.scalar(
        select(func.max(PropertyEvent.occurred_at)).where(
            PropertyEvent.property_id == property_id,
            PropertyEvent.event_type.in_(
                [EventType.APPEARED.value, EventType.RELISTED.value]
            ),
        )
    )


def _pick_representative_listing(
    db: Session, property_id: int, preferred_id: int | None = None
) -> Listing | None:
    if preferred_id:
        row = db.get(Listing, preferred_id)
        if row and row.property_id == property_id:
            return row
    return db.scalar(
        select(Listing)
        .where(
            Listing.property_id == property_id,
            Listing.status == ListingStatus.VANISHED.value,
        )
        .order_by(Listing.vanished_at.desc(), Listing.last_seen_at.desc())
    )


def reconcile_property_vanish(
    db: Session,
    property_id: int,
    *,
    now: datetime | None = None,
    trigger_listing: Listing | None = None,
) -> bool:
    """Emit one VANISHED event per property when it has no active listings anywhere."""
    if not property_id:
        return False
    now = now or utcnow()
    if _count_active_listings(db, property_id) > 0:
        _refresh_property_active(db, property_id)
        return False

    since_reactivate = _last_property_reactivation(db, property_id)
    already_rows = db.scalars(
        select(PropertyEvent).where(
            PropertyEvent.property_id == property_id,
            PropertyEvent.event_type == EventType.VANISHED.value,
        )
    ).all()
    for ev in already_rows:
        if (ev.payload or {}).get("level") != "property":
            continue
        if since_reactivate is not None and ev.occurred_at < since_reactivate:
            continue
        _refresh_property_active(db, property_id)
        return False

    rep = _pick_representative_listing(
        db, property_id, preferred_id=trigger_listing.id if trigger_listing else None
    )
    if rep is None:
        _refresh_property_active(db, property_id)
        return False

    siblings = list(
        db.scalars(select(Listing).where(Listing.property_id == property_id)).all()
    )
    vanished_sources = sorted(
        {s.source for s in siblings if s.status == ListingStatus.VANISHED.value}
    )
    db.add(
        PropertyEvent(
            property_id=property_id,
            listing_id=rep.id,
            event_type=EventType.VANISHED.value,
            occurred_at=now,
            payload={
                "sources_vanished": vanished_sources,
                "trigger_source": trigger_listing.source if trigger_listing else rep.source,
                "trigger_external_id": (
                    trigger_listing.external_id if trigger_listing else rep.external_id
                ),
                "level": "property",
            },
        )
    )
    _refresh_property_active(db, property_id)
    create_or_update_deal_hypothesis(db, rep, allow_likely_deal=True)
    return True


def mark_vanished(
    db: Session,
    source: str,
    seen_external_ids: set[str],
    *,
    grace_hours: int = 6,
) -> int:
    """Mark missing listings vanished; property-level event only if gone from all sources."""
    now = utcnow()
    cutoff = now - timedelta(hours=grace_hours)
    q = select(Listing).where(
        Listing.source == source,
        Listing.status.in_(_ACTIVE_STATUSES),
        Listing.last_seen_at < cutoff,
    )
    listings_marked = 0
    properties_emitted = 0
    touched_properties: set[int] = set()

    for listing in db.scalars(q):
        if listing.external_id in seen_external_ids:
            continue
        listing.status = ListingStatus.VANISHED.value
        listing.vanished_at = now
        listings_marked += 1
        if listing.property_id:
            touched_properties.add(int(listing.property_id))

    db.flush()

    for pid in touched_properties:
        rep = db.scalar(
            select(Listing)
            .where(
                Listing.property_id == pid,
                Listing.source == source,
                Listing.status == ListingStatus.VANISHED.value,
            )
            .order_by(Listing.vanished_at.desc())
        )
        if reconcile_property_vanish(db, pid, now=now, trigger_listing=rep):
            properties_emitted += 1

    db.commit()
    if listings_marked:
        logger.info(
            "%s: marked %s listings vanished, %s properties emitted vanish events",
            source,
            listings_marked,
            properties_emitted,
        )
    return listings_marked


def _refresh_property_active(db: Session, property_id: int) -> None:
    prop = db.get(Property, property_id)
    if not prop:
        return
    prop.is_active = _count_active_listings(db, property_id) > 0


def _agency_bulk_delist(db: Session, listing: Listing, window_hours: int = 24) -> bool:
    if not listing.agency:
        return False
    since = utcnow() - timedelta(hours=window_hours)
    cnt = db.scalar(
        select(func.count())
        .select_from(Listing)
        .where(
            Listing.agency == listing.agency,
            Listing.source == listing.source,
            Listing.status == ListingStatus.VANISHED.value,
            Listing.vanished_at >= since,
        )
    )
    return (cnt or 0) >= 8


def _relisted_soon(db: Session, listing: Listing, within_days: int = 14) -> bool:
    if not listing.property_id:
        return False
    since = utcnow() - timedelta(days=within_days)
    relist = db.scalar(
        select(func.count())
        .select_from(PropertyEvent)
        .where(
            PropertyEvent.property_id == listing.property_id,
            PropertyEvent.event_type == EventType.RELISTED.value,
            PropertyEvent.occurred_at >= since,
        )
    )
    return (relist or 0) > 0


def create_or_update_deal_hypothesis(
    db: Session,
    listing: Listing,
    *,
    allow_likely_deal: bool = True,
) -> DealHypothesis | None:
    if listing.status != ListingStatus.VANISHED.value or not listing.property_id:
        return None

    prop = db.get(Property, listing.property_id)
    if not prop:
        return None

    siblings = list(
        db.scalars(select(Listing).where(Listing.property_id == listing.property_id))
    )
    active_elsewhere = sum(
        1
        for s in siblings
        if s.status in _ACTIVE_STATUSES
    )
    if active_elsewhere > 0:
        return None

    vanished_sources = {
        s.source for s in siblings if s.status == ListingStatus.VANISHED.value
    }
    tracked_sources = {s.source for s in siblings}
    cross_source = len(tracked_sources) >= 2

    prev_price = None
    price_events = list(
        db.scalars(
            select(PropertyEvent)
            .where(
                PropertyEvent.listing_id == listing.id,
                PropertyEvent.event_type == EventType.PRICE_CHANGED.value,
            )
            .order_by(PropertyEvent.occurred_at.desc())
        )
    )
    if price_events and price_events[0].payload:
        prev_price = price_events[0].payload.get("old_price")

    explicit = False
    raw_status = (listing.source_status_raw or "").lower()
    if any(
        x in raw_status
        for x in (
            "продано",
            "sold",
            "здано",
            "арендовано",
            "орендовано",
            "rented",
            "sold_or_unavailable",
        )
    ):
        explicit = True
        if any(x in raw_status for x in ("прода", "sold")):
            listing.status = ListingStatus.SOLD_MARKED.value
        elif any(x in raw_status for x in ("здан", "арендован", "орендован", "rent")):
            listing.status = ListingStatus.RENTED_MARKED.value

    vanished_at = listing.vanished_at or utcnow()
    days_since = (utcnow() - vanished_at).total_seconds() / 86400.0

    result = score_deal(
        DealScoreInput(
            deal_type=DealType(listing.deal_type),
            vanished_at=vanished_at,
            first_seen_at=listing.first_seen_at,
            last_price=listing.price,
            previous_price=prev_price,
            price_drop_count=listing.price_drop_count or 0,
            active_on_other_sources=0,
            vanished_on_sources=len(vanished_sources),
            tracked_sources_for_property=len(tracked_sources),
            explicit_sold_or_rented=explicit,
            agency_bulk_delist=_agency_bulk_delist(db, listing),
            relisted_soon=_relisted_soon(db, listing),
            days_since_vanish=days_since,
            cross_source_confirmed=cross_source,
        )
    )

    bucket = result.bucket.value
    # Partial crawl / skip vanish: keep score but never promote to likely_deal.
    if (
        not allow_likely_deal
        and bucket == DealBucket.LIKELY_DEAL.value
        and not explicit
    ):
        bucket = DealBucket.AMBIGUOUS.value

    existing = db.scalar(
        select(DealHypothesis)
        .where(DealHypothesis.property_id == prop.id)
        .order_by(DealHypothesis.created_at.desc())
    )
    features = result.to_dict()
    features["bucket"] = bucket
    if not allow_likely_deal and bucket != result.bucket.value:
        features["capped_partial_crawl"] = True
    if existing and existing.human_label is None:
        existing.score = result.score
        existing.bucket = bucket
        existing.features = features
        existing.listing_id = listing.id
        hyp = existing
    else:
        hyp = DealHypothesis(
            property_id=prop.id,
            listing_id=listing.id,
            score=result.score,
            bucket=bucket,
            features=features,
        )
        db.add(hyp)
    return hyp


def rescore_all_vanished(
    db: Session,
    *,
    allow_likely_deal: bool = True,
) -> int:
    property_ids = [
        pid
        for pid in db.scalars(
            select(Listing.property_id)
            .where(
                Listing.status == ListingStatus.VANISHED.value,
                Listing.property_id.is_not(None),
            )
            .distinct()
        ).all()
        if pid is not None
    ]
    n = 0
    for pid in property_ids:
        if _count_active_listings(db, int(pid)) > 0:
            continue
        rep = _pick_representative_listing(db, int(pid))
        if rep and create_or_update_deal_hypothesis(
            db, rep, allow_likely_deal=allow_likely_deal
        ):
            n += 1
    db.commit()
    return n


def detect_agency_bulk_patterns(db: Session) -> dict[str, int]:
    """Utility stats: agencies with many vanish events in 24h."""
    since = utcnow() - timedelta(hours=24)
    rows = db.execute(
        select(Listing.agency, Listing.source, func.count())
        .where(
            Listing.status == ListingStatus.VANISHED.value,
            Listing.vanished_at >= since,
            Listing.agency.is_not(None),
        )
        .group_by(Listing.agency, Listing.source)
        .having(func.count() >= 8)
    ).all()
    return {f"{agency}|{source}": cnt for agency, source, cnt in rows}
