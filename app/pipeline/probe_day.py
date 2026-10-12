"""Прогон на десятки минут: голова лент и прямая проверка старых карточек."""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Listing, PropertyEvent, utcnow
from app.domain.enums import EventType, ListingStatus
from app.domain.market_history import record_market_snapshot
from app.domain.probe import (
    classify_probe_body,
    decide_probe,
    listing_url_allowed,
    probe_miss,
    probe_stamp,
    utc_day,
    write_probe_meta,
)
from app.pipeline.ingest import upsert_listing
from app.pipeline.light_day import run_light_day
from app.pipeline.reconcile import _refresh_property_active, reconcile_property_vanish
from app.scrapers import SCRAPERS, get_scraper
from app.scrapers.base import RawListing
from app.scrapers.http_utils import HttpClient, PortalBlockedError

logger = logging.getLogger(__name__)

_ACTIVE = (ListingStatus.ACTIVE.value, ListingStatus.RELISTED.value)
_EXPLICIT = {
    "sold": (ListingStatus.SOLD_MARKED.value, "sold"),
    "rented": (ListingStatus.RENTED_MARKED.value, "rented"),
}


def _raw_from_listing(listing: Listing) -> RawListing:
    return RawListing(
        source=listing.source,
        external_id=listing.external_id,
        url=listing.url,
        deal_type=listing.deal_type,
        title=listing.title,
        description=listing.description,
        property_type=listing.property_type,
        price=listing.price,
        currency=listing.currency,
        price_per_sqm=listing.price_per_sqm,
        area_sqm=listing.area_sqm,
        floor=listing.floor,
        rooms=listing.rooms,
        address_raw=listing.address_raw,
        district=listing.district,
        city=listing.city,
        lat=listing.lat,
        lon=listing.lon,
        phone=listing.phone,
        agency=listing.agency,
    )


def _try_parse(scraper, html: str, raw: RawListing) -> RawListing | None:
    try:
        parsed = scraper.parse_detail(html, raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning("probe parse failed %s %s: %s", raw.source, raw.external_id, exc)
        return None
    if parsed is None:
        return None
    parsed.source = raw.source
    parsed.external_id = raw.external_id
    parsed.url = raw.url or parsed.url
    parsed.deal_type = raw.deal_type
    return parsed


def _remember(listing: Listing, *, now: datetime, miss: int, miss_on: str | None, kind: str) -> None:
    listing.raw_extra = write_probe_meta(
        listing.raw_extra,
        at=now.isoformat(),
        miss=miss,
        miss_on=miss_on,
        kind=kind,
    )


def _mark_explicit(db: Session, listing: Listing, kind: str, html: str, scraper, now: datetime) -> None:
    new_status, status_raw = _EXPLICIT[kind]
    raw = _raw_from_listing(listing)
    raw.source_status_raw = status_raw
    detailed = _try_parse(scraper, html, raw) or raw
    detailed.source_status_raw = status_raw
    saved, _wrote = upsert_listing(db, detailed)
    target = saved or listing
    if target.status != new_status:
        db.add(
            PropertyEvent(
                property_id=target.property_id,
                listing_id=target.id,
                event_type=EventType.STATUS_CHANGED.value,
                occurred_at=now,
                payload={
                    "old_status": target.status,
                    "new_status": new_status,
                    "source_status_raw": status_raw,
                    "reason": "probe",
                },
            )
        )
        target.status = new_status
        target.source_status_raw = status_raw
    if target.property_id:
        _refresh_property_active(db, int(target.property_id))
    _remember(target, now=now, miss=0, miss_on=None, kind=kind)


def _confirm_vanish(db: Session, listing: Listing, now: datetime) -> None:
    listing.status = ListingStatus.VANISHED.value
    listing.vanished_at = now
    listing.source_status_raw = "probe_removed"
    db.flush()
    if listing.property_id:
        reconcile_property_vanish(
            db,
            int(listing.property_id),
            now=now,
            trigger_listing=listing,
        )


def apply_probe_hit(
    db: Session,
    listing: Listing,
    kind: str,
    html: str,
    *,
    scraper,
    now: datetime | None = None,
) -> str:
    """Записать итог одной проверки. Возвращает действие: alive|miss|vanish|sold|rented."""
    now = now or utcnow()
    today = utc_day(now)
    if kind in _EXPLICIT:
        _mark_explicit(db, listing, kind, html, scraper, now)
        return kind
    if kind == "alive":
        raw = _raw_from_listing(listing)
        detailed = _try_parse(scraper, html, raw)
        target = listing
        if detailed is not None:
            detailed.source_status_raw = "active"
            saved, _wrote = upsert_listing(db, detailed)
            if saved is not None:
                target = saved
        if target.status in (ListingStatus.VANISHED.value, ListingStatus.UNKNOWN.value):
            target.status = ListingStatus.ACTIVE.value
        target.last_seen_at = now
        _remember(target, now=now, miss=0, miss_on=None, kind="alive")
        return "alive"

    prev_miss, prev_on = probe_miss(listing.raw_extra)
    decision = decide_probe(prev_miss, prev_on, "removed", today)
    if decision.action == "vanish":
        _confirm_vanish(db, listing, now)
    _remember(
        listing,
        now=now,
        miss=decision.miss,
        miss_on=decision.miss_on,
        kind="removed",
    )
    return decision.action


def probe_source(
    db: Session,
    source: str,
    seen_ids: set[str],
    *,
    deadline: float,
    fetch,
    scraper,
    clock=time.monotonic,
    now_fn=utcnow,
) -> dict:
    """Проверить активные карточки источника, которых не было в голове ленты."""
    rows = db.execute(
        select(Listing.id, Listing.external_id, Listing.url, Listing.raw_extra).where(
            Listing.source == source,
            Listing.status.in_(_ACTIVE),
        )
    ).all()
    ordered = sorted(rows, key=lambda row: probe_stamp(row.raw_extra))
    stats = {
        "checked": 0,
        "alive": 0,
        "miss": 0,
        "vanished": 0,
        "sold": 0,
        "rented": 0,
        "skipped": 0,
        "in_head": 0,
        "stopped": None,
    }
    transport_fails = 0
    for row in ordered:
        if clock() >= deadline:
            stats["stopped"] = "deadline"
            break
        ext = str(row.external_id or "")
        if ext in seen_ids:
            stats["in_head"] += 1
            continue
        url = str(row.url or "")
        if not listing_url_allowed(source, url):
            stats["skipped"] += 1
            continue
        try:
            code, html = fetch(url)
        except PortalBlockedError:
            stats["stopped"] = "blocked"
            break
        except Exception as exc:  # noqa: BLE001
            logger.warning("probe transport %s %s: %s", source, url, exc)
            transport_fails += 1
            stats["skipped"] += 1
            if transport_fails >= 3:
                stats["stopped"] = "transport"
                break
            continue
        transport_fails = 0
        kind = classify_probe_body(int(code), html or "")
        if kind == "blocked":
            stats["stopped"] = "blocked"
            break
        if kind == "skip":
            stats["skipped"] += 1
            continue
        listing = db.get(Listing, int(row.id))
        if listing is None or listing.status not in _ACTIVE:
            continue
        action = apply_probe_hit(
            db,
            listing,
            kind,
            html or "",
            scraper=scraper,
            now=now_fn(),
        )
        db.commit()
        stats["checked"] += 1
        if action == "vanish":
            stats["vanished"] += 1
        elif action in stats:
            stats[action] += 1
        if stats["checked"] % 50 == 0:
            logger.info("probe %s checked=%s", source, stats["checked"])
    logger.info("probe %s done %s", source, stats)
    return stats


def _empty_probe_stats(stopped: str | None = None) -> dict:
    return {
        "checked": 0,
        "alive": 0,
        "miss": 0,
        "vanished": 0,
        "sold": 0,
        "rented": 0,
        "skipped": 0,
        "in_head": 0,
        "stopped": stopped,
    }


def _take_seen(head: dict) -> dict[str, set[str]]:
    seen: dict[str, set[str]] = {}
    for src, info in (head.get("sources") or {}).items():
        if not isinstance(info, dict):
            seen[str(src)] = set()
            continue
        ids = info.pop("external_ids", None) or []
        seen[str(src)] = {str(item) for item in ids if item}
    return seen


def _probe_one_source(source: str, seen_ids: set[str], deadline: float) -> tuple[str, dict]:
    from app.db.models import get_session_factory

    try:
        with get_session_factory()() as db:
            with HttpClient() as client:
                scraper = get_scraper(source, client=client)

                def fetch(url: str) -> tuple[int, str]:
                    return client.probe_url(url)

                stats = probe_source(
                    db,
                    source,
                    seen_ids,
                    deadline=deadline,
                    fetch=fetch,
                    scraper=scraper,
                )
        return source, stats
    except Exception as exc:  # noqa: BLE001
        logger.exception("probe worker failed %s", source)
        stats = _empty_probe_stats("error")
        stats["error"] = str(exc)
        return source, stats


def run_probe_day(
    db: Session,
    *,
    sources: list[str] | None = None,
    minutes: int | None = None,
) -> dict:
    """Голова лент, затем прямые проверки до общего лимита времени."""
    settings = get_settings()
    budget_min = int(minutes if minutes is not None else settings.probe_day_minutes)
    budget_min = max(1, budget_min)
    selected = list(sources or SCRAPERS.keys())
    unknown = [src for src in selected if src not in SCRAPERS]
    if unknown:
        raise KeyError(f"Unknown source: {unknown}. Available: {list(SCRAPERS)}")

    started = time.monotonic()
    deadline = started + budget_min * 60
    started_at = datetime.now(timezone.utc).isoformat()
    logger.info("probe-day: sources=%s minutes=%s", ",".join(selected), budget_min)

    head = run_light_day(
        db,
        sources=selected,
        max_pages=int(settings.probe_head_pages),
        max_details=int(settings.probe_head_details),
        stale_page_limit=int(settings.probe_stale_pages),
        include_ids=True,
    )
    seen = _take_seen(head)
    probes: dict[str, dict] = {}
    if time.monotonic() >= deadline:
        for src in selected:
            probes[src] = _empty_probe_stats("deadline")
    else:
        with ThreadPoolExecutor(max_workers=max(1, len(selected))) as pool:
            futures = {
                pool.submit(_probe_one_source, src, seen.get(src, set()), deadline): src
                for src in selected
            }
            for fut in as_completed(futures):
                src = futures[fut]
                try:
                    name, stats = fut.result()
                except Exception as exc:  # noqa: BLE001
                    logger.exception("probe worker crashed %s", src)
                    stats = _empty_probe_stats("error")
                    stats["error"] = str(exc)
                    name = src
                probes[name] = stats

    try:
        db.expire_all()
        snap = record_market_snapshot(db, force=True)
        snapshot_day = snap.day
    except Exception:  # noqa: BLE001
        logger.exception("probe-day market snapshot failed")
        snapshot_day = None

    head_sources = head.get("sources") or {}
    any_progress = any("error" not in info for info in head_sources.values() if isinstance(info, dict))
    any_progress = any_progress or any(
        int(row.get("checked") or 0) > 0 or row.get("stopped") in {"deadline", "blocked", "transport"}
        for row in probes.values()
    )
    return {
        "mode": "probe",
        "vanish": "confirmed_url",
        "minutes": budget_min,
        "parallel": True,
        "started_at": started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_sec": round(time.monotonic() - started, 1),
        "head": head,
        "probes": probes,
        "market_snapshot_day": snapshot_day,
        "ok": bool(any_progress),
    }
