"""Repair listings where spaced totals like «5 300 $» were stored as trailing 300."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Listing
from app.scrapers.http_utils import parse_price

_SPACED_TOTAL = re.compile(
    r"(?<!\d)(\d{1,3})[ \u00a0](\d{3})(?!\d)\s*(?:\$|USD|дол)",
    re.IGNORECASE,
)
_RENT_HINT = re.compile(r"міс|мес|/ ?mo|month", re.IGNORECASE)


def _blob(listing: Listing) -> str:
    return " ".join(
        t for t in (listing.title, listing.description, listing.address_raw) if t
    )


def _plausible_fix(
    listing: Listing,
    *,
    old_price: float,
    new_price: float,
    match_text: str,
) -> bool:
    if new_price <= old_price or new_price < 1000:
        return False
    deal = (listing.deal_type or "").lower()
    area = float(listing.area_sqm or 0)
    if deal == "rent":
        # Prefer explicit monthly total; else require sane rent $/m² after fix.
        if _RENT_HINT.search(match_text) or _RENT_HINT.search(_blob(listing)[:800]):
            return True
        if area >= 20:
            psm = new_price / area
            return 4.0 <= psm <= 80.0
        return new_price <= 50_000
    # sale: only large totals (avoid treating «3 188 $» $/m² chip as whole price)
    if new_price < 20_000:
        return False
    if area >= 20:
        psm = new_price / area
        return 200.0 <= psm <= 15_000.0
    return True


def find_spaced_price_repairs(db: Session) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(Listing).where(Listing.status.in_(("active", "relisted")))
    ).all()
    out: list[dict[str, Any]] = []
    for listing in rows:
        if listing.price is None:
            continue
        blob = _blob(listing)
        if not blob:
            continue
        old = float(listing.price)
        for m in _SPACED_TOTAL.finditer(blob):
            head, tail = int(m.group(1)), int(m.group(2))
            full = head * 1000 + tail
            if abs(old - float(tail)) > 0.01:
                continue
            if full == tail:
                continue
            parsed, cur = parse_price(m.group(0))
            new_price = float(parsed) if parsed is not None else float(full)
            if not _plausible_fix(
                listing, old_price=old, new_price=new_price, match_text=m.group(0)
            ):
                continue
            area = listing.area_sqm
            new_psm = (
                round(new_price / float(area), 4)
                if area and float(area) > 0
                else listing.price_per_sqm
            )
            out.append(
                {
                    "listing_id": listing.id,
                    "source": listing.source,
                    "deal_type": listing.deal_type,
                    "url": listing.url,
                    "old_price": old,
                    "new_price": new_price,
                    "currency": cur or listing.currency,
                    "old_psm": listing.price_per_sqm,
                    "new_psm": new_psm,
                    "match": m.group(0)[:48],
                }
            )
            break
    return out


def repair_spaced_price_bugs(db: Session, *, dry_run: bool = True) -> dict[str, Any]:
    repairs = find_spaced_price_repairs(db)
    if dry_run:
        return {"dry_run": True, "count": len(repairs), "repairs": repairs[:50]}

    fixed = 0
    by_source: dict[str, int] = {}
    for item in repairs:
        listing = db.get(Listing, int(item["listing_id"]))
        if not listing:
            continue
        listing.price = float(item["new_price"])
        if item.get("currency"):
            listing.currency = str(item["currency"])
        if item.get("new_psm") is not None:
            listing.price_per_sqm = float(item["new_psm"])
        fixed += 1
        by_source[listing.source] = by_source.get(listing.source, 0) + 1
    db.commit()
    return {
        "dry_run": False,
        "count": fixed,
        "by_source": by_source,
        "sample": repairs[:20],
    }
