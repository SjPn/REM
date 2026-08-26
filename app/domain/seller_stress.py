from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, load_only

from app.db.models import Listing, PropertyEvent
from app.domain.enums import EventType
from app.domain.market_stats import (
    KYIV_DISTRICTS,
    count_active_inventory,
    extract_district,
    normalize_district,
)
from app.domain.ttl_cache import cache_get


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


def _empty_counters() -> dict[str, dict[str, int]]:
    return {d: {"v": 0, "r": 0, "p": 0} for d in KYIV_DISTRICTS}


def _scores_for(
    counters: dict[str, dict[str, int]],
    active: dict[str, int],
) -> list[DistrictStress]:
    out: list[DistrictStress] = []
    for name in KYIV_DISTRICTS:
        a = active.get(name, 0)
        c = counters[name]
        v, r, p = c["v"], c["r"], c["p"]
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
                    f"за неделю: сняли {v}, вернули {r}, уценили {p}; "
                    f"сейчас в сети {a}"
                ),
            )
        )
    out.sort(key=lambda x: x.score, reverse=True)
    return out


def compute_seller_stress(
    db: Session, *, days: int = 7, deal_type: str | None = None
) -> list[DistrictStress]:
    """Тиск продавців 0–100 по району (гіпотеза): зникнення, релісти, дампи ціни."""
    # One scan builds sale+rent+all so mode toggle is free after first hit.
    key = f"seller_stress_bundle:{days}"

    def _build() -> dict[str, list[DistrictStress]]:
        since = _since(days)
        by_mode = {
            "sale": _empty_counters(),
            "rent": _empty_counters(),
            "all": _empty_counters(),
        }
        active_sale = {d: 0 for d in KYIV_DISTRICTS}
        active_rent = {d: 0 for d in KYIV_DISTRICTS}
        active_all = {d: 0 for d in KYIV_DISTRICTS}

        inv = count_active_inventory(db)
        for row in inv["districts"]:
            name = row["district"]
            if name not in active_sale:
                continue
            s = int(row.get("sale") or 0)
            r = int(row.get("rent") or 0)
            active_sale[name] = s
            active_rent[name] = r
            active_all[name] = s + r

        events = list(
            db.scalars(
                select(PropertyEvent).where(PropertyEvent.occurred_at >= since)
            ).all()
        )
        listing_ids = {e.listing_id for e in events if e.listing_id}
        listing_cache: dict[int, Listing] = {}
        if listing_ids:
            ids = list(listing_ids)
            for i in range(0, len(ids), 400):
                chunk = ids[i : i + 400]
                for lst in db.scalars(
                    select(Listing)
                    .where(Listing.id.in_(chunk))
                    .options(
                        load_only(
                            Listing.id,
                            Listing.deal_type,
                            Listing.district,
                            Listing.address_raw,
                            Listing.title,
                            Listing.city,
                        )
                    )
                ):
                    listing_cache[int(lst.id)] = lst

        for ev in events:
            listing = listing_cache.get(int(ev.listing_id)) if ev.listing_id else None
            d = _district_of(
                listing, ev.payload if isinstance(ev.payload, dict) else None
            )
            if d not in by_mode["all"]:
                continue
            bump_key = None
            if ev.event_type == EventType.VANISHED.value:
                if not (
                    isinstance(ev.payload, dict) and ev.payload.get("level") == "property"
                ):
                    continue
                bump_key = "v"
            elif ev.event_type == EventType.RELISTED.value:
                bump_key = "r"
            elif ev.event_type == EventType.PRICE_CHANGED.value:
                payload = ev.payload or {}
                try:
                    old_p = float(payload["old_price"])
                    new_p = float(payload["new_price"])
                except (KeyError, TypeError, ValueError):
                    continue
                if new_p >= old_p:
                    continue
                bump_key = "p"
            if bump_key is None:
                continue
            by_mode["all"][d][bump_key] += 1
            if listing is not None and listing.deal_type in ("sale", "rent"):
                by_mode[listing.deal_type][d][bump_key] += 1

        return {
            "sale": _scores_for(by_mode["sale"], active_sale),
            "rent": _scores_for(by_mode["rent"], active_rent),
            "all": _scores_for(by_mode["all"], active_all),
        }

    bundle = cache_get(key, 180.0, _build)
    mode = deal_type if deal_type in ("sale", "rent") else "all"
    return bundle[mode]
