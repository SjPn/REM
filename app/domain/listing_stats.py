"""Exclude noisy listings from medians / market stats."""

from __future__ import annotations

from app.db.models import Listing


def is_excluded_from_stats(listing: Listing) -> bool:
    return bool(getattr(listing, "exclude_from_stats", False))


def apply_auto_stats_exclusion(listing: Listing, *, suspicious: bool) -> None:
    """Auto-check when price is suspicious; clear when repaired unless user opted out."""
    extra = dict(listing.raw_extra or {})
    if extra.get("stats_review_ok"):
        return
    if extra.get("stats_exclude_manual"):
        return
    if suspicious:
        listing.exclude_from_stats = True
        extra["stats_auto_excluded"] = True
    else:
        if extra.get("stats_auto_excluded") or listing.exclude_from_stats:
            listing.exclude_from_stats = False
        extra.pop("stats_auto_excluded", None)
    listing.raw_extra = extra or None


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
        extra["stats_exclude_manual"] = True
        extra.pop("stats_auto_excluded", None)
    else:
        extra["stats_review_ok"] = True
        extra.pop("stats_exclude_manual", None)
        extra.pop("stats_auto_excluded", None)
    listing.raw_extra = extra or None
