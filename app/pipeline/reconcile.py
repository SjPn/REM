from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import DealHypothesis, Listing, Property, PropertyEvent, utcnow
from app.domain.deal_score import DealScoreInput, score_deal
from app.domain.enums import DealType, EventType, ListingStatus

logger = logging.getLogger(__name__)


def mark_vanished(
    db: Session,
    source: str,
    seen_external_ids: set[str],
    *,
    grace_hours: int = 6,
) -> int:
    """Mark active listings of `source` as vanished if missing from latest crawl set.

    Only considers listings previously seen (not created in the far future).
    Uses a grace window so partial crawls don't wipe the DB.
    """
    now = utcnow()
    cutoff = now - timedelta(hours=grace_hours)
    q = select(Listing).where(
        Listing.source == source,
        Listing.status.in_(
            [ListingStatus.ACTIVE.value, ListingStatus.RELISTED.value]
        ),
        Listing.last_seen_at < cutoff,
    )
    vanished = 0
    for listing in db.scalars(q):
        if listing.external_id in seen_external_ids:
            continue
        listing.status = ListingStatus.VANISHED.value
        listing.vanished_at = now
        db.add(
            PropertyEvent(
                property_id=listing.property_id,
                listing_id=listing.id,
                event_type=EventType.VANISHED.value,
                occurred_at=now,
                payload={"source": source, "external_id": listing.external_id},
            )
        )
        vanished += 1
        if listing.property_id:
            _refresh_property_active(db, listing.property_id)
            create_or_update_deal_hypothesis(db, listing)
    db.commit()
    return vanished


def _refresh_property_active(db: Session, property_id: int) -> None:
    prop = db.get(Property, property_id)
    if not prop:
        return
    active_count = db.scalar(
        select(func.count())
        .select_from(Listing)
        .where(
            Listing.property_id == property_id,
            Listing.status.in_(
                [ListingStatus.ACTIVE.value, ListingStatus.RELISTED.value]
            ),
        )
    )
    prop.is_active = bool(active_count)


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


def create_or_update_deal_hypothesis(db: Session, listing: Listing) -> DealHypothesis | None:
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
        if s.id != listing.id
        and s.status in (ListingStatus.ACTIVE.value, ListingStatus.RELISTED.value)
    )
    vanished_sources = {
        s.source
        for s in siblings
        if s.status == ListingStatus.VANISHED.value
    }
    tracked_sources = {s.source for s in siblings}

    # previous price from events
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

    result = score_deal(
        DealScoreInput(
            deal_type=DealType(listing.deal_type),
            vanished_at=listing.vanished_at or utcnow(),
            first_seen_at=listing.first_seen_at,
            last_price=listing.price,
            previous_price=prev_price,
            price_drop_count=listing.price_drop_count or 0,
            active_on_other_sources=active_elsewhere,
            vanished_on_sources=len(vanished_sources),
            tracked_sources_for_property=len(tracked_sources),
            explicit_sold_or_rented=explicit,
            agency_bulk_delist=_agency_bulk_delist(db, listing),
            relisted_soon=_relisted_soon(db, listing),
        )
    )

    existing = db.scalar(
        select(DealHypothesis)
        .where(DealHypothesis.property_id == prop.id)
        .order_by(DealHypothesis.created_at.desc())
    )
    if existing and existing.human_label is None:
        existing.score = result.score
        existing.bucket = result.bucket.value
        existing.features = result.to_dict()
        existing.listing_id = listing.id
        hyp = existing
    else:
        hyp = DealHypothesis(
            property_id=prop.id,
            listing_id=listing.id,
            score=result.score,
            bucket=result.bucket.value,
            features=result.to_dict(),
        )
        db.add(hyp)
    return hyp


def rescore_all_vanished(db: Session) -> int:
    listings = list(
        db.scalars(
            select(Listing).where(Listing.status == ListingStatus.VANISHED.value)
        )
    )
    n = 0
    for listing in listings:
        create_or_update_deal_hypothesis(db, listing)
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
