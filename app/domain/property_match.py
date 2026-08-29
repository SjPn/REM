"""Property identity: exact fingerprint, then soft-match without price as hard key."""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db.models import Listing, Property
from app.domain.fingerprint import (
    FingerprintInput,
    build_fingerprint,
    normalize_address,
    phone_digits,
    round_area,
)
from app.domain.market_stats import to_usd

_STREET_RE = re.compile(
    r"(вул|улиц|просп|пр-т|б-р|бульвар|провул|пер\.|площа|пл\.|"
    r"туп\.|набереж|шосе|дорога|\d)",
    re.IGNORECASE,
)
_BUILDING_NUM_RE = re.compile(
    r"(?:"
    r"№\s*\d+"
    r"|(?:буд|будинок|дом|корп(?:ус)?|корп\.|секц(?:ія)?)\s*\.?\s*\d+"
    r"|(?:офіс|офис|приміщення|помещение|квартира|кв\.?|кім\.?|room)\s*\.?\s*\d+"
    r"|(?:вул|улиц|просп|пр-т|б-р|бульвар|провул|пер\.|площа|пл\.|набереж|шосе)"
    r"[^,\d]{2,60},\s*\d{1,4}[а-яіїєґa-z]?"
    r"|(?:вул|улиц|просп|пр-т|б-р|бульвар|провул|пер\.|площа|пл\.|набереж|шосе)"
    r"\s+[\w'’\-]{2,40}\s+\d{1,4}[а-яіїєґa-z]?"
    r")",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MatchResult:
    property: Property
    reason: str  # fingerprint | soft_address | phone | weak_unique


def has_building_number(address: str | None) -> bool:
    """True when address has a house/office/unit number — required for soft-match."""
    if not address:
        return False
    text = address.strip()
    if _BUILDING_NUM_RE.search(text):
        return True
    if re.search(r",\s*\d{1,4}[а-яіїєґa-z]?(?:\s|$)", text, re.IGNORECASE):
        return True
    norm = normalize_address(text)
    tokens = norm.split()
    for tok in tokens:
        if re.fullmatch(r"\d{1,4}[а-яіїєґa-z]?", tok):
            return True
    return False


def is_weak_location(address: str | None, area_sqm: float | None) -> bool:
    """True if identity is too thin to safely cross-source merge."""
    addr = normalize_address(address)
    if not addr or len(addr) < 8:
        return True
    if not _STREET_RE.search(addr):
        return True
    if area_sqm is None:
        return True
    return False


def soft_identity_key(
    *,
    address: str | None,
    area_sqm: float | None,
    floor: int | None,
    deal_type: str | None,
) -> tuple[str, float | None, int | None, str] | None:
    if is_weak_location(address, area_sqm):
        return None
    if not has_building_number(address):
        return None
    addr = normalize_address(address)
    area = round_area(area_sqm)
    return (addr, area, floor, (deal_type or "").lower())


def _usd_close(a: float | None, cur_a: str | None, b: float | None, cur_b: str | None) -> bool | None:
    """None = cannot compare; True/False = within soft band."""
    if a is None or b is None:
        return None
    ua = to_usd(float(a), cur_a)
    ub = to_usd(float(b), cur_b)
    if ua is None or ub is None or ua <= 0 or ub <= 0:
        return None
    lo, hi = (ua, ub) if ua <= ub else (ub, ua)
    return hi <= lo * 1.20  # ±~18% soft signal, not a hard key


def _same_source_sibling_exists(
    db: Session,
    property_id: int,
    *,
    source: str | None,
    external_id: str | None,
) -> bool:
    """Block merge when the property already has another card from the same portal."""
    if not source or not external_id:
        return False
    hit = db.scalar(
        select(Listing.id)
        .where(
            Listing.property_id == property_id,
            Listing.source == source,
            Listing.external_id != external_id,
        )
        .limit(1)
    )
    return hit is not None


def find_property_match(
    db: Session,
    data: FingerprintInput,
    *,
    source: str | None = None,
    external_id: str | None = None,
) -> MatchResult | None:
    """Exact fingerprint first, then soft address+area+floor, then phone."""
    fp = build_fingerprint(data)
    prop = db.scalar(select(Property).where(Property.fingerprint == fp))
    if prop is not None:
        return MatchResult(prop, "fingerprint")

    soft = soft_identity_key(
        address=data.address,
        area_sqm=data.area_sqm,
        floor=data.floor,
        deal_type=data.deal_type,
    )
    if soft is not None:
        addr, area, floor, deal = soft
        q = select(Property).where(
            Property.address_norm == addr,
            Property.deal_type == deal,
        )
        if floor is not None:
            q = q.where(Property.floor == floor)
        else:
            q = q.where(Property.floor.is_(None))
        candidates = list(db.scalars(q.limit(40)).all())
        area_f = float(area) if area is not None else None
        for c in candidates:
            if _same_source_sibling_exists(
                db, int(c.id), source=source, external_id=external_id
            ):
                continue
            if c.area_sqm is None or area_f is None:
                continue
            if abs(float(c.area_sqm) - area_f) > 2.0:
                continue
            sibling = db.scalar(
                select(Listing)
                .where(Listing.property_id == c.id, Listing.price.is_not(None))
                .order_by(Listing.last_seen_at.desc())
            )
            reason = "soft_address"
            if sibling and data.price is not None:
                price_ok = _usd_close(
                    data.price, data.currency, sibling.price, sibling.currency
                )
                if price_ok is True:
                    reason = "soft_address_price"
            return MatchResult(c, reason)

    phone = phone_digits(data.phone)
    if phone and is_weak_location(data.address, data.area_sqm):
        # Same phone + same deal, prefer linking rather than orphaning.
        lid = db.scalar(
            select(Listing.property_id)
            .where(
                Listing.phone.is_not(None),
                Listing.deal_type == (data.deal_type or ""),
                or_(
                    Listing.phone.endswith(phone[-9:]),
                    Listing.phone.contains(phone[-10:]),
                ),
                Listing.property_id.is_not(None),
            )
            .limit(1)
        )
        if lid is not None:
            prop = db.get(Property, int(lid))
            if prop is not None and not _same_source_sibling_exists(
                db, int(prop.id), source=source, external_id=external_id
            ):
                return MatchResult(prop, "phone")

    return None


def weak_unique_fingerprint(source: str, external_id: str, deal_type: str | None) -> str:
    """Isolate weak cards until detail/phone allows a real merge."""
    raw = f"weak|{(deal_type or '').lower()}|{source}|{external_id}"
    import hashlib

    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def describe_match_reasons(listings: list[Listing]) -> list[str]:
    """Human-readable why portals are considered the same object."""
    if len(listings) < 2:
        return []
    reasons: list[str] = []
    addrs = {normalize_address(x.address_raw) for x in listings if x.address_raw}
    addrs.discard("")
    areas = [round_area(x.area_sqm) for x in listings if x.area_sqm is not None]
    floors = {x.floor for x in listings if x.floor is not None}
    phones = {phone_digits(x.phone) for x in listings}
    phones.discard(None)
    if len(addrs) == 1:
        reasons.append("адрес")
    if areas and max(areas) - min(areas) <= 2.0:
        reasons.append("площадь")
    if len(floors) == 1:
        reasons.append("этаж")
    if len(phones) == 1 and phones:
        reasons.append("телефон")
    prices = []
    for x in listings:
        if x.price is not None:
            u = to_usd(float(x.price), x.currency)
            if u:
                prices.append(u)
    if len(prices) >= 2 and max(prices) <= min(prices) * 1.2:
        reasons.append("близкая цена")
    return reasons or ["общий fingerprint"]


def split_listing_to_own_property(db: Session, listing: Listing) -> Property:
    """Undo a bad soft-merge: one listing card → its own Property."""
    # Always isolate by portal card id — never re-collapse during repair.
    fingerprint = weak_unique_fingerprint(
        listing.source, listing.external_id, listing.deal_type
    )

    prop = db.scalar(select(Property).where(Property.fingerprint == fingerprint))
    if prop is None:
        prop = Property(
            fingerprint=fingerprint,
            title=listing.title,
            address_norm=normalize_address(listing.address_raw),
            district=listing.district,
            city=listing.city,
            property_type=listing.property_type,
            deal_type=listing.deal_type,
            area_sqm=listing.area_sqm,
            floor=listing.floor,
            lat=listing.lat,
            lon=listing.lon,
            first_seen_at=listing.first_seen_at,
            last_seen_at=listing.last_seen_at,
            is_active=listing.status in ("active", "relisted"),
        )
        db.add(prop)
        db.flush()
    else:
        prop.is_active = True
        prop.last_seen_at = listing.last_seen_at

    listing.property_id = prop.id
    extra = dict(listing.raw_extra or {})
    extra["match_reason"] = "repair_split"
    listing.raw_extra = extra or None
    return prop


def repair_overmerged_properties(db: Session, *, dry_run: bool = True) -> dict:
    """Split active listings wrongly glued to one Property on the same source."""
    active = ("active", "relisted")
    groups_q = (
        select(Listing.property_id, Listing.source, func.count())
        .where(Listing.property_id.is_not(None), Listing.status.in_(active))
        .group_by(Listing.property_id, Listing.source)
        .having(func.count() > 1)
    )
    groups = 0
    split_listings = 0
    touched_properties: set[int] = set()

    for property_id, source, _count in db.execute(groups_q):
        groups += 1
        touched_properties.add(int(property_id))
        listings = list(
            db.scalars(
                select(Listing)
                .where(
                    Listing.property_id == property_id,
                    Listing.source == source,
                    Listing.status.in_(active),
                )
                .order_by(Listing.first_seen_at.asc(), Listing.id.asc())
            ).all()
        )
        for lst in listings[1:]:
            split_listings += 1
            if not dry_run:
                split_listing_to_own_property(db, lst)

    if not dry_run:
        for pid in touched_properties:
            prop = db.get(Property, pid)
            if prop is None:
                continue
            active_n = (
                db.scalar(
                    select(func.count())
                    .select_from(Listing)
                    .where(
                        Listing.property_id == pid,
                        Listing.status.in_(active),
                    )
                )
                or 0
            )
            prop.is_active = active_n > 0
        db.commit()

    return {
        "groups": groups,
        "split_listings": split_listings,
        "properties_touched": len(touched_properties),
        "dry_run": dry_run,
    }


def merge_properties(db: Session, keep_id: int, drop_id: int) -> int:
    """Move listings/events/hypotheses from drop → keep. Returns moved listings count."""
    if keep_id == drop_id:
        return 0
    keep = db.get(Property, keep_id)
    drop = db.get(Property, drop_id)
    if not keep or not drop:
        return 0
    moved = 0
    for lst in list(db.scalars(select(Listing).where(Listing.property_id == drop_id))):
        lst.property_id = keep_id
        moved += 1
    from app.db.models import DealHypothesis, PropertyEvent

    for ev in list(db.scalars(select(PropertyEvent).where(PropertyEvent.property_id == drop_id))):
        ev.property_id = keep_id
    for hyp in list(
        db.scalars(select(DealHypothesis).where(DealHypothesis.property_id == drop_id))
    ):
        hyp.property_id = keep_id
    db.delete(drop)
    keep.is_active = (
        db.scalar(
            select(func.count())
            .select_from(Listing)
            .where(
                Listing.property_id == keep_id,
                Listing.status.in_(["active", "relisted"]),
            )
        )
        or 0
    ) > 0
    return moved
