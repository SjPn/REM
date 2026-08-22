"""Compare list-card fields to skip redundant detail fetches in watch crawl."""

from __future__ import annotations

from app.db.models import Listing
from app.scrapers.base import RawListing


def _float_eq(a: float | None, b: float | None) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    try:
        return abs(float(a) - float(b)) < 0.01
    except (TypeError, ValueError):
        return False


def list_card_changed(existing: Listing, raw: RawListing) -> bool:
    """True if list-card data differs enough to warrant a detail fetch."""
    if not _float_eq(existing.price, raw.price):
        return True
    if (existing.currency or "USD") != (raw.currency or "USD"):
        return True
    if (raw.title or "").strip() and (raw.title or "").strip() != (existing.title or "").strip():
        return True
    if not _float_eq(existing.area_sqm, raw.area_sqm):
        return True
    if raw.source_status_raw and raw.source_status_raw != existing.source_status_raw:
        return True
    return False


def needs_detail_fetch(existing: Listing | None, raw: RawListing) -> bool:
    """New listings and changed list cards need detail; stable cards do not."""
    if existing is None:
        return True
    if list_card_changed(existing, raw):
        return True
    # Stale detail: never enriched or missing phone on an old card.
    extra = existing.raw_extra or {}
    if extra.get("skip_detail"):
        return True
    if existing.phone is None and (existing.description or "").strip() == "":
        return True
    return False
