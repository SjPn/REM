from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Listing, PropertyEvent
from app.domain.enums import EventType
from app.domain.market_stats import KYIV_DISTRICTS, extract_district, normalize_district


@dataclass
class DistrictStress:
    district: str
    score: int  # 0–100
    vanished_7d: int
    relisted_7d: int
    price_drops_7d: int
    active: int
    detail: str


def _since(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def _district_of(listing: Listing | None, payload: dict | None = None) -> str | None:
    if listing:
        return normalize_district(listing.district) or extract_district(
            listing.address_raw, listing.title, listing.city
        )
    if payload:
        return normalize_district(payload.get("district")) or extract_district(
            payload.get("address_raw"), payload.get("title"), payload.get("city")
        )
    return None


def compute_seller_stress(
    db: Session, *, days: int = 7, deal_type: str | None = None
) -> list[DistrictStress]:
    """Тиск продавців 0–100 по району (гіпотеза): зникнення, релісти, дампи ціни."""
    since = _since(days)
    vanished = {d: 0 for d in KYIV_DISTRICTS}
    relisted = {d: 0 for d in KYIV_DISTRICTS}
    drops = {d: 0 for d in KYIV_DISTRICTS}
    active = {d: 0 for d in KYIV_DISTRICTS}

    active_q = select(Listing).where(Listing.status.in_(["active", "relisted"]))
    if deal_type:
        active_q = active_q.where(Listing.deal_type == deal_type)
    for lst in db.scalars(active_q):
        d = _district_of(lst)
        if d in active:
            active[d] += 1

    events = db.scalars(
        select(PropertyEvent).where(PropertyEvent.occurred_at >= since)
    ).all()
    listing_cache: dict[int, Listing | None] = {}

    for ev in events:
        listing = None
        if ev.listing_id:
            if ev.listing_id not in listing_cache:
                listing_cache[ev.listing_id] = db.get(Listing, ev.listing_id)
            listing = listing_cache[ev.listing_id]
        if deal_type and listing is not None and listing.deal_type != deal_type:
            continue
        if deal_type and listing is None:
            continue
        d = _district_of(listing, ev.payload if isinstance(ev.payload, dict) else None)
        if d not in vanished:
            continue
        if ev.event_type == EventType.VANISHED.value:
            vanished[d] += 1
        elif ev.event_type == EventType.RELISTED.value:
            relisted[d] += 1
        elif ev.event_type == EventType.PRICE_CHANGED.value:
            payload = ev.payload or {}
            try:
                old_p = float(payload["old_price"])
                new_p = float(payload["new_price"])
            except (KeyError, TypeError, ValueError):
                continue
            if new_p < old_p:
                drops[d] += 1

    out: list[DistrictStress] = []
    for name in KYIV_DISTRICTS:
        a = active[name]
        v, r, p = vanished[name], relisted[name], drops[name]
        if a == 0 and v == 0 and r == 0 and p == 0:
            continue
        base = a if a > 0 else 1
        raw = (v * 40 + r * 25 + p * 20) / base
        score = int(max(0, min(100, round(raw * 8))))
        out.append(
            DistrictStress(
                district=name,
                score=score,
                vanished_7d=v,
                relisted_7d=r,
                price_drops_7d=p,
                active=a,
                detail=(
                    f"за 7 дней: исчезло {v}, повторно выставили {r}, снизили цену {p}; "
                    f"активных сейчас {a}"
                ),
            )
        )
    out.sort(key=lambda x: x.score, reverse=True)
    return out
