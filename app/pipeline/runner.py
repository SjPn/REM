from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import CrawlRun, Listing, utcnow
from app.config import get_settings
from app.domain.list_card import needs_detail_fetch
from app.domain.market_history import record_market_snapshot
from app.domain.ttl_cache import cache_clear
from app.pipeline.ingest import ingest_many
from app.pipeline.reconcile import mark_vanished, rescore_all_vanished
from app.pipeline.vanish_guard import apply_vanish_reconcile, vanish_allowed
from app.scrapers import SCRAPERS, crawl_source
from app.scrapers.base import RawListing
from app.scrapers.http_utils import HttpClient

logger = logging.getLogger(__name__)

CrawlMode = str  # "watch" | "full"


def _build_needs_detail(db: Session) -> Callable[[RawListing], bool]:
    cache: dict[tuple[str, str], Listing | None] = {}

    def _lookup(raw: RawListing) -> Listing | None:
        key = (raw.source, raw.external_id)
        if key not in cache:
            cache[key] = db.scalar(
                select(Listing).where(
                    Listing.source == raw.source,
                    Listing.external_id == raw.external_id,
                )
            )
        return cache[key]

    def needs_detail(raw: RawListing) -> bool:
        return needs_detail_fetch(_lookup(raw), raw)

    return needs_detail


def run_crawl(
    db: Session,
    sources: list[str] | None = None,
    max_pages: int | None = None,
    apply_vanish: bool | None = None,
    apply_vanish_after: bool = False,
    mode: CrawlMode = "full",
    max_details: int | None = None,
) -> dict:
    settings = get_settings()
    if mode == "watch":
        pages = max_pages if max_pages is not None else settings.watch_max_pages
        vanish = (
            settings.watch_apply_vanish if apply_vanish is None else apply_vanish
        )
        details_cap = (
            max_details if max_details is not None else settings.watch_max_details
        )
        needs_detail = _build_needs_detail(db)
        logger.info(
            "watch crawl: pages=%s details=%s vanish=%s",
            pages,
            details_cap,
            vanish,
        )
    else:
        pages = max_pages if max_pages is not None else settings.scheduler_max_pages
        vanish = True if apply_vanish is None else apply_vanish
        details_cap = max_details
        needs_detail = None
        logger.info("full crawl: pages=%s vanish=%s", pages, vanish)

    selected = list(sources or SCRAPERS.keys())
    if mode == "watch":
        # OLX often blocked; watch focuses on reliable portals.
        selected = [s for s in selected if s in {"lun", "domria", "rieltor"}] or selected
    if settings.crawl_human_mode and len(selected) > 1:
        random.shuffle(selected)
        logger.info("crawl source order: %s", ", ".join(selected))

    prev_enrich = settings.enrich_details
    prev_max_details = settings.max_detail_pages
    if mode == "watch":
        settings.enrich_details = True
        settings.max_detail_pages = details_cap

    summary: dict = {
        "mode": mode,
        "sources": {},
        "sources_seen": {},
        "started_at": utcnow().isoformat(),
    }
    sources_seen: dict[str, set[str]] = {}

    try:
        with HttpClient() as client:
            for idx, source in enumerate(selected):
                if idx > 0 and settings.crawl_human_mode:
                    pause = random.uniform(18.0, 48.0) if mode == "full" else random.uniform(8.0, 20.0)
                    logger.info("pause %.0fs before next source (%s)", pause, source)
                    time.sleep(pause)

                run = CrawlRun(source=source, started_at=utcnow(), status="running")
                db.add(run)
                db.commit()

                seen_ids: set[str] = set()
                items = []
                try:
                    for raw in crawl_source(
                        source,
                        max_pages=pages,
                        client=client,
                        needs_detail=needs_detail,
                    ):
                        items.append(raw)
                        seen_ids.add(raw.external_id)
                    stats = ingest_many(db, items)
                    sources_seen[source] = seen_ids
                    vanished = 0
                    vanish_skipped = False
                    vanish_reason = ""
                    do_vanish = vanish and seen_ids and not apply_vanish_after
                    if do_vanish:
                        ok, vanish_reason = vanish_allowed(db, source, len(seen_ids))
                        if ok:
                            vanished = mark_vanished(db, source, seen_ids)
                            logger.info(
                                "%s: vanished %s (%s)", source, vanished, vanish_reason
                            )
                        else:
                            vanish_skipped = True
                            logger.warning("%s: skip vanish — %s", source, vanish_reason)
                    run.status = "ok"
                    run.listings_seen = len(seen_ids)
                    run.pages_fetched = pages
                    summary["sources"][source] = {
                        "upserted": stats["upserted"],
                        "seen": len(seen_ids),
                        "vanished": vanished,
                        "vanish_skipped": vanish_skipped,
                        "vanish_reason": vanish_reason or None,
                        "with_price": sum(1 for x in items if x.price is not None),
                        "skipped_irrelevant": stats.get("skipped_irrelevant", 0),
                        "snapshots_skipped": stats.get("snapshots_skipped", 0),
                    }
                except Exception as exc:  # noqa: BLE001
                    logger.exception("Crawl failed for %s", source)
                    run.status = "error"
                    run.error = str(exc)
                    summary["sources"][source] = {"error": str(exc)}
                finally:
                    run.finished_at = utcnow()
                    db.commit()
    finally:
        settings.enrich_details = prev_enrich
        settings.max_detail_pages = prev_max_details

    summary["sources_seen"] = {k: len(v) for k, v in sources_seen.items()}
    if apply_vanish_after and sources_seen:
        summary["vanish_reconcile"] = apply_vanish_reconcile(db, sources_seen)

    cache_clear()
    rescored = rescore_all_vanished(db)
    try:
        snap = record_market_snapshot(db, force=True)
        summary["market_snapshot_day"] = snap.day
    except Exception:  # noqa: BLE001
        logger.exception("market snapshot failed")
        summary["market_snapshot_day"] = None
    summary["rescored_hypotheses"] = rescored
    summary["finished_at"] = utcnow().isoformat()
    return summary
