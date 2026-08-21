"""Collect and read daily market snapshots for charts."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import MarketStatSnapshot, utcnow
from app.domain.market_stats import (
    KYIV_DISTRICTS,
    compute_all_market_stats,
    count_active_inventory,
    pick_rent_market_slice,
)


def _day_key(when: datetime | None = None) -> str:
    dt = when or utcnow()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


def record_market_snapshot(db: Session, *, force: bool = False) -> MarketStatSnapshot:
    """Upsert today's city snapshot (one row per UTC day)."""
    day = _day_key()
    existing = db.scalar(select(MarketStatSnapshot).where(MarketStatSnapshot.day == day))
    if existing and not force:
        # Refresh same-day numbers so crawl updates the chart point.
        snap = existing
    else:
        snap = existing or MarketStatSnapshot(day=day)

    market = compute_all_market_stats(db)
    inventory = count_active_inventory(db)
    sale = market["sale"]
    rent = pick_rent_market_slice(market)

    sale_by = {d.district: d for d in sale.districts}
    rent_by = {d.district: d for d in rent.districts}
    inv_by = {d["district"]: d for d in inventory["districts"]}
    districts = []
    for name in KYIV_DISTRICTS:
        s = sale_by.get(name)
        r = rent_by.get(name)
        inv = inv_by.get(name) or {}
        if not s and not r and not inv:
            continue
        districts.append(
            {
                "district": name,
                "sale_median": s.median_psm if s else None,
                "sale_avg": s.avg_psm if s else None,
                "sale_n": s.count if s else 0,
                "rent_median": r.median_psm if r else None,
                "rent_avg": r.avg_psm if r else None,
                "rent_n": r.count if r else 0,
                "sale_active": inv.get("sale", 0),
                "rent_active": inv.get("rent", 0),
            }
        )

    snap.captured_at = utcnow()
    snap.sale_median_psm = sale.city_median_psm
    snap.sale_avg_psm = sale.city_avg_psm
    snap.sale_sample_n = int(sale.city_count or 0)
    snap.sale_active_n = int(inventory["sale_total"])
    snap.rent_median_psm = rent.city_median_psm
    snap.rent_avg_psm = rent.city_avg_psm
    snap.rent_sample_n = int(rent.city_count or 0)
    snap.rent_active_n = int(inventory["rent_total"])
    snap.payload = {"districts": districts}

    if snap.id is None:
        db.add(snap)
    db.commit()
    db.refresh(snap)
    return snap


def ensure_today_snapshot(db: Session) -> MarketStatSnapshot:
    """Return today's snapshot; compute only if missing (do not rebuild on every page view)."""
    day = _day_key()
    existing = db.scalar(select(MarketStatSnapshot).where(MarketStatSnapshot.day == day))
    if existing is not None:
        return existing
    return record_market_snapshot(db, force=True)


def load_snapshot_series(db: Session, *, limit: int = 90) -> list[MarketStatSnapshot]:
    rows = list(
        db.scalars(
            select(MarketStatSnapshot)
            .order_by(MarketStatSnapshot.day.desc())
            .limit(max(1, min(limit, 365)))
        ).all()
    )
    rows.reverse()
    return rows


def series_for_charts(db: Session, *, limit: int = 90) -> dict:
    rows = load_snapshot_series(db, limit=limit)
    return {
        "labels": [r.day for r in rows],
        "sale_median": [r.sale_median_psm for r in rows],
        "sale_avg": [r.sale_avg_psm for r in rows],
        "rent_median": [r.rent_median_psm for r in rows],
        "rent_avg": [r.rent_avg_psm for r in rows],
        "sale_active": [r.sale_active_n for r in rows],
        "rent_active": [r.rent_active_n for r in rows],
        "n": len(rows),
        "latest": rows[-1] if rows else None,
    }
