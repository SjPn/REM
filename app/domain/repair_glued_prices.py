"""Repair totals that swallowed a neighboring house number («73-79 100 900 ₴» → 79 млн)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Listing, ListingSnapshot, PropertyEvent
from app.domain.enums import EventType
from app.domain.listing_stats import apply_auto_stats_exclusion
from app.domain.market_stats import to_usd
from app.domain.pricing import psm_suspicious
from app.scrapers.http_utils import _collect_price_candidates, parse_price


def _blob(listing: Listing) -> str:
    return " ".join(t for t in (listing.title, listing.description, listing.address_raw) if t)


def _sane_psm(deal_type: str | None, psm_usd: float | None) -> bool:
    if psm_usd is None:
        return False
    if (deal_type or "").lower() == "rent":
        return 3.0 <= psm_usd <= 70.0
    if (deal_type or "").lower() == "sale":
        return 450.0 <= psm_usd <= 10_000.0
    return False


def _inflated_psm(deal_type: str | None, psm_usd: float | None) -> bool:
    if psm_usd is None:
        return False
    if (deal_type or "").lower() == "rent":
        return psm_usd > 70.0
    if (deal_type or "").lower() == "sale":
        return psm_usd > 10_000.0
    return False


def find_glued_house_price_repairs(db: Session) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(Listing).where(Listing.status.in_(("active", "relisted")))
    ).all()
    out: list[dict[str, Any]] = []
    for listing in rows:
        if listing.price is None or listing.area_sqm is None:
            continue
        try:
            old = float(listing.price)
            area = float(listing.area_sqm)
        except (TypeError, ValueError):
            continue
        if old <= 0 or area < 15:
            continue
        blob = _blob(listing)
        if not blob:
            continue
        parsed, cur = parse_price(blob)
        if parsed is None:
            continue
        new_price = float(parsed)
        # The stored total is still written on the card (two lots, one price). Leave it.
        if any(abs(cand[0] - old) < 1.0 for cand in _collect_price_candidates(blob)):
            continue
        currency = (cur or listing.currency or "USD").upper()
        old_usd = to_usd(old, listing.currency)
        new_usd = to_usd(new_price, currency)
        if old_usd is None or new_usd is None or old_usd <= new_usd * 3:
            continue
        old_psm = to_usd(old / area, listing.currency)
        new_psm = to_usd(new_price / area, currency)
        if not _inflated_psm(listing.deal_type, old_psm) or not _sane_psm(
            listing.deal_type, new_psm
        ):
            continue
        out.append(
            {
                "listing_id": listing.id,
                "source": listing.source,
                "deal_type": listing.deal_type,
                "url": listing.url,
                "old_price": old,
                "new_price": new_price,
                "currency": currency,
                "new_psm": round(new_price / area, 4),
            }
        )
    return out


def repair_glued_house_prices(db: Session, *, dry_run: bool = True) -> dict[str, Any]:
    repairs = find_glued_house_price_repairs(db)
    by_source: dict[str, int] = {}
    for item in repairs:
        by_source[item["source"]] = by_source.get(item["source"], 0) + 1
    if dry_run:
        return {
            "dry_run": True,
            "count": len(repairs),
            "by_source": by_source,
            "sample": repairs[:15],
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
        listing.currency = str(item["currency"])
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
        extra["price_repair"] = "glued_house"
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
        "sample": repairs[:15],
    }
