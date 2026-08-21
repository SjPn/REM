from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import DealHypothesis, Listing, PropertyEvent
from app.domain.enums import EventType, ListingStatus
from app.domain.market_stats import extract_district, normalize_district, to_usd


def _since(hours: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=hours)


def _count_events(db: Session, event_type: str, since: datetime) -> int:
    return (
        db.scalar(
            select(func.count())
            .select_from(PropertyEvent)
            .where(
                PropertyEvent.event_type == event_type,
                PropertyEvent.occurred_at >= since,
            )
        )
        or 0
    )


def _price_drop_events(db: Session, since: datetime) -> int:
    n = 0
    rows = db.scalars(
        select(PropertyEvent).where(
            PropertyEvent.event_type == EventType.PRICE_CHANGED.value,
            PropertyEvent.occurred_at >= since,
        )
    ).all()
    for e in rows:
        payload = e.payload or {}
        old_p = payload.get("old_price")
        new_p = payload.get("new_price")
        try:
            if old_p is not None and new_p is not None and float(new_p) < float(old_p):
                n += 1
        except (TypeError, ValueError):
            continue
    return n


def activity_summary(db: Session, *, hours: int = 24) -> dict[str, int]:
    """In-UI alert counters for the chosen window."""
    since = _since(hours)
    sold_or_rented = (
        db.scalar(
            select(func.count())
            .select_from(Listing)
            .where(
                Listing.status.in_(
                    [
                        ListingStatus.SOLD_MARKED.value,
                        ListingStatus.RENTED_MARKED.value,
                    ]
                ),
                Listing.updated_at >= since,
            )
        )
        or 0
    )
    likely_deals = (
        db.scalar(
            select(func.count())
            .select_from(DealHypothesis)
            .where(
                DealHypothesis.bucket == "likely_deal",
                DealHypothesis.created_at >= since,
            )
        )
        or 0
    )
    return {
        "hours": hours,
        "new_listings": _count_events(db, EventType.APPEARED.value, since),
        "vanished": _count_events(db, EventType.VANISHED.value, since),
        "relisted": _count_events(db, EventType.RELISTED.value, since),
        "price_drops": _price_drop_events(db, since),
        "sold_or_rented": sold_or_rented,
        "likely_deals": likely_deals,
    }


def recent_events(db: Session, *, hours: int = 24, limit: int = 40) -> list[PropertyEvent]:
    since = _since(hours)
    return list(
        db.scalars(
            select(PropertyEvent)
            .where(PropertyEvent.occurred_at >= since)
            .order_by(PropertyEvent.occurred_at.desc())
            .limit(limit)
        ).all()
    )


_CAP_RE = re.compile(
    r"(?:cap[\s\-]?rate|кап(?:італ)?\.?\s*ставк\w*|капіталізац\w*)\s*[:\-]?\s*(\d+(?:[.,]\d+)?)\s*%?",
    re.IGNORECASE,
)
_NOI_RE = re.compile(
    r"(?:NOI|чистий\s+операц(?:ійний)?\s+дохід|чистый\s+операционн\w*\s+доход)\s*[:\-]?\s*"
    r"(\d{1,3}(?:[ \u00a0]?\d{3})+|\d+(?:[.,]\d+)?)",
    re.IGNORECASE,
)


def parse_cap_and_noi(text: str | None) -> dict[str, float]:
    """Extract only explicitly stated cap rate / NOI. Never invent."""
    out: dict[str, float] = {}
    if not text:
        return out
    m = _CAP_RE.search(text)
    if m:
        try:
            val = float(m.group(1).replace(",", "."))
            if math.isfinite(val) and 0 < val < 100:
                out["cap_rate_pct"] = val
        except ValueError:
            pass
    m = _NOI_RE.search(text)
    if m:
        raw = m.group(1).replace("\xa0", "").replace(" ", "").replace(",", ".")
        if re.fullmatch(r"\d{1,3}(\.\d{3})+", raw):
            raw = raw.replace(".", "")
        try:
            val = float(raw)
            if math.isfinite(val) and val > 0:
                out["noi"] = val
        except ValueError:
            pass
    return out


def listing_psm_usd(price: float | None, currency: str | None, area: float | None) -> float | None:
    if price is None or area is None:
        return None
    try:
        p, a = float(price), float(area)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(p) or not math.isfinite(a) or p <= 0 or a <= 0:
        return None
    usd = to_usd(p, currency)
    if usd is None or not math.isfinite(usd):
        return None
    psm = usd / a
    if not math.isfinite(psm) or psm <= 0:
        return None
    return psm


@dataclass
class MarketHint:
    below_market: bool
    discount_pct: float | None
    ref_median_psm: float | None
    district: str | None


def below_market_hint(
    *,
    price: float | None,
    currency: str | None,
    area: float | None,
    deal_type: str | None,
    district: str | None,
    address: str | None,
    title: str | None,
    city: str | None,
    median_by_district: dict[str, float],
    city_median: float | None,
    threshold: float = 0.12,
) -> MarketHint:
    """True if listing $/m² is meaningfully below district (or city) median."""
    psm = listing_psm_usd(price, currency, area)
    dist = normalize_district(district) or extract_district(address, title, city)
    ref = median_by_district.get(dist) if dist else None
    if ref is None:
        ref = city_median
    if psm is None or ref is None or ref <= 0:
        return MarketHint(False, None, ref, dist)
    discount = (ref - psm) / ref
    return MarketHint(discount >= threshold, round(discount * 100, 1), ref, dist)


_AGENCY_RE = re.compile(
    r"(агент|ріелтор|риелтор|realtor|broker|АН\b|агентство|agency|консульт)",
    re.IGNORECASE,
)


def classify_seller(
    *,
    agency: str | None,
    phone: str | None,
    title: str | None = None,
    description: str | None = None,
    phone_listing_count: int = 1,
) -> str:
    """owner | agency | unknown — heuristic only."""
    blob = " ".join(x for x in (agency, title, description) if x)
    if agency and agency.strip():
        return "agency"
    if _AGENCY_RE.search(blob or ""):
        return "agency"
    if phone_listing_count >= 3:
        return "agency"
    if phone and phone_listing_count == 1 and not _AGENCY_RE.search(blob or ""):
        return "owner"
    return "unknown"
