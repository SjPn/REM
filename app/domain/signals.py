from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import DealHypothesis, Listing, Property, PropertyEvent
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


def _count_listing_events(
    db: Session,
    event_type: str,
    since: datetime,
    *,
    deal_type: str | None = None,
) -> int:
    q = (
        select(func.count())
        .select_from(PropertyEvent)
        .join(Listing, PropertyEvent.listing_id == Listing.id)
        .where(
            PropertyEvent.event_type == event_type,
            PropertyEvent.occurred_at >= since,
            PropertyEvent.listing_id.is_not(None),
        )
    )
    if deal_type:
        q = q.where(Listing.deal_type == deal_type)
    return db.scalar(q) or 0


def _price_drop_events(db: Session, since: datetime, *, deal_type: str | None = None) -> int:
    return len(listing_ids_for_price_drops(db, since=since, deal_type=deal_type))


def _is_property_level_vanish(payload: dict | None) -> bool:
    return bool(payload) and payload.get("level") == "property"


def _count_property_vanished_events(
    db: Session,
    since: datetime,
    *,
    deal_type: str | None = None,
) -> int:
    """Count properties that fully left the market (property-level vanish only)."""
    q = (
        select(PropertyEvent)
        .where(
            PropertyEvent.event_type == EventType.VANISHED.value,
            PropertyEvent.occurred_at >= since,
            PropertyEvent.property_id.is_not(None),
        )
        .order_by(PropertyEvent.occurred_at.desc())
    )
    if deal_type:
        q = q.join(Property, PropertyEvent.property_id == Property.id).where(
            Property.deal_type == deal_type
        )
    events = db.scalars(q).all()
    counted: set[int] = set()
    for ev in events:
        if not _is_property_level_vanish(ev.payload):
            continue
        pid = int(ev.property_id)
        if pid in counted:
            continue
        # Skip if object is active again on any source.
        active = db.scalar(
            select(func.count())
            .select_from(Listing)
            .where(
                Listing.property_id == pid,
                Listing.status.in_(
                    [ListingStatus.ACTIVE.value, ListingStatus.RELISTED.value]
                ),
            )
        )
        if (active or 0) > 0:
            continue
        counted.add(pid)
    return len(counted)


def listing_ids_for_vanished(
    db: Session,
    *,
    since: datetime,
    deal_type: str | None = None,
) -> list[int]:
    """One representative listing per property that fully vanished in the window."""
    q = (
        select(PropertyEvent)
        .where(
            PropertyEvent.event_type == EventType.VANISHED.value,
            PropertyEvent.occurred_at >= since,
            PropertyEvent.property_id.is_not(None),
        )
        .order_by(PropertyEvent.occurred_at.desc())
    )
    if deal_type:
        q = q.join(Property, PropertyEvent.property_id == Property.id).where(
            Property.deal_type == deal_type
        )
    events = db.scalars(q).all()
    ids: list[int] = []
    seen_properties: set[int] = set()
    for ev in events:
        if not _is_property_level_vanish(ev.payload):
            continue
        pid = int(ev.property_id)
        if pid in seen_properties:
            continue
        active = db.scalar(
            select(func.count())
            .select_from(Listing)
            .where(
                Listing.property_id == pid,
                Listing.status.in_(
                    [ListingStatus.ACTIVE.value, ListingStatus.RELISTED.value]
                ),
            )
        )
        if (active or 0) > 0:
            continue
        seen_properties.add(pid)
        lid = ev.listing_id
        if lid is not None:
            ids.append(int(lid))
            continue
        rep = db.scalar(
            select(Listing.id)
            .where(
                Listing.property_id == pid,
                Listing.status == ListingStatus.VANISHED.value,
            )
            .order_by(Listing.vanished_at.desc())
        )
        if rep is not None:
            ids.append(int(rep))
    return ids


def listing_ids_for_price_drops(
    db: Session,
    *,
    since: datetime,
    deal_type: str | None = None,
) -> list[int]:
    """Listing ids with a price drop (new < old) in the window."""
    rows = db.scalars(
        select(PropertyEvent)
        .where(
            PropertyEvent.event_type == EventType.PRICE_CHANGED.value,
            PropertyEvent.occurred_at >= since,
            PropertyEvent.listing_id.is_not(None),
        )
        .order_by(PropertyEvent.occurred_at.desc())
    ).all()
    ids: list[int] = []
    seen: set[int] = set()
    for e in rows:
        payload = e.payload or {}
        old_p = payload.get("old_price")
        new_p = payload.get("new_price")
        try:
            if old_p is None or new_p is None or float(new_p) >= float(old_p):
                continue
        except (TypeError, ValueError):
            continue
        lid = e.listing_id
        if lid is None or int(lid) in seen:
            continue
        seen.add(int(lid))
        ids.append(int(lid))
    if deal_type and ids:
        allowed = set(
            db.scalars(
                select(Listing.id).where(
                    Listing.id.in_(ids),
                    Listing.deal_type == deal_type,
                )
            ).all()
        )
        ids = [i for i in ids if i in allowed]
    return ids


def activity_summary(
    db: Session, *, hours: int = 24, deal_type: str | None = None
) -> dict[str, int]:
    """In-UI alert counters for the chosen window (optionally sale/rent only)."""
    from app.domain.ttl_cache import cache_get

    cache_key = f"activity_summary:{hours}:{deal_type or 'all'}"

    def _build() -> dict[str, int]:
        since = _since(hours)
        if deal_type == "sale":
            marked_statuses = [ListingStatus.SOLD_MARKED.value]
        elif deal_type == "rent":
            marked_statuses = [ListingStatus.RENTED_MARKED.value]
        else:
            marked_statuses = [
                ListingStatus.SOLD_MARKED.value,
                ListingStatus.RENTED_MARKED.value,
            ]
        marked_q = select(func.count()).select_from(Listing).where(
            Listing.status.in_(marked_statuses),
            Listing.updated_at >= since,
        )
        if deal_type:
            marked_q = marked_q.where(Listing.deal_type == deal_type)
        sold_or_rented = db.scalar(marked_q) or 0

        likely_q = (
            select(func.count())
            .select_from(DealHypothesis)
            .join(Listing, DealHypothesis.listing_id == Listing.id)
            .where(
                DealHypothesis.bucket == "likely_deal",
                DealHypothesis.created_at >= since,
            )
        )
        if deal_type:
            likely_q = likely_q.where(Listing.deal_type == deal_type)
        likely_deals = db.scalar(likely_q) or 0

        return {
            "hours": hours,
            "new_listings": _count_listing_events(
                db, EventType.APPEARED.value, since, deal_type=deal_type
            ),
            "vanished": _count_property_vanished_events(
                db, since, deal_type=deal_type
            ),
            "relisted": _count_listing_events(
                db, EventType.RELISTED.value, since, deal_type=deal_type
            ),
            "price_drops": _price_drop_events(db, since, deal_type=deal_type),
            "sold_or_rented": sold_or_rented,
            "likely_deals": likely_deals,
        }

    return cache_get(cache_key, 30.0, _build)


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


def listing_psm_usd(
    price: float | None,
    currency: str | None,
    area: float | None,
    *,
    deal_type: str | None = None,
    price_per_sqm: float | None = None,
) -> float | None:
    from app.domain.pricing import effective_listing_psm_usd

    return effective_listing_psm_usd(
        price,
        currency,
        area,
        deal_type=deal_type,
        price_per_sqm=price_per_sqm,
    )


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
    price_per_sqm: float | None = None,
) -> MarketHint:
    """True if listing $/m² is meaningfully below district (or city) median."""
    psm = listing_psm_usd(
        price, currency, area, deal_type=deal_type, price_per_sqm=price_per_sqm
    )
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

# OPEX / operating expenses in rent (explicit text only)
OPEX_WITH = "with"
OPEX_WITHOUT = "without"
OPEX_UNKNOWN = "unknown"

_OPEX_WITHOUT_RE = re.compile(
    r"("
    r"без\s*(?:opex|опекс)|"
    r"\+\s*(?:opex|опекс)|"
    r"(?:opex|опекс)\s*(?:окремо|отдельно)|"
    r"не\s*включа\w*\s*(?:opex|опекс)|"
    r"netto|net[\s\-]?rent|nnn|triple\s*net|"
    r"чист[аяоїі]*\s*(?:оренд|аренд)|"
    r"без\s*(?:комунал|коммунал)|"
    r"(?:комуналка|коммуналка)\s*(?:окремо|отдельно)|"
    r"(?:експлуатац|эксплуатац)\w*\s*(?:окремо|отдельно)"
    r")",
    re.IGNORECASE,
)
_OPEX_WITH_RE = re.compile(
    r"("
    # не матчить «з» внутри слова «без»
    r"(?<![А-Яа-яІіЇїЄєҐґA-Za-z])(?:з|с|со)\s*(?:opex|опекс)|"
    r"включа\w*\s*(?:opex|опекс)|включая\s*(?:opex|опекс)|включаючи\s*(?:opex|опекс)|"
    r"включен\w*\s*(?:opex|опекс)|"
    r"all\s*in(?:clusive)?|все\s*включен|"
    r"грязн\w*\s*(?:оренд|аренд)|gross\s*rent|"
    r"(?<![А-Яа-яІіЇїЄєҐґA-Za-z])(?:з|с)\s*(?:комунал|коммунал)|"
    r"включа\w*\s*(?:комунал|коммунал|експлуатац|эксплуатац)|"
    r"(?:opex|опекс)\s*включ"
    r")",
    re.IGNORECASE,
)


def detect_opex(*parts: str | None) -> str:
    """with | without | unknown — only from explicit listing text. Never invent."""
    blob = " ".join(p for p in parts if p)
    if not blob.strip():
        return OPEX_UNKNOWN
    has_without = bool(_OPEX_WITHOUT_RE.search(blob))
    has_with = bool(_OPEX_WITH_RE.search(blob))
    # «без OPEX» приоритетнее ложного «з OPEX» внутри того же слова
    if has_without and not has_with:
        return OPEX_WITHOUT
    if has_with and not has_without:
        return OPEX_WITH
    if has_without and has_with:
        # конфликт маркеров — не угадываем
        return OPEX_UNKNOWN
    return OPEX_UNKNOWN


def resolve_listing_opex(listing) -> str:
    """Prefer stored signal; else parse title/description."""
    extra = getattr(listing, "raw_extra", None) or {}
    stored = extra.get("opex")
    if stored in (OPEX_WITH, OPEX_WITHOUT, OPEX_UNKNOWN):
        # Re-check unknown from text if we can improve
        if stored != OPEX_UNKNOWN:
            return stored
    return detect_opex(
        getattr(listing, "title", None),
        getattr(listing, "description", None),
    )


_OWNER_RE = re.compile(
    r"("
    r"від\s*власник|"
    r"от\s*собственник|"
    r"власник\b|"
    r"собственник\b|"
    r"без\s*коміс\w*\s*(?:від\s*)?власник|"
    r"без\s*комисс\w*\s*(?:от\s*)?собственник|"
    r"owner\s*direct|"
    r"private\s*owner|"
    r"без\s*посередник|"
    r"без\s*посредник"
    r")",
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
    """owner | agency | unknown — heuristic only.

    Best signals (strong → weak):
    1) agency name / realtor keywords → agency
    2) same phone on many listings → agency
    3) explicit «від власника» + unique phone → owner
    4) unique phone (1 listing), no agency markers → owner
    """
    blob = " ".join(x for x in (agency, title, description) if x)
    owner_hint = bool(_OWNER_RE.search(blob or ""))
    if agency and agency.strip() and not owner_hint:
        return "agency"
    if _AGENCY_RE.search(blob or "") and not owner_hint:
        return "agency"
    if phone_listing_count >= 3:
        return "agency"
    if owner_hint and phone_listing_count <= 2:
        return "owner"
    if phone and phone_listing_count == 1 and not _AGENCY_RE.search(blob or ""):
        return "owner"
    if phone and phone_listing_count == 2 and owner_hint:
        return "owner"
    return "unknown"
