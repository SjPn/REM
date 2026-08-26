"""When vanish reconcile is safe after a crawl."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Listing, utcnow
from app.domain.enums import ListingStatus

_ACTIVE = (ListingStatus.ACTIVE.value, ListingStatus.RELISTED.value)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _is_fresh(last_seen: datetime | None, cutoff: datetime) -> bool:
    ls = _aware(last_seen)
    if ls is None:
        return False
    return ls >= _aware(cutoff)  # type: ignore[operator]


def listing_geo_scope(listing: Listing) -> str | None:
    """kyiv | region for LUN; None for other sources / unknown."""
    if listing.source != "lun":
        return None
    return geo_scope_from_fields(listing.city, listing.raw_extra)


def geo_scope_from_fields(city: str | None, raw_extra: dict | None) -> str:
    """Same rules as listing_geo_scope for LUN rows (always kyiv|region)."""
    extra = raw_extra or {}
    z = extra.get("zone") if isinstance(extra, dict) else None
    if z in ("kyiv", "region"):
        return str(z)
    city_l = (city or "").lower()
    if "област" in city_l or "oblast" in city_l:
        return "region"
    return "kyiv"


def _count_active_sql(
    db: Session,
    source: str,
    *,
    fresh_cutoff=None,
) -> int:
    q = select(func.count()).select_from(Listing).where(
        Listing.source == source,
        Listing.status.in_(_ACTIVE),
    )
    if fresh_cutoff is not None:
        q = q.where(
            Listing.last_seen_at.is_not(None),
            Listing.last_seen_at >= fresh_cutoff,
        )
    return int(db.scalar(q) or 0)


def _count_active_zone(
    db: Session,
    source: str,
    zone: str,
    *,
    fresh_cutoff=None,
) -> int:
    """Count by zone without loading full Listing ORM rows."""
    q = select(Listing.city, Listing.raw_extra).where(
        Listing.source == source,
        Listing.status.in_(_ACTIVE),
    )
    if fresh_cutoff is not None:
        q = q.where(
            Listing.last_seen_at.is_not(None),
            Listing.last_seen_at >= fresh_cutoff,
        )
    n = 0
    for city, extra in db.execute(q):
        if isinstance(extra, str):
            # defensive: some drivers may return JSON text
            extra_obj: Any = None
            try:
                import json

                extra_obj = json.loads(extra)
            except Exception:
                extra_obj = None
        else:
            extra_obj = extra
        if geo_scope_from_fields(city, extra_obj if isinstance(extra_obj, dict) else None) == zone:
            n += 1
    return n


def count_active_for_source(db: Session, source: str, *, zone: str | None = None) -> int:
    if zone is None:
        return _count_active_sql(db, source)
    return _count_active_zone(db, source, zone)


def count_source_zones(
    db: Session,
    source: str,
    *,
    lookback_days: int | None = None,
) -> dict[str, dict[str, int]]:
    """One pass: total/kyiv/region active + fresh (for LUN-style zoning)."""
    settings = get_settings()
    days = int(lookback_days if lookback_days is not None else settings.coverage_lookback_days)
    cutoff = utcnow() - timedelta(days=days)
    active = {"total": 0, "kyiv": 0, "region": 0}
    fresh = {"total": 0, "kyiv": 0, "region": 0}
    q = select(Listing.city, Listing.raw_extra, Listing.last_seen_at).where(
        Listing.source == source,
        Listing.status.in_(_ACTIVE),
    )
    for city, extra, last_seen in db.execute(q):
        if isinstance(extra, str):
            try:
                import json

                extra_obj: Any = json.loads(extra)
            except Exception:
                extra_obj = None
        else:
            extra_obj = extra
        z = geo_scope_from_fields(
            city, extra_obj if isinstance(extra_obj, dict) else None
        )
        active["total"] += 1
        active[z] = active.get(z, 0) + 1
        if _is_fresh(last_seen, cutoff):
            fresh["total"] += 1
            fresh[z] = fresh.get(z, 0) + 1
    return {"active": active, "fresh": fresh}


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
    if zone is None:
        return _count_active_sql(db, source, fresh_cutoff=cutoff)
    return _count_active_zone(db, source, zone, fresh_cutoff=cutoff)


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
    fresh: int | None = None,
    total: int | None = None,
) -> tuple[bool, str]:
    """Return (ok, reason). Ratio uses fresh (lookback) active, not all-time ghosts."""
    settings = get_settings()
    if seen_count <= 0:
        return False, "seen=0"
    if seen_count < settings.min_seen_for_vanish:
        return False, f"seen={seen_count} < min={settings.min_seen_for_vanish}"

    lookback = int(settings.coverage_lookback_days)
    if fresh is None:
        fresh = count_fresh_active(db, source, lookback_days=lookback, zone=zone)
    if total is None:
        total = count_active_for_source(db, source, zone=zone)
    stale = max(0, int(total) - int(fresh))
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
