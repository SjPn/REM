from __future__ import annotations

import logging
from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger

from app.config import get_settings
from app.db import get_session_factory, init_db
from app.pipeline.runner import run_crawl

logger = logging.getLogger(__name__)


def _parse_cron(expr: str) -> CronTrigger:
    """Parse classic 5-field cron: min hour day month dow."""
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError(f"Expected 5-field cron, got: {expr!r}")
    minute, hour, day, month, day_of_week = parts
    return CronTrigger(
        minute=minute,
        hour=hour,
        day=day,
        month=month,
        day_of_week=day_of_week,
    )


def run_scheduled_crawl(*, mode: str = "watch") -> dict:
    settings = get_settings()
    settings.enrich_details = True
    if mode == "watch":
        settings.max_detail_pages = settings.watch_max_details
        pages = settings.watch_max_pages
        details = settings.watch_max_details
    else:
        settings.max_detail_pages = settings.scheduler_max_details
        pages = settings.scheduler_max_pages
        details = settings.scheduler_max_details
    init_db()
    SessionLocal = get_session_factory()
    logger.info(
        "Scheduled crawl mode=%s pages=%s details=%s",
        mode,
        pages,
        details,
    )
    with SessionLocal() as db:
        summary = run_crawl(
            db,
            max_pages=pages,
            max_details=details,
            mode=mode,
        )
    logger.info("Scheduled crawl finished: %s", summary)
    return summary


def start_scheduler(cron: str | None = None, *, mode: str = "watch") -> None:
    settings = get_settings()
    expr = cron or settings.crawl_schedule_cron
    trigger = _parse_cron(expr)
    scheduler = BlockingScheduler()
    scheduler.add_job(
        lambda: run_scheduled_crawl(mode=mode),
        trigger=trigger,
        id="daily_crawl",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    logger.info(
        "Scheduler armed cron=%r mode=%s next roughly daily; now=%s",
        expr,
        mode,
        datetime.now(),
    )
    scheduler.start()
