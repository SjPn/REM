"""Repair sale totals taken from the token after «$» (a date day or the next area)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Listing, ListingSnapshot, PropertyEvent
from app.domain.enums import EventType
from app.domain.listing_stats import apply_auto_stats_exclusion
from app.domain.market_stats import to_usd
from app.domain.pricing import sale_psm_suspicious
from app.scrapers.http_utils import parse_price, parse_price_per_sqm

# Below this implied $/m² a commercial sale total is the stray token, not the price.
_ABSURD_SALE_PSM_USD = 20.0
_MIN_TOTAL = 10_000.0
_PSM_LO = 200.0
_PSM_HI = 15_000.0


def _blob(listing: Listing) -> str:
    return " ".join(t for t in (listing.title, listing.description, listing.address_raw) if t)


def _plausible_sale(new_price: float, currency: str | None, area: float) -> bool:
    if new_price < _MIN_TOTAL or area < 20:
        return False
    psm = to_usd(new_price / area, currency)
    if psm is None:
        return False
    return _PSM_LO <= psm <= _PSM_HI


def find_dollar_suffix_repairs(db: Session) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(Listing).where(
            Listing.status.in_(("active", "relisted")),
            Listing.deal_type == "sale",
        )
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
        if old <= 0 or area < 20:
            continue
        implied = to_usd(old / area, listing.currency)
        if implied is None or implied >= _ABSURD_SALE_PSM_USD:
            continue
        blob = _blob(listing)
        if not blob:
            continue
        parsed, cur = parse_price(blob)
        if parsed is None:
            continue
        new_price = float(parsed)
        currency = (cur or listing.currency or "USD").upper()
        if new_price <= old * 8:
            continue
        if not _plausible_sale(new_price, currency, area):
            continue
        psm_text, _psm_cur = parse_price_per_sqm(blob)
        new_psm = round(new_price / area, 4)
        if psm_text is not None and area > 0:
            rate_usd = to_usd(float(psm_text), currency)
            implied_usd = to_usd(new_psm, currency)
            if (
                rate_usd is not None
                and implied_usd is not None
                and abs(rate_usd - implied_usd) / max(implied_usd, 0.5) <= 0.12
            ):
                new_psm = float(psm_text)
        out.append(
            {
                "listing_id": listing.id,
                "source": listing.source,
                "url": listing.url,
                "old_price": old,
                "new_price": new_price,
                "currency": currency,
                "old_psm": listing.price_per_sqm,
                "new_psm": new_psm,
            }
        )
    return out


def _drop_fake_price_events(db: Session, listing: Listing, old_price: float) -> int:
    """Remove PRICE_CHANGED rows that recorded the stray token as a price cut."""
    events = db.scalars(
        select(PropertyEvent).where(
            PropertyEvent.listing_id == listing.id,
            PropertyEvent.event_type == EventType.PRICE_CHANGED.value,
        )
    ).all()
    removed = 0
    for ev in events:
        payload = ev.payload or {}
        try:
            new_p = float(payload["new_price"])
            old_p = float(payload["old_price"])
        except (KeyError, TypeError, ValueError):
            continue
        if abs(new_p - old_price) < 0.01 and old_p > new_p * 8:
            db.delete(ev)
            removed += 1
    if removed and listing.price_drop_count:
        listing.price_drop_count = max(0, int(listing.price_drop_count) - removed)
    return removed


def repair_dollar_suffix_prices(db: Session, *, dry_run: bool = True) -> dict[str, Any]:
    repairs = find_dollar_suffix_repairs(db)
    if dry_run:
        by_source: dict[str, int] = {}
        for item in repairs:
            by_source[item["source"]] = by_source.get(item["source"], 0) + 1
        return {
            "dry_run": True,
            "count": len(repairs),
            "by_source": by_source,
            "sample": repairs[:20],
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
        events_removed += _drop_fake_price_events(db, listing, old)
        extra = dict(listing.raw_extra or {})
        extra.pop("price_suspicious", None)
        extra["price_repair"] = "dollar_suffix"
        listing.raw_extra = extra
        psm_usd = to_usd(listing.price_per_sqm, listing.currency)
        apply_auto_stats_exclusion(
            listing, suspicious=sale_psm_suspicious(psm_usd)
        )
        fixed += 1
        by_source[listing.source] = by_source.get(listing.source, 0) + 1
    db.commit()
    return {
        "dry_run": False,
        "count": fixed,
        "events_removed": events_removed,
        "by_source": by_source,
        "sample": repairs[:20],
    }
