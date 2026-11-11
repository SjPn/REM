"""Undo a monthly rent that was multiplied by area.

3 000 грн for 27 m² is about $2.5/m² and used to be treated as a rate, so the
stored total became 81 000. A real rate («15 $/м²», «1000 грн./кв.м») stays.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Listing, ListingSnapshot, PropertyEvent
from app.domain.enums import EventType
from app.domain.listing_stats import apply_auto_stats_exclusion
from app.domain.market_stats import to_usd
from app.domain.pricing import extract_explicit_psm, normalize_listing_price, psm_suspicious
from app.scrapers.http_utils import parse_price

_HOURLY = re.compile(
    r"погодин|почасов|посуточ|за\s+годину|за\s+час\b|на\s+день|/\s*день",
    re.IGNORECASE,
)
_M2_HRYVNIA = re.compile(r"(\d{1,3}(?:[ \u00a0]\d{3})+|\d+)\s*₴")
_TITLE_PRICE = re.compile(r"ціна\s*(\d[\d \u00a0]{0,12})\s*грн", re.IGNORECASE)


def _amount(raw: str) -> float | None:
    cleaned = raw.replace("\xa0", "").replace(" ", "")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    if value <= 0:
        return None
    return value


def _marked_monthly_total(source: str | None, title: str | None, description: str | None) -> float | None:
    """Hryvnia amount the card itself calls the price, not a rate and not a day fee."""
    text = " ".join(t for t in (title, description) if t)
    if not text or _HOURLY.search(text):
        return None
    if extract_explicit_psm(text) is not None:
        return None
    marked: float | None = None
    if (source or "") == "m2bomber":
        m = _M2_HRYVNIA.search(text)
        if m:
            marked = _amount(m.group(1))
    title_m = _TITLE_PRICE.search(title or "")
    if title_m:
        marked = _amount(title_m.group(1))
    return marked


def restored_monthly_rent(
    *,
    price: float | None,
    currency: str | None,
    area_sqm: float | None,
    deal_type: str | None,
    title: str | None,
    description: str | None,
    source: str | None,
) -> float | None:
    """Monthly total to restore, when the stored price is that total times the area."""
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
    marked = _marked_monthly_total(source, title, description)
    if marked is None:
        return None
    text = " ".join(t for t in (title, description) if t)
    parsed, cur = parse_price(text)
    if parsed is None or cur is None:
        return None
    if (cur or "").upper() != "UAH" or (currency or "").upper() != "UAH":
        return None
    if abs(float(parsed) - marked) / marked > 0.05:
        return None
    if stored <= marked * 2:
        return None
    if abs(stored / area - marked) / marked > 0.05:
        return None
    norm = normalize_listing_price(
        price=marked,
        currency="UAH",
        area_sqm=area,
        deal_type="rent",
        title=title,
        description=description,
    )
    if norm.price is None or norm.reinterpreted_as_psm:
        return None
    if abs(float(norm.price) - marked) / marked > 0.05:
        return None
    return marked


def find_false_rent_expansions(db: Session) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(Listing).where(
            Listing.deal_type == "rent",
            Listing.status.in_(("active", "relisted")),
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
        area = float(listing.area_sqm)
        out.append(
            {
                "listing_id": listing.id,
                "source": listing.source,
                "url": listing.url,
                "old_price": float(listing.price),
                "new_price": restored,
                "currency": "UAH",
                "new_psm": round(restored / area, 4),
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
        listing.currency = "UAH"
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
