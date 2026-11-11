"""Undo a monthly rent that was multiplied by area.

3 000 грн for 27 m² is about $2.5/m² and used to be treated as a rate, so the
stored total became 81 000. A real rate («15 $/м²», «1000 грн./кв.м») stays.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Listing, ListingSnapshot, PropertyEvent
from app.domain.enums import EventType
from app.domain.listing_stats import apply_auto_stats_exclusion
from app.domain.market_stats import to_usd
from app.domain.pricing import psm_suspicious, rent_text_correction


def restored_monthly_rent(
    *,
    price: float | None,
    currency: str | None,
    area_sqm: float | None,
    deal_type: str | None,
    title: str | None,
    description: str | None,
    source: str | None,
) -> tuple[float, str] | None:
    """Monthly total written in the title or the ad, when the stored price differs."""
    del source
    if (deal_type or "").lower() != "rent":
        return None
    if price is None or area_sqm is None:
        return None
    try:
        stored = float(price)
        area = float(area_sqm)
    except (TypeError, ValueError):
        return None
    if stored <= 0 or area < 10:
        return None
    return rent_text_correction(stored, currency, area, title, description)


def find_false_rent_expansions(db: Session) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(Listing).where(
            Listing.deal_type == "rent",
            Listing.price > 0,
        )
    ).all()
    out: list[dict[str, Any]] = []
    for listing in rows:
        restored = restored_monthly_rent(
            price=listing.price,
            currency=listing.currency,
            area_sqm=listing.area_sqm,
            deal_type=listing.deal_type,
            title=listing.title,
            description=listing.description,
            source=listing.source,
        )
        if restored is None or listing.area_sqm is None:
            continue
        amount, scur = restored
        area = float(listing.area_sqm)
        out.append(
            {
                "listing_id": listing.id,
                "source": listing.source,
                "url": listing.url,
                "old_price": float(listing.price),
                "new_price": amount,
                "currency": scur,
                "new_psm": round(amount / area, 4),
            }
        )
    return out


def repair_false_rent_expansions(db: Session, *, dry_run: bool = True) -> dict[str, Any]:
    repairs = find_false_rent_expansions(db)
    by_source: dict[str, int] = {}
    for item in repairs:
        by_source[item["source"]] = by_source.get(item["source"], 0) + 1
    if dry_run:
        return {
            "dry_run": True,
            "count": len(repairs),
            "by_source": by_source,
            "sample": repairs[:30],
        }

    fixed = 0
    events_removed = 0
    by_source = {}
    for item in repairs:
        listing = db.get(Listing, int(item["listing_id"]))
        if listing is None:
            continue
        old = float(item["old_price"])
        listing.price = float(item["new_price"])
        listing.currency = str(item["currency"] or "UAH")
        listing.price_per_sqm = float(item["new_psm"])
        for snap in db.scalars(
            select(ListingSnapshot).where(ListingSnapshot.listing_id == listing.id)
        ):
            if snap.price is not None and abs(float(snap.price) - old) < 0.01:
                snap.price = listing.price
                snap.currency = listing.currency
        events = db.scalars(
            select(PropertyEvent).where(
                PropertyEvent.listing_id == listing.id,
                PropertyEvent.event_type == EventType.PRICE_CHANGED.value,
            )
        ).all()
        for ev in events:
            payload = ev.payload or {}
            try:
                new_p = float(payload["new_price"])
            except (KeyError, TypeError, ValueError):
                continue
            if abs(new_p - old) < 0.01:
                db.delete(ev)
                events_removed += 1
        extra = dict(listing.raw_extra or {})
        extra.pop("price_suspicious", None)
        extra["price_repair"] = "rent_total_not_rate"
        listing.raw_extra = extra
        apply_auto_stats_exclusion(
            listing,
            suspicious=psm_suspicious(
                listing.deal_type, to_usd(listing.price_per_sqm, listing.currency)
            ),
        )
        fixed += 1
        by_source[listing.source] = by_source.get(listing.source, 0) + 1
    db.commit()
    return {
        "dry_run": False,
        "count": fixed,
        "events_removed": events_removed,
        "by_source": by_source,
        "sample": repairs[:30],
    }
