from __future__ import annotations

from datetime import datetime, timezone

from app.domain.fingerprint import FingerprintInput
from app.domain.property_match import (
    find_property_match,
    has_building_number,
    repair_overmerged_properties,
    soft_identity_key,
)


def test_has_building_number():
    assert has_building_number("вул. Хрещатик, 12") is True
    assert has_building_number("вул. Богдана Хмельницького 16") is True
    assert has_building_number("Київ вул. Хрещатик, бізнес-центр") is False
    assert has_building_number("Печерський район") is False


def test_soft_identity_requires_building_number():
    assert (
        soft_identity_key(
            address="Київ вул. Хрещатик, бізнес-центр",
            area_sqm=14.0,
            floor=None,
            deal_type="sale",
        )
        is None
    )
    assert (
        soft_identity_key(
            address="вул. Хрещатик, 12",
            area_sqm=14.0,
            floor=None,
            deal_type="sale",
        )
        is not None
    )


def test_soft_match_blocks_same_source_sibling(tmp_path, monkeypatch):
    db_path = tmp_path / "soft_src.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    from app.config import get_settings
    from app.db import models as db_models
    from app.db.models import Listing, Property, get_session_factory, init_db

    get_settings.cache_clear()
    db_models._engine = None
    db_models._SessionLocal = None
    init_db()
    now = datetime.now(timezone.utc)
    with get_session_factory()() as db:
        prop = Property(
            fingerprint="fp-soft-src",
            address_norm="хрещатик 12",
            deal_type="sale",
            area_sqm=14.0,
            first_seen_at=now,
            last_seen_at=now,
            is_active=True,
        )
        db.add(prop)
        db.flush()
        db.add(
            Listing(
                property_id=prop.id,
                source="lun",
                external_id="lun-a",
                url="https://example.test/a",
                deal_type="sale",
                status="active",
                address_raw="вул. Хрещатик, 12",
                area_sqm=14.0,
            )
        )
        db.commit()

        match = find_property_match(
            db,
            FingerprintInput(
                address="вул. Хрещатик, 12",
                area_sqm=14.2,
                floor=None,
                deal_type="sale",
                price=32000,
                currency="USD",
            ),
            source="lun",
            external_id="lun-b",
        )
        assert match is None


def test_repair_overmerged_splits_same_source(tmp_path, monkeypatch):
    db_path = tmp_path / "repair.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    from app.config import get_settings
    from app.db import models as db_models
    from app.db.models import Listing, Property, get_session_factory, init_db
    from sqlalchemy import func, select

    get_settings.cache_clear()
    db_models._engine = None
    db_models._SessionLocal = None
    init_db()
    now = datetime.now(timezone.utc)
    with get_session_factory()() as db:
        prop = Property(
            fingerprint="fp-bloat",
            deal_type="sale",
            area_sqm=14.0,
            first_seen_at=now,
            last_seen_at=now,
            is_active=True,
        )
        db.add(prop)
        db.flush()
        for i in range(3):
            db.add(
                Listing(
                    property_id=prop.id,
                    source="lun",
                    external_id=f"lun-{i}",
                    url=f"https://example.test/{i}",
                    deal_type="sale",
                    status="active",
                    area_sqm=14.0 + i,
                    first_seen_at=now,
                )
            )
        db.commit()

        dry = repair_overmerged_properties(db, dry_run=True)
        assert dry["groups"] == 1
        assert dry["split_listings"] == 2

        applied = repair_overmerged_properties(db, dry_run=False)
        assert applied["split_listings"] == 2

        multi = db.scalar(
            select(func.count())
            .select_from(Listing)
            .where(Listing.property_id == prop.id, Listing.source == "lun")
        )
        assert multi == 1
        props = db.scalar(select(func.count()).select_from(Property)) or 0
        assert props == 3
