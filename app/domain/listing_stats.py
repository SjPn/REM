"""Exclude noisy listings from medians / market stats."""

from __future__ import annotations

from app.db.models import Listing


def is_excluded_from_stats(listing: Listing) -> bool:
    return bool(getattr(listing, "exclude_from_stats", False))


def apply_auto_stats_exclusion(listing: Listing, *, suspicious: bool) -> None:
    """Auto-check when price is suspicious; respect manual user clearance."""
    extra = dict(listing.raw_extra or {})
    if extra.get("stats_review_ok"):
        return
    if suspicious:
        listing.exclude_from_stats = True


def set_stats_exclusion(
    listing: Listing,
    *,
    excluded: bool,
    user_action: bool = True,
) -> None:
    listing.exclude_from_stats = excluded
    if not user_action:
        return
    extra = dict(listing.raw_extra or {})
    if excluded:
        extra.pop("stats_review_ok", None)
    else:
        extra["stats_review_ok"] = True
    listing.raw_extra = extra or None
