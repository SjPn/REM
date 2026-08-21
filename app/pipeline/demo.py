from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import DealHypothesis, Listing, Property, PropertyEvent, utcnow
from app.domain.enums import ListingStatus
from app.scrapers.base import RawListing
from app.pipeline.ingest import ingest_many
from app.pipeline.reconcile import create_or_update_deal_hypothesis, mark_vanished


def seed_demo_dataset(db: Session) -> dict:
    """Deterministic demo data so UI/pipeline work without live portal access."""
    now = utcnow()
    base = [
        RawListing(
            source="lun",
            external_id="100001",
            url="https://lun.ua/realty/100001",
            deal_type="rent",
            title="Офіс 120 м² Шевченківський",
            property_type="office",
            price=2500,
            currency="USD",
            area_sqm=120,
            floor=4,
            address_raw="вул. Богдана Хмельницького, 16",
            district="Шевченківський",
            city="Київ",
            agency="Demo Agency A",
        ),
        RawListing(
            source="olx",
            external_id="200001",
            url="https://www.olx.ua/d/uk/obyavlenie/office-ID200001.html",
            deal_type="rent",
            title="Оренда офісу 120 м² центр",
            property_type="office",
            price=2400,
            currency="USD",
            area_sqm=120,
            floor=4,
            address_raw="вул. Богдана Хмельницького, 16",
            district="Шевченківський",
            city="Київ",
            agency="Demo Agency A",
        ),
        RawListing(
            source="domria",
            external_id="300001",
            url="https://dom.ria.com/uk/realty-300001.html",
            deal_type="sale",
            title="Продаж торгового приміщення 85 м² Поділ",
            property_type="retail",
            price=185000,
            currency="USD",
            area_sqm=85,
            floor=1,
            address_raw="вул. Сагайдачного, 25",
            district="Подільський",
            city="Київ",
            agency="Demo Agency B",
        ),
            RawListing(
            source="rieltor",
            external_id="400001",
            url="https://rieltor.ua/kyiv/commerce-400001/",
            deal_type="rent",
            title="Офіс 180 м² в БЦ Святошин",
            property_type="office",
            price=180000,
            currency="UAH",
            area_sqm=180,
            floor=3,
            address_raw="вул. Кільцева, 12",
            district="Святошинський",
            city="Київ",
            agency="Demo Agency C",
        ),
        RawListing(
            source="lun",
            external_id="100002",
            url="https://lun.ua/realty/100002",
            deal_type="sale",
            title="Free purpose 45 м² Оболонь",
            property_type="free_purpose",
            price=95000,
            currency="USD",
            area_sqm=45,
            floor=1,
            address_raw="просп. Оболонський, 21",
            district="Оболонський",
            city="Київ",
        ),
    ]
    stats = ingest_many(db, base)

    # Simulate price drop then vanish on multi-source office → likely deal
    office_olx = db.scalar(
        select(Listing).where(Listing.source == "olx", Listing.external_id == "200001")
    )
    office_lun = db.scalar(
        select(Listing).where(Listing.source == "lun", Listing.external_id == "100001")
    )
    if office_olx:
        office_olx.price = 2200
        office_olx.price_drop_count = 1
        office_olx.first_seen_at = now - timedelta(days=40)
        office_olx.last_seen_at = now - timedelta(days=2)
    if office_lun:
        office_lun.first_seen_at = now - timedelta(days=40)
        office_lun.last_seen_at = now - timedelta(days=2)
        office_lun.price = 2300
        office_lun.price_drop_count = 1

    # Force vanish both office listings
    if office_olx and office_lun:
        for lst in (office_olx, office_lun):
            lst.status = ListingStatus.VANISHED.value
            lst.vanished_at = now
            db.add(
                PropertyEvent(
                    property_id=lst.property_id,
                    listing_id=lst.id,
                    event_type="vanished",
                    occurred_at=now,
                    payload={"demo": True},
                )
            )
            create_or_update_deal_hypothesis(db, lst)

    # Single-source fast vanish → likely withdrawn
    warehouse = db.scalar(
        select(Listing).where(Listing.source == "rieltor", Listing.external_id == "400001")
    )
    if warehouse:
        warehouse.first_seen_at = now - timedelta(days=1)
        warehouse.last_seen_at = now - timedelta(hours=20)
        warehouse.status = ListingStatus.VANISHED.value
        warehouse.vanished_at = now
        create_or_update_deal_hypothesis(db, warehouse)

    db.commit()

    props = db.scalar(select(func.count()).select_from(Property)) or 0
    listings = db.scalar(select(func.count()).select_from(Listing)) or 0
    hyps = db.scalar(select(func.count()).select_from(DealHypothesis)) or 0
    return {
        **stats,
        "properties": props,
        "listings": listings,
        "deal_hypotheses": hyps,
    }
