from __future__ import annotations

import logging
from typing import Optional

import typer
from rich import print as rprint

from app.db import get_session_factory, init_db
from app.pipeline.demo import seed_demo_dataset
from app.pipeline.reconcile import rescore_all_vanished
from app.pipeline.runner import run_crawl
from app.scrapers import SCRAPERS

app = typer.Typer(help="EstateMonitor CLI")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


@app.command()
def init() -> None:
    """Create DB tables."""
    init_db()
    rprint("[green]DB initialized[/green]")


@app.command()
def demo() -> None:
    """Seed demo dataset for local UI/pipeline testing."""
    init_db()
    SessionLocal = get_session_factory()
    with SessionLocal() as db:
        result = seed_demo_dataset(db)
    rprint(result)


@app.command()
def crawl(
    source: Optional[str] = typer.Option(
        None, help=f"One of: {', '.join(SCRAPERS)} (default: all)"
    ),
    max_pages: int = typer.Option(2, min=1, max=50),
    no_vanish: bool = typer.Option(False, help="Do not mark missing listings vanished"),
    no_enrich: bool = typer.Option(False, help="Skip detail-page enrichment"),
    max_details: int = typer.Option(40, min=0, max=2000),
) -> None:
    """Crawl selected portals and ingest into DB."""
    from app.config import get_settings

    init_db()
    settings = get_settings()
    if no_enrich:
        settings.enrich_details = False
    settings.max_detail_pages = max_details
    sources = [source] if source else None
    if source and source not in SCRAPERS:
        raise typer.BadParameter(f"Unknown source. Choose from {list(SCRAPERS)}")
    SessionLocal = get_session_factory()
    with SessionLocal() as db:
        summary = run_crawl(
            db,
            sources=sources,
            max_pages=max_pages,
            apply_vanish=not no_vanish,
        )
    rprint(summary)


@app.command()
def backfill(
    source: Optional[str] = typer.Option(
        None, help=f"One of: {', '.join(SCRAPERS)} (default: lun,domria,rieltor)"
    ),
    max_pages: Optional[int] = typer.Option(None, help="Override backfill pages"),
    with_details: bool = typer.Option(
        False,
        help="Also enrich detail pages (slower). Default: list-only for max coverage+prices",
    ),
    max_details: Optional[int] = typer.Option(None),
) -> None:
    """Initial bulk fill: many list pages, prices from list cards/JSON-LD.

    Recommended first run (fast, max inventory+prices):
      python -m scripts.cli backfill

    Then deepen phones/status:
      python -m scripts.cli backfill --with-details
    """
    from app.config import get_settings

    init_db()
    settings = get_settings()
    pages = max_pages or settings.backfill_max_pages
    settings.enrich_details = with_details
    settings.max_detail_pages = max_details or settings.backfill_max_details

    if source:
        sources = [source]
    else:
        # OLX often blocked; fill from reliable portals first
        sources = ["lun", "domria", "rieltor"]

    rprint(
        {
            "mode": "backfill",
            "sources": sources,
            "max_pages": pages,
            "enrich_details": with_details,
            "max_details": settings.max_detail_pages,
        }
    )
    SessionLocal = get_session_factory()
    with SessionLocal() as db:
        # First fill: do not vanish — inventory is still incomplete
        summary = run_crawl(
            db,
            sources=sources,
            max_pages=pages,
            apply_vanish=False,
        )
    rprint(summary)


@app.command()
def scheduler(
    cron: Optional[str] = typer.Option(
        None, help="5-field cron, default from CRAWL_SCHEDULE_CRON (0 7 * * *)"
    ),
    run_now: bool = typer.Option(False, help="Run one crawl immediately, then schedule"),
) -> None:
    """Run blocking daily crawler scheduler (keep process alive)."""
    from app.config import get_settings
    from app.pipeline.scheduler import run_scheduled_crawl, start_scheduler

    settings = get_settings()
    expr = cron or settings.crawl_schedule_cron
    rprint(f"Daily crawl cron={expr!r} pages={settings.scheduler_max_pages}")
    if run_now:
        rprint(run_scheduled_crawl())
    start_scheduler(expr)


@app.command()
def rescore() -> None:
    """Recompute deal hypotheses for vanished listings."""
    init_db()
    SessionLocal = get_session_factory()
    with SessionLocal() as db:
        n = rescore_all_vanished(db)
    rprint({"rescored": n})


@app.command("clean-junk")
def clean_junk() -> None:
    """Remove non-Kyiv URLs and nonsensical prices (inf / absurd)."""
    import math

    from sqlalchemy import select

    from app.db.models import DealHypothesis, Listing, ListingSnapshot, Property, PropertyEvent
    from app.scrapers.http_utils import is_kyiv_region_url

    init_db()
    SessionLocal = get_session_factory()
    removed = 0
    fixed_price = 0
    with SessionLocal() as db:
        for lst in list(db.scalars(select(Listing))):
            drop = False
            if lst.source == "domria" and lst.url and not is_kyiv_region_url(lst.url):
                drop = True
            price = lst.price
            try:
                bad_price = price is not None and (
                    not math.isfinite(float(price)) or float(price) <= 0 or float(price) > 500_000_000
                )
            except (TypeError, ValueError):
                bad_price = True
            if drop:
                for snap in list(lst.snapshots):
                    db.delete(snap)
                db.execute(
                    DealHypothesis.__table__.delete().where(DealHypothesis.listing_id == lst.id)
                )
                db.execute(
                    PropertyEvent.__table__.delete().where(PropertyEvent.listing_id == lst.id)
                )
                db.delete(lst)
                removed += 1
            elif bad_price:
                lst.price = None
                fixed_price += 1
        db.commit()
        orphans = 0
        for prop in list(db.scalars(select(Property))):
            if not prop.listings:
                db.execute(
                    PropertyEvent.__table__.delete().where(PropertyEvent.property_id == prop.id)
                )
                db.execute(
                    DealHypothesis.__table__.delete().where(DealHypothesis.property_id == prop.id)
                )
                db.delete(prop)
                orphans += 1
        db.commit()
    rprint({"removed_non_kyiv": removed, "nulled_bad_prices": fixed_price, "orphan_props": orphans})


@app.command("backfill-opex")
def backfill_opex() -> None:
    """Parse OPEX markers from rent listing text into raw_extra.opex."""
    from sqlalchemy import select

    from app.db.models import Listing
    from app.domain.signals import detect_opex

    init_db()
    SessionLocal = get_session_factory()
    updated = 0
    counts = {"with": 0, "without": 0, "unknown": 0}
    with SessionLocal() as db:
        for lst in db.scalars(select(Listing).where(Listing.deal_type == "rent")):
            flag = detect_opex(lst.title, lst.description)
            extra = dict(lst.raw_extra or {})
            if extra.get("opex") != flag:
                extra["opex"] = flag
                lst.raw_extra = extra
                updated += 1
            counts[flag] = counts.get(flag, 0) + 1
        db.commit()
    rprint({"updated": updated, "counts": counts})


@app.command("reclassify")
def reclassify() -> None:
    """Recompute segment tags for all listings (fixes noisy list-card mis-tags)."""
    from sqlalchemy import select

    from app.db.models import Listing
    from app.domain.segments import classify_segment

    init_db()
    SessionLocal = get_session_factory()
    changed = 0
    removed = 0
    with SessionLocal() as db:
        for lst in db.scalars(select(Listing)):
            decision = classify_segment(
                title=lst.title,
                description=lst.description,
                property_type=lst.property_type,
                floor=lst.floor,
                address=lst.address_raw,
                url=lst.url,
            )
            if not decision.relevant:
                db.delete(lst)
                removed += 1
                continue
            if lst.property_type != decision.segment:
                lst.property_type = decision.segment
                changed += 1
        db.commit()
    rprint({"retagged": changed, "removed_irrelevant": removed})


@app.command()
def prune_irrelevant() -> None:
    """Remove warehouse/industrial/land listings that do not match target segments."""
    from sqlalchemy import select

    from app.db.models import DealHypothesis, Listing, ListingSnapshot, Property, PropertyEvent
    from app.domain.segments import classify_segment

    init_db()
    SessionLocal = get_session_factory()
    removed = 0
    with SessionLocal() as db:
        listings = list(db.scalars(select(Listing)))
        for lst in listings:
            decision = classify_segment(
                title=lst.title,
                description=lst.description,
                property_type=lst.property_type,
                floor=lst.floor,
                address=lst.address_raw,
                url=lst.url,
            )
            if decision.relevant:
                if decision.segment != lst.property_type and decision.segment in {
                    "office",
                    "retail",
                    "showroom",
                    "business_center",
                    "street_retail",
                    "building",
                    "free_purpose",
                }:
                    lst.property_type = decision.segment
                continue
            # delete dependent rows
            for snap in list(lst.snapshots):
                db.delete(snap)
            db.execute(
                DealHypothesis.__table__.delete().where(
                    DealHypothesis.listing_id == lst.id
                )
            )
            db.execute(
                PropertyEvent.__table__.delete().where(PropertyEvent.listing_id == lst.id)
            )
            db.delete(lst)
            removed += 1
        db.commit()

        # drop orphan properties
        orphans = 0
        for prop in list(db.scalars(select(Property))):
            if not prop.listings:
                db.execute(
                    PropertyEvent.__table__.delete().where(
                        PropertyEvent.property_id == prop.id
                    )
                )
                db.execute(
                    DealHypothesis.__table__.delete().where(
                        DealHypothesis.property_id == prop.id
                    )
                )
                db.delete(prop)
                orphans += 1
        db.commit()
    rprint({"removed_listings": removed, "removed_orphan_properties": orphans})


@app.command()
def stats() -> None:
    """Show DB fill stats."""
    from sqlalchemy import func, select

    from app.db.models import DealHypothesis, Listing, Property

    init_db()
    SessionLocal = get_session_factory()
    with SessionLocal() as db:
        data = {
            "properties": db.scalar(select(func.count()).select_from(Property)) or 0,
            "listings": db.scalar(select(func.count()).select_from(Listing)) or 0,
            "with_price": db.scalar(
                select(func.count()).select_from(Listing).where(Listing.price.is_not(None))
            )
            or 0,
            "active": db.scalar(
                select(func.count()).select_from(Listing).where(Listing.status == "active")
            )
            or 0,
            "vanished": db.scalar(
                select(func.count()).select_from(Listing).where(Listing.status == "vanished")
            )
            or 0,
            "deal_hypotheses": db.scalar(select(func.count()).select_from(DealHypothesis))
            or 0,
            "by_source": dict(
                db.execute(
                    select(Listing.source, func.count()).group_by(Listing.source)
                ).all()
            ),
            "by_segment": dict(
                db.execute(
                    select(Listing.property_type, func.count())
                    .group_by(Listing.property_type)
                    .order_by(func.count().desc())
                ).all()
            ),
        }
    rprint(data)


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    """Run web UI + API."""
    import uvicorn

    uvicorn.run("app.main:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    app()
