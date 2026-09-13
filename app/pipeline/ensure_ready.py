"""Довести портал до готовности: coverage 5/5 → reconcile-vanish → rescore."""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import DealHypothesis, Listing
from app.domain.coverage import (
    backfill_pages_for_source,
    coverage_for_source,
    coverage_report,
)
from app.domain.ttl_cache import cache_clear
from app.pipeline.reconcile import rescore_all_vanished
from app.pipeline.runner import run_crawl
from app.scrapers import SCRAPERS

logger = logging.getLogger(__name__)


@dataclass
class SourceReadyStatus:
    source: str
    vanish_ok: bool
    fresh_active: int
    stale_active: int
    last_seen: int | None
    last_pages: int | None
    last_status: str | None
    last_finished_at: str | None
    vanish_reason: str
    note: str
    needs_refresh: bool = False  # ok только из‑за «нет fresh» / старый прогон


@dataclass
class ReadySnapshot:
    ready: int
    total: int
    all_ready: bool
    sources: list[SourceReadyStatus]
    deal_buckets: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "total": self.total,
            "all_ready": self.all_ready,
            "sources": [asdict(s) for s in self.sources],
            "deal_buckets": self.deal_buckets,
        }


def _primary_sources(sources: list[str] | None = None) -> list[str]:
    if sources:
        return list(sources)
    return list(SCRAPERS.keys())


def deal_bucket_counts(db: Session) -> dict[str, int]:
    rows = db.execute(
        select(DealHypothesis.bucket, func.count()).group_by(DealHypothesis.bucket)
    ).all()
    return {str(b): int(c) for b, c in rows}


def snapshot_readiness(
    db: Session,
    *,
    sources: list[str] | None = None,
    stale_crawl_days: int = 14,
) -> ReadySnapshot:
    """Статус primary-источников (без zone-строк LUN)."""
    cache_clear()
    srcs = _primary_sources(sources)
    report = coverage_report(db, sources=srcs)
    now = datetime.now(timezone.utc)
    statuses: list[SourceReadyStatus] = []
    for row in report.get("sources", []):
        if row.get("zone"):
            continue
        finished = row.get("last_finished_at")
        needs_refresh = False
        if row.get("vanish_ok") and int(row.get("fresh_active") or 0) <= 0:
            needs_refresh = True
        if finished and row.get("vanish_ok"):
            try:
                ft = datetime.fromisoformat(str(finished).replace("Z", "+00:00"))
                if ft.tzinfo is None:
                    ft = ft.replace(tzinfo=timezone.utc)
                age_days = (now - ft).total_seconds() / 86400.0
                if age_days > stale_crawl_days:
                    needs_refresh = True
            except Exception:  # noqa: BLE001
                needs_refresh = True
        statuses.append(
            SourceReadyStatus(
                source=str(row["source"]),
                vanish_ok=bool(row.get("vanish_ok")),
                fresh_active=int(row.get("fresh_active") or 0),
                stale_active=int(row.get("stale_active") or 0),
                last_seen=row.get("last_seen"),
                last_pages=row.get("last_pages"),
                last_status=row.get("last_status"),
                last_finished_at=str(finished) if finished else None,
                vanish_reason=str(row.get("vanish_reason") or ""),
                note=str(row.get("note") or ""),
                needs_refresh=needs_refresh,
            )
        )
    ready = sum(1 for s in statuses if s.vanish_ok and not s.needs_refresh)
    # Для UI «N/M» считаем vanish_ok; для финального reconcile требуем без needs_refresh.
    ui_ready = sum(1 for s in statuses if s.vanish_ok)
    total = len(statuses)
    return ReadySnapshot(
        ready=ui_ready,
        total=total,
        all_ready=ui_ready == total and total > 0 and ready == total,
        sources=statuses,
        deal_buckets=deal_bucket_counts(db),
    )


def _fill_source_until_ready(
    db: Session,
    source: str,
    *,
    page_ceiling: int,
    max_rounds: int,
    max_retries: int,
) -> dict[str, Any]:
    """Backfill одного источника до vanish_ok или потолка страниц."""
    settings = get_settings()
    target = float(settings.vanish_min_active_ratio)
    pages = backfill_pages_for_source(source)
    history: list[dict[str, Any]] = []
    rounds = 0

    while rounds < max_rounds:
        rounds += 1
        attempt = 0
        last_err: str | None = None
        while attempt < max_retries:
            attempt += 1
            try:
                logger.info(
                    "ensure-ready fill %s round=%s attempt=%s pages=%s",
                    source,
                    rounds,
                    attempt,
                    pages,
                )
                summary = run_crawl(
                    db,
                    sources=[source],
                    max_pages=pages,
                    apply_vanish=False,
                    apply_vanish_after=False,
                    mode="full",
                )
                cache_clear()
                cov = coverage_for_source(db, source)
                entry = {
                    "source": source,
                    "round": rounds,
                    "attempt": attempt,
                    "pages": pages,
                    "vanish_ok": cov.vanish_ok,
                    "seen": cov.last_seen,
                    "fresh": cov.fresh_active,
                    "ratio": cov.ratio,
                    "reason": cov.vanish_reason,
                    "crawl_status": (summary.get("sources") or {}).get(source),
                }
                history.append(entry)
                last_err = None
                break
            except Exception as exc:  # noqa: BLE001
                last_err = str(exc)
                logger.exception("ensure-ready crawl failed %s: %s", source, exc)
                time.sleep(min(30, 5 * attempt))
        if last_err:
            history.append(
                {
                    "source": source,
                    "round": rounds,
                    "error": last_err,
                    "pages": pages,
                }
            )
            # следующий round с тем же pages — ещё раз; иначе выходим
            if rounds >= max_rounds:
                break
            continue

        cov = coverage_for_source(db, source)
        if cov.vanish_ok:
            return {"source": source, "ok": True, "history": history}

        if pages >= page_ceiling:
            return {
                "source": source,
                "ok": False,
                "history": history,
                "stuck": "page_ceiling",
                "reason": cov.vanish_reason,
            }

        ratio = cov.ratio or 0.0
        if ratio <= 0.01:
            nxt = min(page_ceiling, max(pages * 2, pages + 10))
        else:
            nxt = int(pages * (target / max(ratio, 0.05)) * 1.15)
            nxt = max(pages + 10, nxt)
        nxt = min(page_ceiling, nxt)
        if nxt <= pages:
            return {
                "source": source,
                "ok": False,
                "history": history,
                "stuck": "no_page_growth",
                "reason": cov.vanish_reason,
            }
        pages = nxt

    cov = coverage_for_source(db, source)
    return {
        "source": source,
        "ok": bool(cov.vanish_ok),
        "history": history,
        "stuck": None if cov.vanish_ok else "max_rounds",
        "reason": cov.vanish_reason,
    }


def ensure_portal_ready(
    db: Session,
    *,
    sources: list[str] | None = None,
    page_ceiling: int | None = None,
    max_rounds_per_source: int = 8,
    max_retries: int = 3,
    skip_reconcile: bool = False,
    skip_rescore: bool = False,
    refresh_stale_ok: bool = True,
) -> dict[str, Any]:
    """
    1) Добить coverage до vanish_ok по всем primary источникам.
    2) Обновить «ok без fresh» / устаревшие прогоны.
    3) reconcile-vanish (полный crawl + vanish).
    4) rescore с allow_likely_deal=True.
    """
    settings = get_settings()
    ceiling = int(
        page_ceiling
        if page_ceiling is not None
        else settings.backfill_coverage_max_pages
    )
    srcs = _primary_sources(sources)
    settings.enrich_details = False
    report: dict[str, Any] = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "sources": srcs,
        "page_ceiling": ceiling,
        "fill": [],
        "refresh": [],
        "reconcile": [],
        "rescore": None,
        "before": None,
        "after": None,
        "ok": False,
        "errors": [],
    }

    before = snapshot_readiness(db, sources=srcs)
    report["before"] = before.to_dict()
    logger.info(
        "ensure-ready before %s/%s all_ready=%s",
        before.ready,
        before.total,
        before.all_ready,
    )

    # --- fill not-ready ---
    for src in srcs:
        snap = snapshot_readiness(db, sources=srcs)
        st = next((s for s in snap.sources if s.source == src), None)
        if st and st.vanish_ok and not (refresh_stale_ok and st.needs_refresh):
            continue
        if st and st.vanish_ok and st.needs_refresh:
            # обновим ниже в refresh
            continue
        result = _fill_source_until_ready(
            db,
            src,
            page_ceiling=ceiling,
            max_rounds=max_rounds_per_source,
            max_retries=max_retries,
        )
        report["fill"].append(result)
        if not result.get("ok"):
            report["errors"].append(
                {
                    "source": src,
                    "phase": "fill",
                    "stuck": result.get("stuck"),
                    "reason": result.get("reason"),
                }
            )

    # --- refresh stale "ok" sources so vanish is not on ghosts ---
    if refresh_stale_ok:
        snap = snapshot_readiness(db, sources=srcs)
        for st in snap.sources:
            if not st.needs_refresh:
                continue
            pages = min(ceiling, max(backfill_pages_for_source(st.source), 40))
            try:
                logger.info("ensure-ready refresh stale %s pages=%s", st.source, pages)
                summary = run_crawl(
                    db,
                    sources=[st.source],
                    max_pages=pages,
                    apply_vanish=False,
                    apply_vanish_after=False,
                    mode="full",
                )
                cache_clear()
                cov = coverage_for_source(db, st.source)
                report["refresh"].append(
                    {
                        "source": st.source,
                        "pages": pages,
                        "vanish_ok": cov.vanish_ok,
                        "fresh": cov.fresh_active,
                        "seen": cov.last_seen,
                        "reason": cov.vanish_reason,
                        "summary_keys": list((summary.get("sources") or {}).keys()),
                    }
                )
                if not cov.vanish_ok:
                    # ещё один fill-pass
                    result = _fill_source_until_ready(
                        db,
                        st.source,
                        page_ceiling=ceiling,
                        max_rounds=max_rounds_per_source,
                        max_retries=max_retries,
                    )
                    report["fill"].append(result)
                    if not result.get("ok"):
                        report["errors"].append(
                            {
                                "source": st.source,
                                "phase": "refresh_fill",
                                "stuck": result.get("stuck"),
                                "reason": result.get("reason"),
                            }
                        )
            except Exception as exc:  # noqa: BLE001
                logger.exception("refresh failed %s", st.source)
                report["errors"].append(
                    {"source": st.source, "phase": "refresh", "error": str(exc)}
                )

    mid = snapshot_readiness(db, sources=srcs)
    report["mid"] = mid.to_dict()
    if not mid.all_ready:
        # последняя попытка: fill всё, что ещё NO
        for st in mid.sources:
            if st.vanish_ok and not st.needs_refresh:
                continue
            result = _fill_source_until_ready(
                db,
                st.source,
                page_ceiling=ceiling,
                max_rounds=max(2, max_rounds_per_source // 2),
                max_retries=max_retries,
            )
            report["fill"].append(result)

    final_cov = snapshot_readiness(db, sources=srcs)
    report["coverage_final"] = final_cov.to_dict()

    not_ready = [
        s.source
        for s in final_cov.sources
        if not (s.vanish_ok and not s.needs_refresh)
    ]
    if not_ready and not skip_reconcile:
        logger.warning(
            "ensure-ready: не все источники готовы %s — vanish только по ready",
            not_ready,
        )

    reconcile_sources = [
        s.source for s in final_cov.sources if s.vanish_ok and not s.needs_refresh
    ]
    if not skip_reconcile and reconcile_sources:
        for src in reconcile_sources:
            pages = min(ceiling, max(backfill_pages_for_source(src), 25))
            try:
                logger.info("ensure-ready reconcile-vanish %s pages=%s", src, pages)
                summary = run_crawl(
                    db,
                    sources=[src],
                    max_pages=pages,
                    apply_vanish=False,
                    apply_vanish_after=True,
                    mode="full",
                )
                report["reconcile"].append(
                    {
                        "source": src,
                        "pages": pages,
                        "vanish": (summary.get("sources") or {}).get(src),
                        "vanish_reconcile": summary.get("vanish_reconcile"),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("reconcile failed %s", src)
                report["errors"].append(
                    {"source": src, "phase": "reconcile", "error": str(exc)}
                )

    if not skip_rescore:
        try:
            n = rescore_all_vanished(db, allow_likely_deal=True)
            report["rescore"] = {"rescored": n}
        except Exception as exc:  # noqa: BLE001
            logger.exception("rescore failed")
            report["errors"].append({"phase": "rescore", "error": str(exc)})

    after = snapshot_readiness(db, sources=srcs)
    # listings quick stats
    listing_status = {
        str(st): int(c)
        for st, c in db.execute(
            select(Listing.status, func.count()).group_by(Listing.status)
        ).all()
    }
    report["after"] = after.to_dict()
    report["listing_status"] = listing_status
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["ok"] = (
        after.ready == after.total
        and after.total > 0
        and all(not s.needs_refresh for s in after.sources)
        and not any(e.get("phase") == "rescore" for e in report["errors"])
    )
    report["ready_label"] = f"{after.ready}/{after.total}"
    return report
