from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Listing, ListingSnapshot, Property, PropertyEvent, utcnow
from app.domain.enums import EventType, ListingStatus
from app.domain.fingerprint import FingerprintInput, build_fingerprint, normalize_address
from app.domain.property_match import (
    find_property_match,
    is_weak_location,
    weak_unique_fingerprint,
)
from app.domain.segments import classify_segment
from app.domain.signals import detect_opex, parse_cap_and_noi
from app.domain.list_card import list_card_changed
from app.domain.listing_stats import apply_auto_stats_exclusion, set_stats_exclusion
from app.domain.pricing import normalize_listing_price, sanitize_price_per_sqm
from app.scrapers.http_utils import strip_leading_price_junk
from app.scrapers.base import RawListing

logger = logging.getLogger(__name__)


def _listing_status_from_source(raw_status: str | None) -> str:
    if not raw_status:
        return ListingStatus.ACTIVE.value
    s = raw_status.lower()
    if any(x in s for x in ("sold", "продано")):
        return ListingStatus.SOLD_MARKED.value
    if any(x in s for x in ("rented", "здано", "арендовано", "орендовано")):
        return ListingStatus.RENTED_MARKED.value
    if any(x in s for x in ("inactive", "404", "unavailable", "архів", "архив")):
        return ListingStatus.VANISHED.value
    return ListingStatus.ACTIVE.value


def _aware(dt: datetime | None) -> datetime:
    if dt is None:
        return utcnow()
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _merge_finance_signals(raw: RawListing) -> dict:
    extra = dict(raw.extra or {})
    text = " ".join(x for x in (raw.title, raw.description) if x)
    found = parse_cap_and_noi(text)
    # Only keep explicitly parsed fields; never invent.
    for key, val in found.items():
        extra[key] = val
    if (raw.deal_type or "").lower() == "rent":
        extra["opex"] = detect_opex(raw.title, raw.description)
    return extra


def upsert_listing(
    db: Session, raw: RawListing, seen_at: datetime | None = None
) -> tuple[Listing | None, bool]:
    decision = classify_segment(
        title=raw.title,
        description=raw.description,
        property_type=raw.property_type,
        floor=raw.floor,
        address=raw.address_raw,
        url=raw.url,
    )
    if not decision.relevant:
        logger.debug(
            "skip irrelevant %s/%s segment=%s reason=%s",
            raw.source,
            raw.external_id,
            decision.segment,
            decision.reason,
        )
        return None, False

    # Normalize to product segment taxonomy
    if decision.segment in {
        "office",
        "retail",
        "showroom",
        "business_center",
        "street_retail",
        "building",
        "free_purpose",
    }:
        raw.property_type = decision.segment

    # Drop nonsensical prices from polluted parsers
    if raw.price is not None:
        try:
            import math

            if not math.isfinite(float(raw.price)) or float(raw.price) <= 0 or float(raw.price) > 500_000_000:
                raw.price = None
        except (TypeError, ValueError):
            raw.price = None

    # If implied $/m² is absurdly low, the "price" was likely already $/m²
    norm = normalize_listing_price(
        price=raw.price,
        currency=raw.currency,
        area_sqm=raw.area_sqm,
        deal_type=raw.deal_type,
        price_per_sqm=raw.price_per_sqm,
        title=raw.title,
        description=raw.description,
    )
    raw.price = norm.price
    raw.currency = norm.currency or raw.currency
    raw.price_per_sqm = sanitize_price_per_sqm(
        price=raw.price,
        currency=raw.currency,
        area_sqm=raw.area_sqm,
        deal_type=raw.deal_type,
        price_per_sqm=norm.price_per_sqm,
    )
    if raw.source == "rieltor" and raw.title:
        raw.title = strip_leading_price_junk(raw.title) or raw.title
    if norm.reinterpreted_as_psm:
        finance_extra_hint = {"price_was_psm": True, "price_norm": norm.detail}
    else:
        finance_extra_hint = {}

    now = _aware(seen_at)
    scrape_extra = dict(raw.extra or {})
    finance_extra = _merge_finance_signals(raw)
    if finance_extra_hint:
        finance_extra = {**finance_extra, **finance_extra_hint}
    # Keep scraper metadata (seller_type, isBusiness, …) + finance signals.
    finance_extra = {**scrape_extra, **finance_extra}
    raw.extra = finance_extra
    listing = db.scalar(
        select(Listing).where(
            Listing.source == raw.source,
            Listing.external_id == raw.external_id,
        )
    )

    fp_input = FingerprintInput(
        address=raw.address_raw,
        area_sqm=raw.area_sqm,
        floor=raw.floor,
        price=raw.price,
        currency=raw.currency,
        property_type=raw.property_type,
        deal_type=raw.deal_type,
        lat=raw.lat,
        lon=raw.lon,
        phone=raw.phone,
    )
    match = find_property_match(
        db, fp_input, source=raw.source, external_id=raw.external_id
    )
    match_reason = match.reason if match else None
    if match is not None:
        prop = match.property
        prop.last_seen_at = now
        prop.is_active = True
        if raw.title and not prop.title:
            prop.title = raw.title
        if raw.district and not prop.district:
            prop.district = raw.district
        if raw.address_raw and (
            not prop.address_norm or prop.fingerprint.startswith("weak")
        ):
            # Upgrade weak orphan identity when we finally get a street address.
            if not is_weak_location(raw.address_raw, raw.area_sqm):
                new_fp = build_fingerprint(fp_input)
                existing = db.scalar(select(Property).where(Property.fingerprint == new_fp))
                if existing is None or existing.id == prop.id:
                    prop.fingerprint = new_fp
                    prop.address_norm = normalize_address(raw.address_raw)
                    prop.area_sqm = raw.area_sqm if raw.area_sqm is not None else prop.area_sqm
                    prop.floor = raw.floor if raw.floor is not None else prop.floor
                    match_reason = "upgraded_from_weak"
    else:
        weak = is_weak_location(raw.address_raw, raw.area_sqm)
        fingerprint = (
            weak_unique_fingerprint(raw.source, raw.external_id, raw.deal_type)
            if weak
            else build_fingerprint(fp_input)
        )
        # Race: exact fp may appear after soft-miss
        prop = db.scalar(select(Property).where(Property.fingerprint == fingerprint))
        if prop is None:
            prop = Property(
                fingerprint=fingerprint,
                title=raw.title,
                address_norm=normalize_address(raw.address_raw),
                district=raw.district,
                city=raw.city or "Київ",
                property_type=raw.property_type,
                deal_type=raw.deal_type,
                area_sqm=raw.area_sqm,
                floor=raw.floor,
                lat=raw.lat,
                lon=raw.lon,
                first_seen_at=now,
                last_seen_at=now,
                is_active=True,
            )
            db.add(prop)
            db.flush()
            db.add(
                PropertyEvent(
                    property_id=prop.id,
                    event_type=EventType.APPEARED.value,
                    occurred_at=now,
                    payload={
                        "source": raw.source,
                        "external_id": raw.external_id,
                        "weak_identity": weak,
                    },
                )
            )
            match_reason = "weak_unique" if weak else "fingerprint_new"
        else:
            prop.last_seen_at = now
            prop.is_active = True
            match_reason = "fingerprint"

    write_snapshot = True
    if listing is None:
        listing = Listing(
            property_id=prop.id,
            source=raw.source,
            external_id=raw.external_id,
            url=raw.url,
            title=raw.title,
            description=raw.description,
            deal_type=raw.deal_type,
            property_type=raw.property_type,
            price=raw.price,
            currency=raw.currency,
            price_per_sqm=raw.price_per_sqm,
            area_sqm=raw.area_sqm,
            floor=raw.floor,
            rooms=raw.rooms,
            address_raw=raw.address_raw,
            district=raw.district,
            city=raw.city,
            lat=raw.lat,
            lon=raw.lon,
            phone=raw.phone,
            agency=raw.agency,
            status=_listing_status_from_source(raw.source_status_raw),
            source_status_raw=raw.source_status_raw,
            first_seen_at=now,
            last_seen_at=now,
            raw_extra=finance_extra or None,
        )
        db.add(listing)
        db.flush()
        db.add(
            PropertyEvent(
                property_id=prop.id,
                listing_id=listing.id,
                event_type=EventType.APPEARED.value,
                occurred_at=now,
                payload={"source": raw.source, "url": raw.url},
            )
        )
    else:
        write_snapshot = list_card_changed(listing, raw)
        if raw.description and raw.description.strip() != (listing.description or "").strip():
            write_snapshot = True
        if raw.phone and not listing.phone:
            write_snapshot = True
        if listing.status == ListingStatus.VANISHED.value:
            write_snapshot = True
        # Relist detection
        if listing.status == ListingStatus.VANISHED.value:
            listing.status = ListingStatus.RELISTED.value
            db.add(
                PropertyEvent(
                    property_id=prop.id,
                    listing_id=listing.id,
                    event_type=EventType.RELISTED.value,
                    occurred_at=now,
                    payload={"previous_vanished_at": listing.vanished_at.isoformat() if listing.vanished_at else None},
                )
            )
            listing.vanished_at = None
            # Hypotheses for this property are no longer "deals".
            from app.db.models import DealHypothesis
            from app.domain.enums import DealBucket

            for hyp in db.scalars(
                select(DealHypothesis).where(
                    DealHypothesis.property_id == prop.id,
                    DealHypothesis.human_label.is_(None),
                )
            ):
                hyp.bucket = DealBucket.LIKELY_WITHDRAWN.value
                hyp.score = min(int(hyp.score or 0), 35)
                feats = dict(hyp.features or {}) if isinstance(hyp.features, dict) else {}
                feats["relisted_override"] = True
                hyp.features = feats

        if (
            listing.price is not None
            and raw.price is not None
            and float(listing.price) != float(raw.price)
        ):
            if float(raw.price) < float(listing.price):
                listing.price_drop_count = (listing.price_drop_count or 0) + 1
            db.add(
                PropertyEvent(
                    property_id=prop.id,
                    listing_id=listing.id,
                    event_type=EventType.PRICE_CHANGED.value,
                    occurred_at=now,
                    payload={
                        "old_price": listing.price,
                        "new_price": raw.price,
                        "currency": raw.currency or listing.currency,
                    },
                )
            )

        listing.property_id = prop.id
        listing.url = raw.url
        listing.title = raw.title or listing.title
        listing.description = raw.description or listing.description
        listing.deal_type = raw.deal_type
        listing.property_type = raw.property_type or listing.property_type
        listing.price = raw.price if raw.price is not None else listing.price
        listing.currency = raw.currency or listing.currency
        listing.price_per_sqm = raw.price_per_sqm or listing.price_per_sqm
        listing.area_sqm = raw.area_sqm if raw.area_sqm is not None else listing.area_sqm
        listing.floor = raw.floor if raw.floor is not None else listing.floor
        listing.address_raw = raw.address_raw or listing.address_raw
        listing.district = raw.district or listing.district
        listing.city = raw.city or listing.city
        listing.lat = raw.lat if raw.lat is not None else listing.lat
        listing.lon = raw.lon if raw.lon is not None else listing.lon
        listing.phone = raw.phone or listing.phone
        listing.agency = raw.agency or listing.agency
        listing.source_status_raw = raw.source_status_raw or listing.source_status_raw
        new_status = _listing_status_from_source(listing.source_status_raw)
        if new_status != ListingStatus.ACTIVE.value:
            if listing.status != new_status:
                db.add(
                    PropertyEvent(
                        property_id=prop.id,
                        listing_id=listing.id,
                        event_type=EventType.STATUS_CHANGED.value,
                        occurred_at=now,
                        payload={
                            "old_status": listing.status,
                            "new_status": new_status,
                            "source_status_raw": listing.source_status_raw,
                        },
                    )
                )
            listing.status = new_status
            if new_status == ListingStatus.VANISHED.value and not listing.vanished_at:
                listing.vanished_at = now
        else:
            listing.status = ListingStatus.ACTIVE.value
        listing.last_seen_at = now
        listing.raw_extra = {**(listing.raw_extra or {}), **finance_extra} or listing.raw_extra

    from app.domain.pricing import effective_listing_psm_usd, psm_suspicious

    extra = dict(listing.raw_extra or {})
    if match_reason:
        extra["match_reason"] = match_reason
    psm_usd = effective_listing_psm_usd(
        listing.price,
        listing.currency,
        listing.area_sqm,
        deal_type=listing.deal_type,
        price_per_sqm=listing.price_per_sqm,
    )
    suspicious = psm_suspicious(listing.deal_type, psm_usd)
    if suspicious:
        extra["price_suspicious"] = True
    else:
        extra.pop("price_suspicious", None)
    listing.raw_extra = extra or None
    apply_auto_stats_exclusion(listing, suspicious=suspicious)

    if write_snapshot:
        db.add(
            ListingSnapshot(
                listing_id=listing.id,
                crawled_at=now,
                price=listing.price,
                currency=listing.currency,
                status=listing.status,
                title=listing.title,
                area_sqm=listing.area_sqm,
                payload=raw.to_dict(),
            )
        )
    return listing, write_snapshot


def ingest_many(db: Session, items: list[RawListing]) -> dict[str, int]:
    seen_ids: set[tuple[str, str]] = set()
    upserted_external_ids: set[str] = set()
    created_or_updated = 0
    skipped_irrelevant = 0
    snapshots_skipped = 0
    for raw in items:
        key = (raw.source, raw.external_id)
        if key in seen_ids:
            continue
        seen_ids.add(key)
        listing, wrote_snapshot = upsert_listing(db, raw)
        if listing is None:
            skipped_irrelevant += 1
            continue
        created_or_updated += 1
        upserted_external_ids.add(raw.external_id)
        if not wrote_snapshot:
            snapshots_skipped += 1
    db.commit()
    return {
        "upserted": created_or_updated,
        "unique_in_batch": len(seen_ids),
        "skipped_irrelevant": skipped_irrelevant,
        "snapshots_skipped": snapshots_skipped,
        "upserted_external_ids": upserted_external_ids,
    }
