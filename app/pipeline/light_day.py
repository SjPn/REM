"""Ежедневный лёгкий сбор: верх выдачи, новые и уценки, без vanish."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Listing
from app.domain.list_card import needs_detail_fetch
from app.domain.market_history import record_market_snapshot
from app.pipeline.ingest import ingest_many
from app.scrapers import SCRAPERS, crawl_source
from app.scrapers.base import RawListing
from app.scrapers.http_utils import HttpClient

logger = logging.getLogger(__name__)


def _light_needs_detail(db: Session):
    """Короткое чтение карточки. Транзакцию не держим на время HTTP."""
    db.expire_on_commit = False
    cache: dict[tuple[str, str], Listing | None] = {}

    def needs_detail(raw: RawListing) -> bool:
        key = (raw.source, raw.external_id)
        if key not in cache:
            cache[key] = db.scalar(
                select(Listing).where(
                    Listing.source == raw.source,
                    Listing.external_id == raw.external_id,
                )
            )
            db.rollback()
        return needs_detail_fetch(cache[key], raw)

    return needs_detail


def _fetch_one(
    source: str,
    pages: int,
    stale_page_limit: int,
    max_details: int,
) -> tuple[str, list[RawListing], str | None]:
    from app.db.models import get_session_factory

    try:
        with get_session_factory()() as db:
            needs = _light_needs_detail(db)
            with HttpClient(list_fast=True) as client:
                items = list(
                    crawl_source(
                        source,
                        max_pages=pages,
                        client=client,
                        needs_detail=needs,
                        stale_page_limit=stale_page_limit,
                        max_details=max_details,
                    )
                )
        return source, items, None
    except Exception as exc:  # noqa: BLE001
        logger.exception("light-day fetch failed %s", source)
        return source, [], str(exc)


def run_light_day(
    db: Session,
    *,
    sources: list[str] | None = None,
    max_pages: int | None = None,
    max_details: int | None = None,
    stale_page_limit: int | None = None,
) -> dict:
    """Пять площадок параллельно, первые страницы, детали только у новых и изменённых."""
    settings = get_settings()
    pages = int(max_pages if max_pages is not None else settings.light_max_pages)
    details = int(max_details if max_details is not None else settings.light_max_details)
    stale = int(
        stale_page_limit if stale_page_limit is not None else settings.light_stale_pages
    )
    selected = list(sources or SCRAPERS.keys())
    unknown = [s for s in selected if s not in SCRAPERS]
    if unknown:
        raise KeyError(f"Unknown source: {unknown}. Available: {list(SCRAPERS)}")

    started_at = datetime.now(timezone.utc).isoformat()
    prev_enrich = settings.enrich_details
    prev_max = settings.max_detail_pages
    settings.enrich_details = True
    settings.max_detail_pages = details
    logger.info(
        "light-day: sources=%s pages=%s details=%s stale=%s",
        ",".join(selected),
        pages,
        details,
        stale,
    )
    fetched: list[tuple[str, list[RawListing], str | None]] = []
    try:
        with ThreadPoolExecutor(max_workers=max(1, len(selected))) as pool:
            futures = {
                pool.submit(_fetch_one, src, pages, stale, details): src for src in selected
            }
            for fut in as_completed(futures):
                src = futures[fut]
                try:
                    fetched.append(fut.result())
                except Exception as exc:  # noqa: BLE001
                    logger.exception("light-day worker failed %s", src)
                    fetched.append((src, [], str(exc)))
    finally:
        settings.enrich_details = prev_enrich
        settings.max_detail_pages = prev_max

    by_source = {src: (items, err) for src, items, err in fetched}
    summary: dict = {
        "mode": "light",
        "vanish": False,
        "parallel": True,
        "max_pages": pages,
        "max_details": details,
        "stale_page_limit": stale,
        "started_at": started_at,
        "sources": {},
    }
    for source in selected:
        items, err = by_source.get(source, ([], "missing"))
        if err:
            summary["sources"][source] = {"error": err}
            continue
        stats = ingest_many(db, items)
        ids = stats.pop("upserted_external_ids", set()) or set()
        summary["sources"][source] = {
            "cards": len(items),
            "upserted": int(stats.get("upserted", 0)),
            "seen": len(ids),
            "skipped_irrelevant": int(stats.get("skipped_irrelevant", 0)),
            "snapshots_skipped": int(stats.get("snapshots_skipped", 0)),
        }
    try:
        snap = record_market_snapshot(db, force=True)
        summary["market_snapshot_day"] = snap.day
    except Exception:  # noqa: BLE001
        logger.exception("light-day market snapshot failed")
        summary["market_snapshot_day"] = None
    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    summary["ok"] = bool(summary["sources"]) and any(
        "error" not in info for info in summary["sources"].values()
    )
    return summary
