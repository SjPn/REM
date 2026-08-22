"""When vanish reconcile is safe after a crawl."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Listing
from app.domain.enums import ListingStatus


def count_active_for_source(db: Session, source: str) -> int:
    return (
        db.scalar(
            select(func.count())
            .select_from(Listing)
            .where(
                Listing.source == source,
                Listing.status.in_(
                    [ListingStatus.ACTIVE.value, ListingStatus.RELISTED.value]
                ),
            )
        )
        or 0
    )


def vanish_allowed(db: Session, source: str, seen_count: int) -> tuple[bool, str]:
    """Return (ok, reason). Blocks partial crawls from mass-vanishing inventory."""
    settings = get_settings()
    if seen_count <= 0:
        return False, "seen=0"
    if seen_count < settings.min_seen_for_vanish:
        return False, f"seen={seen_count} < min={settings.min_seen_for_vanish}"

    active = count_active_for_source(db, source)
    if active <= 0:
        return True, f"no active baseline, seen={seen_count}"

    ratio = seen_count / active
    min_ratio = float(settings.vanish_min_active_ratio)
    if ratio < min_ratio:
        return (
            False,
            f"seen/active={ratio:.2%} < {min_ratio:.0%} (partial crawl?)",
        )
    return True, f"seen={seen_count}, active={active}, ratio={ratio:.2%}"


def apply_vanish_reconcile(
    db: Session, sources_seen: dict[str, set[str]]
) -> dict[str, dict]:
    """Run vanish after a full ingest pass (e.g. backfill), per source with coverage guard."""
    from app.pipeline.reconcile import mark_vanished

    out: dict[str, dict] = {}
    for source, seen_ids in sources_seen.items():
        if not seen_ids:
            out[source] = {"vanished": 0, "skipped": True, "reason": "seen=0"}
            continue
        ok, reason = vanish_allowed(db, source, len(seen_ids))
        if not ok:
            out[source] = {"vanished": 0, "skipped": True, "reason": reason}
            continue
        n = mark_vanished(db, source, seen_ids)
        out[source] = {"vanished": n, "skipped": False, "reason": reason}
    return out
