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


def start_scheduler(
    cron: str | None = None,
    *,
    mode: str = "watch",
    full_cron: str | None = None,
    dual: bool = False,
) -> None:
    """Arm crawl jobs.

    dual=True: daily watch (no vanish) + weekly full (vanish if coverage OK).
    dual=False: single job for ``mode`` on ``cron``.
    """
    settings = get_settings()
    scheduler = BlockingScheduler()

    if dual:
        watch_expr = cron or settings.crawl_schedule_cron
        full_expr = full_cron or settings.full_crawl_schedule_cron
        scheduler.add_job(
            lambda: run_scheduled_crawl(mode="watch"),
            trigger=_parse_cron(watch_expr),
            id="daily_watch",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        scheduler.add_job(
            lambda: run_scheduled_crawl(mode="full"),
            trigger=_parse_cron(full_expr),
            id="weekly_full",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        logger.info(
            "Scheduler dual: watch=%r full=%r now=%s",
            watch_expr,
            full_expr,
            datetime.now(),
        )
    else:
        expr = cron or (
            settings.full_crawl_schedule_cron
            if mode == "full"
            else settings.crawl_schedule_cron
        )
        scheduler.add_job(
            lambda: run_scheduled_crawl(mode=mode),
            trigger=_parse_cron(expr),
            id="daily_crawl",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        logger.info(
            "Scheduler single cron=%r mode=%s now=%s",
            expr,
            mode,
            datetime.now(),
        )

    scheduler.start()
