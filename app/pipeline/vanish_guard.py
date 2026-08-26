"""When vanish reconcile is safe after a crawl."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Listing, utcnow
from app.domain.enums import ListingStatus

_ACTIVE = (ListingStatus.ACTIVE.value, ListingStatus.RELISTED.value)


def listing_geo_scope(listing: Listing) -> str | None:
    """kyiv | region for LUN; None for other sources / unknown."""
    if listing.source != "lun":
        return None
    extra = listing.raw_extra or {}
    z = extra.get("zone")
    if z in ("kyiv", "region"):
        return str(z)
    city = (listing.city or "").lower()
    if "област" in city or "oblast" in city:
        return "region"
    return "kyiv"


def count_active_for_source(db: Session, source: str, *, zone: str | None = None) -> int:
    rows = db.scalars(
        select(Listing).where(
            Listing.source == source,
            Listing.status.in_(_ACTIVE),
        )
    ).all()
    if zone is None:
        return len(rows)
    return sum(1 for x in rows if listing_geo_scope(x) == zone)


def count_fresh_active(
    db: Session,
    source: str,
    *,
    lookback_days: int | None = None,
    zone: str | None = None,
) -> int:
    """Active listings recently touched — ghosts older than lookback do not block vanish."""
    settings = get_settings()
    days = int(lookback_days if lookback_days is not None else settings.coverage_lookback_days)
    cutoff = utcnow() - timedelta(days=days)
    rows = db.scalars(
        select(Listing).where(
            Listing.source == source,
            Listing.status.in_(_ACTIVE),
            Listing.last_seen_at.is_not(None),
            Listing.last_seen_at >= cutoff,
        )
    ).all()
    if zone is None:
        return len(rows)
    return sum(1 for x in rows if listing_geo_scope(x) == zone)


def count_stale_active(
    db: Session,
    source: str,
    *,
    lookback_days: int | None = None,
    zone: str | None = None,
) -> int:
    total = count_active_for_source(db, source, zone=zone)
    fresh = count_fresh_active(db, source, lookback_days=lookback_days, zone=zone)
    return max(0, total - fresh)


def vanish_allowed(
    db: Session,
    source: str,
    seen_count: int,
    *,
    zone: str | None = None,
) -> tuple[bool, str]:
    """Return (ok, reason). Ratio uses fresh (lookback) active, not all-time ghosts."""
    settings = get_settings()
    if seen_count <= 0:
        return False, "seen=0"
    if seen_count < settings.min_seen_for_vanish:
        return False, f"seen={seen_count} < min={settings.min_seen_for_vanish}"

    lookback = int(settings.coverage_lookback_days)
    fresh = count_fresh_active(db, source, lookback_days=lookback, zone=zone)
    total = count_active_for_source(db, source, zone=zone)
    stale = max(0, total - fresh)
    scope = f", zone={zone}" if zone else ""

    if fresh <= 0:
        return True, f"no fresh baseline{scope}, seen={seen_count}, stale={stale}"

    ratio = seen_count / fresh
    min_ratio = float(settings.vanish_min_active_ratio)
    if ratio < min_ratio:
        return (
            False,
            (
                f"seen/fresh={ratio:.2%} < {min_ratio:.0%} "
                f"(seen={seen_count}, fresh={fresh}, stale_ignored={stale}{scope})"
            ),
        )
    return (
        True,
        (
            f"seen={seen_count}, fresh={fresh}, stale_ignored={stale}, "
            f"ratio={ratio:.2%}{scope}"
        ),
    )


def apply_vanish_reconcile(
    db: Session, sources_seen: dict[str, set[str]],
    *,
    scoped_seen: dict[tuple[str, str], set[str]] | None = None,
) -> dict[str, dict]:
    """Run vanish after a full ingest pass, per source (and LUN zone when known)."""
    from app.pipeline.reconcile import mark_vanished

    out: dict[str, dict] = {}
    scoped_seen = scoped_seen or {}

    for source, seen_ids in sources_seen.items():
        lun_zones = {
            z: ids
            for (src, z), ids in scoped_seen.items()
            if src == source and z in ("kyiv", "region") and ids
        }
        if source == "lun" and lun_zones:
            zone_out: dict[str, dict] = {}
            total_vanished = 0
            any_ok = False
            for zone, ids in lun_zones.items():
                ok, reason = vanish_allowed(db, source, len(ids), zone=zone)
                if not ok:
                    zone_out[zone] = {"vanished": 0, "skipped": True, "reason": reason}
                    continue
                n = mark_vanished(db, source, ids, zone=zone)
                total_vanished += n
                any_ok = True
                zone_out[zone] = {"vanished": n, "skipped": False, "reason": reason}
            out[source] = {
                "vanished": total_vanished,
                "skipped": not any_ok,
                "reason": "per-zone",
                "zones": zone_out,
            }
            continue

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
