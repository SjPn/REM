"""Crawl coverage vs active inventory — when vanish is safe."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import CrawlRun
from app.pipeline.vanish_guard import count_active_for_source, vanish_allowed
from app.scrapers import SCRAPERS


@dataclass
class SourceCoverage:
    source: str
    active: int
    last_seen: int | None
    last_pages: int | None
    last_status: str | None
    last_finished_at: datetime | None
    last_error: str | None
    ratio: float | None
    vanish_ok: bool
    vanish_reason: str
    target_ratio: float
    gap_to_target: int | None  # how many more listings to see to hit target
    note: str

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
) -> SourceCoverage:
    settings = get_settings()
    target = float(settings.vanish_min_active_ratio)
    active = count_active_for_source(db, source)
    run = _last_crawl(db, source)
    seen = seen_override
    if seen is None and run is not None:
        seen = int(run.listings_seen or 0)
    pages = int(run.pages_fetched) if run else None
    status = run.status if run else None
    finished = run.finished_at or run.started_at if run else None
    err = run.error if run else None

    if seen is None:
        ok, reason = False, "нет успешного crawl"
        ratio = None
        gap = None
        note = "Ещё не было сбора по источнику"
    else:
        ok, reason = vanish_allowed(db, source, int(seen))
        ratio = (float(seen) / active) if active > 0 else None
        if active > 0 and ratio is not None and ratio < target:
            gap = max(0, int(active * target) - int(seen))
        else:
            gap = 0
        if ok:
            note = "Coverage достаточный для vanish"
        elif ratio is not None and ratio < target:
            note = (
                f"Мало страниц ленты: увидели {seen} из ~{active} активных "
                f"({ratio:.0%}, нужно >={target:.0%}). Detail-enrich это не чинит."
            )
        else:
            note = reason

    return SourceCoverage(
        source=source,
        active=active,
        last_seen=seen,
        last_pages=pages,
        last_status=status,
        last_finished_at=finished,
        last_error=err,
        ratio=round(ratio, 4) if ratio is not None else None,
        vanish_ok=ok,
        vanish_reason=reason,
        target_ratio=target,
        gap_to_target=gap,
        note=note,
    )


def coverage_report(
    db: Session,
    sources: list[str] | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    srcs = sources or list(SCRAPERS.keys())
    rows = [coverage_for_source(db, s) for s in srcs]
    ready = sum(1 for r in rows if r.vanish_ok)
    return {
        "target_ratio": float(settings.vanish_min_active_ratio),
        "min_seen_for_vanish": int(settings.min_seen_for_vanish),
        "sources_ready_for_vanish": ready,
        "sources_total": len(rows),
        "all_ready": ready == len(rows) and len(rows) > 0,
        "ops": {
            "watch": "ежедневно, без vanish (WATCH_APPLY_VANISH=false)",
            "full": (
                "реже, много страниц; vanish только при coverage >= "
                f"{settings.vanish_min_active_ratio:.0%}"
            ),
            "details": "detail-enrich != coverage lenty",
        },
        "sources": [r.to_dict() for r in rows],
    }


def recent_crawls(db: Session, *, limit: int = 30) -> list[dict[str, Any]]:
    rows = db.scalars(
        select(CrawlRun).order_by(CrawlRun.started_at.desc()).limit(limit)
    ).all()
    out: list[dict[str, Any]] = []
    for r in rows:
        active = count_active_for_source(db, r.source)
        seen = int(r.listings_seen or 0)
        ratio = (seen / active) if active > 0 else None
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
