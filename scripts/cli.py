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

app = typer.Typer(help="RealEstateMonitor (REM) CLI")
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
    max_pages: int = typer.Option(8, min=1, max=50),
    no_vanish: bool = typer.Option(False, help="Do not mark missing listings vanished"),
    no_enrich: bool = typer.Option(False, help="Skip detail-page enrichment"),
    max_details: int = typer.Option(80, min=0, max=2000),
) -> None:
    """Full crawl: several list pages + vanish reconcile (weekly / manual deep scan)."""
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
            mode="full",
            max_details=max_details,
        )
    rprint(summary)


@app.command()
def watch(
    source: Optional[str] = typer.Option(
        None, help=f"One of: lun, domria, rieltor (default: all three)"
    ),
    max_pages: Optional[int] = typer.Option(
        None, min=1, max=5, help="List pages per feed (default from WATCH_MAX_PAGES)"
    ),
    max_details: Optional[int] = typer.Option(
        None, min=0, max=200, help="Detail fetches for new/changed only"
    ),
) -> None:
    """Lightweight watch crawl: first pages only, enrich new/changed cards, no vanish."""
    from app.config import get_settings

    init_db()
    settings = get_settings()
    sources = [source] if source else ["lun", "domria", "rieltor"]
    if source and source not in SCRAPERS:
        raise typer.BadParameter(f"Unknown source. Choose from {list(SCRAPERS)}")
    pages = max_pages or settings.watch_max_pages
    details = max_details if max_details is not None else settings.watch_max_details
    rprint(
        {
            "mode": "watch",
            "sources": sources,
            "max_pages": pages,
            "max_details": details,
            "vanish": False,
        }
    )
    SessionLocal = get_session_factory()
    with SessionLocal() as db:
        summary = run_crawl(
            db,
            sources=sources,
            max_pages=pages,
            max_details=details,
            mode="watch",
        )
    rprint(summary)


@app.command()
def backfill(
    source: Optional[str] = typer.Option(
        None, help=f"One of: {', '.join(SCRAPERS)}"
    ),
    sources: Optional[str] = typer.Option(
        None,
        help="Comma-separated sources (default: lun,domria)",
    ),
    max_pages: Optional[int] = typer.Option(None, help="Override backfill pages"),
    with_details: bool = typer.Option(
        False,
        help="Also enrich detail pages (slower). Default: list-only for max coverage+prices",
    ),
    max_details: Optional[int] = typer.Option(None),
    reconcile_vanish: bool = typer.Option(
        True,
        "--reconcile-vanish/--no-reconcile-vanish",
        help="После backfill — vanish только если crawl увидел ≥55% активных",
    ),
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

    if sources:
        src_list = [s.strip() for s in sources.split(",") if s.strip()]
    elif source:
        src_list = [source]
    else:
        src_list = ["lun", "domria"]

    for s in src_list:
        if s not in SCRAPERS:
            raise typer.BadParameter(f"Unknown source {s!r}. Choose from {list(SCRAPERS)}")

    rprint(
        {
            "mode": "backfill",
            "sources": src_list,
            "max_pages": pages,
            "enrich_details": with_details,
            "max_details": settings.max_detail_pages,
            "reconcile_vanish": reconcile_vanish,
        }
    )
    SessionLocal = get_session_factory()
    with SessionLocal() as db:
        summary = run_crawl(
            db,
            sources=src_list,
            max_pages=pages,
            apply_vanish=False,
            apply_vanish_after=reconcile_vanish,
            mode="full",
        )
    rprint(summary)


@app.command("reconcile-vanish")
def reconcile_vanish_cmd(
    source: Optional[str] = typer.Option(None, help="One source"),
    sources: Optional[str] = typer.Option(None, help="Comma list, default lun,domria"),
    max_pages: Optional[int] = typer.Option(
        None, help="List pages (default BACKFILL_MAX_PAGES)"
    ),
) -> None:
    """Полный list-crawl + vanish только при достаточном coverage."""
    from app.config import get_settings

    init_db()
    settings = get_settings()
    pages = max_pages or settings.backfill_max_pages
    if sources:
        src_list = [s.strip() for s in sources.split(",") if s.strip()]
    elif source:
        src_list = [source]
    else:
        src_list = ["lun", "domria"]
    settings.enrich_details = False
    SessionLocal = get_session_factory()
    with SessionLocal() as db:
        summary = run_crawl(
            db,
            sources=src_list,
            max_pages=pages,
            apply_vanish=False,
            apply_vanish_after=True,
            mode="full",
        )
    rprint(summary)


@app.command()
def scheduler(
    cron: Optional[str] = typer.Option(
        None, help="5-field cron, default from CRAWL_SCHEDULE_CRON (0 7 * * *)"
    ),
    run_now: bool = typer.Option(False, help="Run one crawl immediately, then schedule"),
    full: bool = typer.Option(
        False, help="Use full crawl (many pages + vanish) instead of lightweight watch"
    ),
) -> None:
    """Run blocking daily crawler scheduler (keep process alive)."""
    from app.config import get_settings
    from app.pipeline.scheduler import run_scheduled_crawl, start_scheduler

    settings = get_settings()
    expr = cron or settings.crawl_schedule_cron
    mode = "full" if full else "watch"
    rprint(f"Daily crawl cron={expr!r} mode={mode}")
    if run_now:
        rprint(run_scheduled_crawl(mode=mode))
    start_scheduler(expr, mode=mode)


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


@app.command("fix-prices")
def fix_prices(
    status: str = typer.Option("all", help="all | active | active,relisted"),
) -> None:
    """Repair listings where stored price is actually $/m² (or was wrongly multiplied)."""
    stats = _run_fix_prices(status_filter=status)
    rprint(stats)


def _price_audit_counts(db) -> dict[str, int | float]:
    """Snapshot of common price-quality counters."""
    from sqlalchemy import func, select

    from app.db.models import Listing
    from app.domain.pricing import effective_listing_psm_usd

    active = (
        db.scalar(
            select(func.count())
            .select_from(Listing)
            .where(Listing.status.in_(["active", "relisted"]))
        )
        or 0
    )
    sale_bad = 0
    rent_bad = 0
    for lst in db.scalars(
        select(Listing).where(
            Listing.status.in_(["active", "relisted"]),
            Listing.price.is_not(None),
            Listing.area_sqm.is_not(None),
        )
    ):
        psm = effective_listing_psm_usd(
            lst.price,
            lst.currency,
            lst.area_sqm,
            deal_type=lst.deal_type,
            price_per_sqm=lst.price_per_sqm,
        )
        if not psm:
            continue
        if lst.deal_type == "sale" and (psm < 450 or psm > 10_000):
            sale_bad += 1
        elif lst.deal_type == "rent" and psm > 70:
            rent_bad += 1
    rieltor_junk = (
        db.scalar(
            select(func.count())
            .select_from(Listing)
            .where(
                Listing.source == "rieltor",
                Listing.status.in_(["active", "relisted"]),
                Listing.title.op("GLOB")("[0-9][0-9] [0-9][0-9] *"),
            )
        )
        or 0
    )
    return {
        "active_listings": active,
        "sale_psm_out_of_band": sale_bad,
        "rent_psm_over_70": rent_bad,
        "rieltor_junk_titles": rieltor_junk,
    }


def _run_fix_prices(*, status_filter: str = "all") -> dict[str, int]:
    from sqlalchemy import select

    from app.db.models import Listing
    from app.domain.market_stats import to_usd
    from app.domain.listing_stats import apply_auto_stats_exclusion
    from app.domain.pricing import normalize_listing_price, psm_suspicious, sanitize_price_per_sqm
    from app.domain.signals import listing_psm_usd
    from app.scrapers.http_utils import strip_leading_price_junk

    init_db()
    SessionLocal = get_session_factory()
    touched = 0
    titles_cleaned = 0
    repaired_text = 0
    repaired_psm = 0

    statuses: list[str] | None = None
    if status_filter.strip().lower() != "all":
        statuses = [s.strip() for s in status_filter.split(",") if s.strip()]

    with SessionLocal() as db:
        query = select(Listing)
        if statuses:
            query = query.where(Listing.status.in_(statuses))
        for lst in db.scalars(query):
            title_for_parse = lst.title
            norm = normalize_listing_price(
                price=lst.price,
                currency=lst.currency,
                area_sqm=lst.area_sqm,
                deal_type=lst.deal_type,
                price_per_sqm=lst.price_per_sqm,
                title=title_for_parse,
                description=lst.description,
            )
            extra = dict(lst.raw_extra or {})
            before = (lst.price, lst.currency, lst.price_per_sqm)
            title_before = lst.title

            if norm.price is not None:
                lst.price = norm.price
            if norm.currency:
                lst.currency = norm.currency
            fixed_psm = sanitize_price_per_sqm(
                price=lst.price,
                currency=lst.currency,
                area_sqm=lst.area_sqm,
                deal_type=lst.deal_type,
                price_per_sqm=norm.price_per_sqm,
            )
            if fixed_psm is not None:
                lst.price_per_sqm = fixed_psm
            elif norm.price_per_sqm is not None:
                lst.price_per_sqm = norm.price_per_sqm

            after = (lst.price, lst.currency, lst.price_per_sqm)
            price_changed = after != before

            if norm.detail == "text_total_and_psm":
                extra.pop("price_was_psm", None)
                extra["price_norm"] = norm.detail
                if price_changed:
                    repaired_text += 1
            elif norm.detail == "repair_absurd_from_text_psm":
                extra.pop("price_was_psm", None)
                extra["price_norm"] = norm.detail
                if price_changed:
                    repaired_psm += 1
            elif norm.reinterpreted_as_psm:
                extra["price_was_psm"] = True
                extra["price_norm"] = norm.detail
            elif extra.get("price_was_psm") and (lst.deal_type or "") == "rent":
                psm = listing_psm_usd(
                    lst.price,
                    lst.currency,
                    lst.area_sqm,
                    deal_type=lst.deal_type,
                    price_per_sqm=lst.price_per_sqm,
                )
                if psm is not None and psm > 70:
                    rate = lst.price_per_sqm
                    rate_usd = to_usd(float(rate), lst.currency) if rate is not None else None
                    if rate is not None and rate_usd is not None and rate_usd <= 70 and lst.area_sqm:
                        lst.price = round(float(rate) * float(lst.area_sqm), 2)
                        extra["price_norm"] = "rollback_reexpand_sane_rate"
                    else:
                        extra.pop("price_was_psm", None)
                        extra["price_norm"] = "clear_bad_expand"
                        extra["price_suspicious"] = True

            psm_now = listing_psm_usd(
                lst.price,
                lst.currency,
                lst.area_sqm,
                deal_type=lst.deal_type,
                price_per_sqm=lst.price_per_sqm,
            )
            if psm_suspicious(lst.deal_type, psm_now) or norm.suspicious_psm:
                extra["price_suspicious"] = True
            else:
                extra.pop("price_suspicious", None)
            apply_auto_stats_exclusion(
                lst, suspicious=bool(extra.get("price_suspicious"))
            )

            if lst.source == "rieltor" and lst.title:
                cleaned = strip_leading_price_junk(lst.title)
                if cleaned and cleaned != lst.title:
                    lst.title = cleaned
                    titles_cleaned += 1

            extra_before = dict(lst.raw_extra or {})
            if (
                after != before
                or extra != extra_before
                or lst.title != title_before
            ):
                lst.raw_extra = extra or None
                touched += 1
        db.commit()
    return {
        "touched": touched,
        "repaired_from_text": repaired_text,
        "repaired_from_text_psm": repaired_psm,
        "titles_cleaned": titles_cleaned,
    }


@app.command("review-prices")
def review_prices(
    status: str = typer.Option("active,relisted", help="all | active | active,relisted"),
) -> None:
    """Full price review: audit → fix all matching listings → audit again."""
    init_db()
    SessionLocal = get_session_factory()
    with SessionLocal() as db:
        before = _price_audit_counts(db)
    rprint({"before": before})
    stats = _run_fix_prices(status_filter=status)
    rprint({"fix": stats})
    with SessionLocal() as db:
        after = _price_audit_counts(db)
    rprint({"after": after})


@app.command("refresh-prices")
def refresh_prices(
    source: str = typer.Option("rieltor", help="Источник: rieltor, lun, domria"),
    limit: int = typer.Option(80, help="Макс. карточек за прогон"),
    min_sale_psm: float = typer.Option(10_000, help="Продажа: перезагрузить если $/м² выше"),
) -> None:
    """Перезагрузить детальные страницы объявлений с подозрительной ценой."""
    from sqlalchemy import select

    from app.db.models import Listing
    from app.domain.pricing import effective_listing_psm_usd
    from app.pipeline.ingest import upsert_listing
    from app.scrapers.base import RawListing
    from app.scrapers.rieltor import RieltorScraper

    scrapers = {"rieltor": RieltorScraper}
    if source not in scrapers:
        rprint(f"[red]Источник {source} пока не поддержан[/red]")
        raise typer.Exit(code=1)

    init_db()
    SessionLocal = get_session_factory()
    refreshed = 0
    scraper = scrapers[source]()
    try:
        with SessionLocal() as db:
            for lst in db.scalars(
                select(Listing).where(
                    Listing.source == source,
                    Listing.status.in_(["active", "relisted"]),
                    Listing.url.is_not(None),
                )
            ):
                if refreshed >= limit:
                    break
                if not lst.price or not lst.area_sqm:
                    continue
                psm = effective_listing_psm_usd(
                    lst.price,
                    lst.currency,
                    lst.area_sqm,
                    deal_type=lst.deal_type,
                    price_per_sqm=lst.price_per_sqm,
                )
                if lst.deal_type == "sale" and (not psm or psm < min_sale_psm):
                    continue
                if lst.deal_type == "rent" and (not psm or psm <= 70):
                    continue
                raw_stub = RawListing(
                    source=lst.source,
                    external_id=lst.external_id,
                    url=lst.url,
                    deal_type=lst.deal_type or "sale",
                    title=lst.title,
                    description=lst.description,
                    property_type=lst.property_type,
                    price=lst.price,
                    currency=lst.currency,
                    price_per_sqm=lst.price_per_sqm,
                    area_sqm=lst.area_sqm,
                    floor=lst.floor,
                    address_raw=lst.address_raw,
                    district=lst.district,
                    city=lst.city,
                )
                try:
                    detail = scraper.fetch_detail(raw_stub)
                except Exception as exc:  # noqa: BLE001
                    rprint(f"[yellow]skip {lst.external_id}: {exc}[/yellow]")
                    continue
                upsert_listing(db, detail)
                refreshed += 1
            db.commit()
    finally:
        scraper.client.close()
    stats = _run_fix_prices(status_filter="active,relisted")
    rprint({"refreshed": refreshed, "post_fix": stats})


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


@app.command("merge-orphans")
def merge_orphans_cmd(dry_run: bool = typer.Option(True, help="Только показать, без записи")) -> None:
    """Склеить Property с одним адресом+площадью±2+этажом, но разным fingerprint (цена)."""
    from collections import defaultdict

    from sqlalchemy import func, select

    from app.db.models import Listing, Property
    from app.domain.fingerprint import normalize_address, round_area
    from app.domain.property_match import merge_properties

    init_db()
    SessionLocal = get_session_factory()
    merged = 0
    groups = 0
    with SessionLocal() as db:
        props = list(
            db.scalars(
                select(Property).where(
                    Property.address_norm.is_not(None),
                    Property.area_sqm.is_not(None),
                )
            ).all()
        )
        buckets: dict[tuple, list[Property]] = defaultdict(list)
        for p in props:
            addr = normalize_address(p.address_norm)
            if not addr or len(addr) < 8:
                continue
            area = round_area(p.area_sqm)
            key = (addr, p.floor, (p.deal_type or "").lower(), area)
            buckets[key].append(p)

        for key, items in buckets.items():
            if len(items) < 2:
                continue
            # Also merge near areas (±2) that rounded differently — already rounded
            # Keep the one with most listings / oldest
            scored = []
            for p in items:
                n = db.scalar(
                    select(func.count()).select_from(Listing).where(Listing.property_id == p.id)
                ) or 0
                scored.append((n, p.first_seen_at, p))
            scored.sort(key=lambda x: (-x[0], x[1]))
            keep = scored[0][2]
            groups += 1
            for _, _, drop in scored[1:]:
                # Area already same band via round_area key; still check ±2 for safety
                if abs(float(keep.area_sqm) - float(drop.area_sqm)) > 2.0:
                    continue
                if dry_run:
                    rprint(
                        f"[yellow]would merge[/yellow] #{drop.id} → #{keep.id} "
                        f"({key[0][:40]} · {key[3]} м² · floor={key[1]})"
                    )
                    merged += 1
                else:
                    moved = merge_properties(db, keep.id, drop.id)
                    merged += 1 if moved or True else 0
        if not dry_run:
            db.commit()
    rprint({"groups": groups, "merged": merged, "dry_run": dry_run})


@app.command("fix-mojibake")
def fix_mojibake_cmd(
    source: Optional[str] = typer.Option("olx", help="Источник (по умолчанию olx)"),
) -> None:
    """Починить double-UTF-8 («абракадабру») в title/description/address."""
    from sqlalchemy import select

    from app.db.models import Listing
    from app.scrapers.text_fix import fix_mojibake, looks_like_mojibake

    init_db()
    SessionLocal = get_session_factory()
    fixed = 0
    scanned = 0
    with SessionLocal() as db:
        q = select(Listing)
        if source:
            q = q.where(Listing.source == source)
        for lst in db.scalars(q):
            scanned += 1
            changed = False
            for attr in ("title", "description", "address_raw", "district", "city"):
                val = getattr(lst, attr, None)
                if not val:
                    continue
                if not (looks_like_mojibake(val) or "Ð" in val or "Ñ" in val):
                    continue
                new = fix_mojibake(val)
                if new and new != val:
                    setattr(lst, attr, new)
                    changed = True
            if changed:
                fixed += 1
        db.commit()
    rprint({"source": source, "scanned": scanned, "fixed": fixed})


@app.command("snapshot-market")
def snapshot_market() -> None:
    """Write/refresh today's market snapshot for /stats charts."""
    from app.domain.market_history import record_market_snapshot

    init_db()
    SessionLocal = get_session_factory()
    with SessionLocal() as db:
        snap = record_market_snapshot(db, force=True)
    rprint(
        {
            "day": snap.day,
            "sale_median": snap.sale_median_psm,
            "rent_median": snap.rent_median_psm,
            "sale_active": snap.sale_active_n,
            "rent_active": snap.rent_active_n,
        }
    )


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


@app.command("probe-olx")
def probe_olx(pages: int = 1) -> None:
    """Проверка доступа к OLX (CloudFront / URL / парсер)."""
    from app.config import get_settings
    from app.scrapers.http_utils import HttpClient
    from app.scrapers.olx import OlxScraper

    settings = get_settings()
    if not settings.crawl_tls_impersonate:
        rprint("[yellow]CRAWL_TLS_IMPERSONATE пуст — OLX часто отвечает 403[/yellow]")
    client = HttpClient()
    scraper = OlxScraper(client)
    total = 0
    try:
        for item in scraper.crawl(max_pages=max(1, pages), needs_detail=lambda _: False):
            total += 1
            if total <= 3:
                title = (item.title or "")[:60].encode("ascii", "replace").decode()
                rprint(
                    f"[green]{item.external_id}[/green] {item.price} {item.currency} - {title}"
                )
    finally:
        client.close()
    if total:
        rprint(f"[green]OLX OK[/green]: {total} cards")
    else:
        rprint(
            "[red]OLX: 0 cards[/red] — проверьте CRAWL_TLS_IMPERSONATE=chrome131 и/или HTTP_PROXY"
        )


@app.command()
def serve(
    host: str = "127.0.0.1",
    port: int = 8000,
    reload: bool = typer.Option(
        False,
        "--reload",
        help="Автоперезагрузка при изменении кода (локальная разработка).",
    ),
) -> None:
    """Run web UI + API."""
    from pathlib import Path

    import uvicorn

    root = Path(__file__).resolve().parents[1]
    kwargs: dict = {"host": host, "port": port, "reload": reload}
    if reload:
        kwargs["reload_dirs"] = [str(root / "app"), str(root / "scripts")]
        rprint("[yellow]reload=ON[/yellow] — код в app/ и scripts/ подхватывается автоматически")
    uvicorn.run("app.main:app", **kwargs)


if __name__ == "__main__":
    app()
