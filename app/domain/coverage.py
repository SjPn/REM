"""Crawl coverage vs fresh active inventory — when vanish is safe."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import CrawlRun
from app.pipeline.vanish_guard import (
    count_active_for_source,
    count_fresh_active,
    count_stale_active,
    vanish_allowed,
)
from app.scrapers import SCRAPERS


@dataclass
class SourceCoverage:
    source: str
    active: int
    fresh_active: int
    stale_active: int
    last_seen: int | None
    last_pages: int | None
    last_status: str | None
    last_finished_at: datetime | None
    last_error: str | None
    ratio: float | None
    ratio_vs_all: float | None
    vanish_ok: bool
    vanish_reason: str
    target_ratio: float
    lookback_days: int
    gap_to_target: int | None
    note: str
    zone: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        if self.last_finished_at is not None:
            d["last_finished_at"] = self.last_finished_at.isoformat()
        return d


def _last_crawl(db: Session, source: str) -> CrawlRun | None:
    return db.scalar(
        select(CrawlRun)
        .where(CrawlRun.source == source)
        .order_by(CrawlRun.started_at.desc())
        .limit(1)
    )


def coverage_for_source(
    db: Session,
    source: str,
    *,
    seen_override: int | None = None,
    zone: str | None = None,
) -> SourceCoverage:
    settings = get_settings()
    target = float(settings.vanish_min_active_ratio)
    lookback = int(settings.coverage_lookback_days)
    active = count_active_for_source(db, source, zone=zone)
    fresh = count_fresh_active(db, source, lookback_days=lookback, zone=zone)
    stale = count_stale_active(db, source, lookback_days=lookback, zone=zone)
    run = _last_crawl(db, source)
    seen = seen_override
    if seen is None and run is not None:
        seen = int(run.listings_seen or 0)
    pages = int(run.pages_fetched) if run else None
    status = run.status if run else None
    finished = run.finished_at or run.started_at if run else None
    err = run.error if run else None
    label = f"{source}:{zone}" if zone else source

    # Zone rows are inventory splits; per-zone seen is only known during crawl.
    if zone is not None and seen_override is None:
        return SourceCoverage(
            source=label,
            active=active,
            fresh_active=fresh,
            stale_active=stale,
            last_seen=None,
            last_pages=pages,
            last_status=status,
            last_finished_at=finished,
            last_error=err,
            ratio=None,
            ratio_vs_all=None,
            vanish_ok=False,
            vanish_reason="zone inventory",
            target_ratio=target,
            lookback_days=lookback,
            gap_to_target=None,
            note=(
                f"Сегмент LUN ({zone}): fresh={fresh}, stale={stale}. "
                "Vanish считается отдельно по зоне во время crawl."
            ),
            zone=zone,
        )

    if seen is None:
        ok, reason = False, "нет успешного crawl"
        ratio = None
        ratio_all = None
        gap = None
        note = f"Ещё не было сбора ({label})"
    else:
        ok, reason = vanish_allowed(db, source, int(seen), zone=zone)
        ratio = (float(seen) / fresh) if fresh > 0 else None
        ratio_all = (float(seen) / active) if active > 0 else None
        if fresh > 0 and ratio is not None and ratio < target:
            gap = max(0, int(fresh * target) - int(seen))
        else:
            gap = 0
        if ok:
            note = (
                f"Coverage достаточный для vanish "
                f"(fresh={fresh}, stale_ignored={stale})"
            )
        elif ratio is not None and ratio < target:
            note = (
                f"Мало покрытия fresh: увидели {seen} из {fresh} "
                f"за {lookback}д ({ratio:.0%}, нужно >={target:.0%}). "
                f"Stale {stale} не в знаменателе. Detail-enrich это не чинит."
            )
        else:
            note = reason

    return SourceCoverage(
        source=label,
        active=active,
        fresh_active=fresh,
        stale_active=stale,
        last_seen=seen,
        last_pages=pages,
        last_status=status,
        last_finished_at=finished,
        last_error=err,
        ratio=round(ratio, 4) if ratio is not None else None,
        ratio_vs_all=round(ratio_all, 4) if ratio_all is not None else None,
        vanish_ok=ok,
        vanish_reason=reason,
        target_ratio=target,
        lookback_days=lookback,
        gap_to_target=gap,
        note=note,
        zone=zone,
    )


def coverage_report(
    db: Session,
    sources: list[str] | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    srcs = sources or list(SCRAPERS.keys())
    rows: list[SourceCoverage] = []
    for s in srcs:
        rows.append(coverage_for_source(db, s))
        if s == "lun":
            rows.append(coverage_for_source(db, s, zone="kyiv"))
            rows.append(coverage_for_source(db, s, zone="region"))
    # Ready = primary sources (not zone rows) that pass
    primary = [r for r in rows if r.zone is None]
    ready = sum(1 for r in primary if r.vanish_ok)
    return {
        "target_ratio": float(settings.vanish_min_active_ratio),
        "min_seen_for_vanish": int(settings.min_seen_for_vanish),
        "lookback_days": int(settings.coverage_lookback_days),
        "sources_ready_for_vanish": ready,
        "sources_total": len(primary),
        "all_ready": ready == len(primary) and len(primary) > 0,
        "ops": {
            "watch": "ежедневно, без vanish (WATCH_APPLY_VANISH=false)",
            "full": (
                "реже, много страниц; vanish если seen/fresh >= "
                f"{settings.vanish_min_active_ratio:.0%} "
                f"(fresh = last_seen за {settings.coverage_lookback_days}д)"
            ),
            "details": "detail-enrich != coverage; stale ghosts ignored in ratio",
            "lun": "coverage/vanish отдельно для lun:kyiv и lun:region",
        },
        "sources": [r.to_dict() for r in rows],
    }


def recent_crawls(db: Session, *, limit: int = 30) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(CrawlRun).order_by(CrawlRun.started_at.desc()).limit(limit)
    ).all()
    out: list[dict[str, Any]] = []
    lookback = int(get_settings().coverage_lookback_days)
    for r in rows:
        active = count_active_for_source(db, r.source)
        fresh = count_fresh_active(db, r.source, lookback_days=lookback)
        seen = int(r.listings_seen or 0)
        ratio = (seen / fresh) if fresh > 0 else None
        ok, reason = vanish_allowed(db, r.source, seen) if seen else (False, "seen=0")
        out.append(
            {
                "id": r.id,
                "source": r.source,
                "status": r.status,
                "started_at": r.started_at.isoformat() if r.started_at else None,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "pages_fetched": r.pages_fetched,
                "listings_seen": seen,
                "active_now": active,
                "fresh_active": fresh,
                "stale_active": max(0, active - fresh),
                "ratio": round(ratio, 4) if ratio is not None else None,
                "vanish_would_ok": ok,
                "vanish_reason": reason,
                "error": r.error,
            }
        )
    return out


def backfill_pages_for_source(source: str, override: int | None = None) -> int:
    settings = get_settings()
    if override is not None:
        return int(override)
    if source == "lun":
        return int(settings.backfill_lun_max_pages)
    return int(settings.backfill_max_pages)
