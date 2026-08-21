from __future__ import annotations

import logging
import random
import time

from sqlalchemy.orm import Session

from app.db.models import CrawlRun, utcnow
from app.config import get_settings
from app.pipeline.ingest import ingest_many
from app.pipeline.reconcile import mark_vanished, rescore_all_vanished
from app.scrapers import SCRAPERS, crawl_source

logger = logging.getLogger(__name__)


def run_crawl(
    db: Session,
    sources: list[str] | None = None,
    max_pages: int | None = None,
    apply_vanish: bool = True,
) -> dict:
    settings = get_settings()
    selected = list(sources or SCRAPERS.keys())
    if settings.crawl_human_mode and len(selected) > 1:
        random.shuffle(selected)
        logger.info("crawl source order: %s", ", ".join(selected))
    summary: dict = {"sources": {}, "started_at": utcnow().isoformat()}

    for idx, source in enumerate(selected):
        if idx > 0 and settings.crawl_human_mode:
            pause = random.uniform(12.0, 35.0)
            logger.info("pause %.0fs before next source (%s)", pause, source)
            time.sleep(pause)

        run = CrawlRun(source=source, started_at=utcnow(), status="running")
        db.add(run)
        db.commit()

        seen_ids: set[str] = set()
        items = []
        try:
            for raw in crawl_source(source, max_pages=max_pages):
                items.append(raw)
                seen_ids.add(raw.external_id)
            stats = ingest_many(db, items)
            vanished = 0
            vanish_skipped = False
            if apply_vanish and seen_ids:
                if len(seen_ids) >= settings.min_seen_for_vanish:
                    vanished = mark_vanished(db, source, seen_ids)
                else:
                    vanish_skipped = True
                    logger.warning(
                        "%s: skip vanish (seen=%s < min=%s)",
                        source,
                        len(seen_ids),
                        settings.min_seen_for_vanish,
                    )
            run.status = "ok"
            run.listings_seen = len(seen_ids)
            run.pages_fetched = max_pages or 0
            summary["sources"][source] = {
                "upserted": stats["upserted"],
                "seen": len(seen_ids),
                "vanished": vanished,
                "vanish_skipped": vanish_skipped,
                "with_price": sum(1 for x in items if x.price is not None),
                "skipped_irrelevant": stats.get("skipped_irrelevant", 0),
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception("Crawl failed for %s", source)
            run.status = "error"
            run.error = str(exc)
            summary["sources"][source] = {"error": str(exc)}
        finally:
            run.finished_at = utcnow()
            db.commit()

    rescored = rescore_all_vanished(db)
    summary["rescored_hypotheses"] = rescored
    summary["finished_at"] = utcnow().isoformat()
    return summary
