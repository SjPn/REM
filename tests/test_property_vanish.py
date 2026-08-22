from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from app.db.models import Listing, Property, PropertyEvent
from app.domain.enums import EventType, ListingStatus
from app.pipeline.reconcile import mark_vanished, reconcile_property_vanish


def _init_db(tmp_path, monkeypatch, db_name: str = "pv.db"):
    db_path = tmp_path / db_name
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    from app.config import get_settings
    from app.db import models as db_models
    from app.db.models import get_session_factory, init_db

    get_settings.cache_clear()
    db_models._engine = None
    db_models._SessionLocal = None
    init_db()
    return get_session_factory()


def test_property_vanish_waits_for_all_sources(tmp_path, monkeypatch):
    SessionLocal = _init_db(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    old_seen = now - timedelta(hours=12)

    with SessionLocal() as db:
        prop = Property(fingerprint="fp-multi", deal_type="sale", property_type="office")
        db.add(prop)
        db.flush()

        lun = Listing(
            source="lun",
            external_id="l1",
            url="https://e/l1",
            deal_type="sale",
            property_id=prop.id,
            status=ListingStatus.ACTIVE.value,
            first_seen_at=now - timedelta(days=5),
            last_seen_at=old_seen,
            price=100_000,
            currency="USD",
        )
        dom = Listing(
            source="domria",
            external_id="d1",
            url="https://e/d1",
            deal_type="sale",
            property_id=prop.id,
            status=ListingStatus.ACTIVE.value,
            first_seen_at=now - timedelta(days=5),
            last_seen_at=now,
            price=100_000,
            currency="USD",
        )
        db.add_all([lun, dom])
        db.commit()

        marked = mark_vanished(db, "lun", seen_external_ids=set(), grace_hours=6)
        assert marked == 1
        db.refresh(lun)
        db.refresh(dom)
        assert lun.status == ListingStatus.VANISHED.value
        assert dom.status == ListingStatus.ACTIVE.value

        cnt = db.scalar(
            select(func.count())
            .select_from(PropertyEvent)
            .where(
                PropertyEvent.property_id == prop.id,
                PropertyEvent.event_type == EventType.VANISHED.value,
            )
        )
        assert cnt == 0


def test_property_vanish_emits_once_when_fully_gone(tmp_path, monkeypatch):
    SessionLocal = _init_db(tmp_path, monkeypatch, "pv2.db")
    now = datetime.now(timezone.utc)
    old_seen = now - timedelta(hours=12)

    with SessionLocal() as db:
        prop = Property(fingerprint="fp-gone", deal_type="rent", property_type="office")
        db.add(prop)
        db.flush()

        lun = Listing(
            source="lun",
            external_id="l2",
            url="https://e/l2",
            deal_type="rent",
            property_id=prop.id,
            status=ListingStatus.VANISHED.value,
            first_seen_at=now - timedelta(days=5),
            last_seen_at=old_seen,
            vanished_at=now - timedelta(hours=1),
            price=2200,
            currency="USD",
        )
        dom = Listing(
            source="domria",
            external_id="d2",
            url="https://e/d2",
            deal_type="rent",
            property_id=prop.id,
            status=ListingStatus.ACTIVE.value,
            first_seen_at=now - timedelta(days=5),
            last_seen_at=old_seen,
            price=2200,
            currency="USD",
        )
        db.add_all([lun, dom])
        db.commit()

        marked = mark_vanished(db, "domria", seen_external_ids=set(), grace_hours=6)
        assert marked == 1

        cnt = db.scalar(
            select(func.count())
            .select_from(PropertyEvent)
            .where(
                PropertyEvent.property_id == prop.id,
                PropertyEvent.event_type == EventType.VANISHED.value,
            )
        )
        assert cnt == 1
        event = db.scalar(
            select(PropertyEvent).where(
                PropertyEvent.property_id == prop.id,
                PropertyEvent.event_type == EventType.VANISHED.value,
            )
        )
        assert event is not None
        assert event.payload.get("level") == "property"

        again = reconcile_property_vanish(db, prop.id, now=now, trigger_listing=dom)
        assert again is False
        cnt2 = db.scalar(
            select(func.count())
            .select_from(PropertyEvent)
            .where(
                PropertyEvent.property_id == prop.id,
                PropertyEvent.event_type == EventType.VANISHED.value,
            )
        )
        assert cnt2 == 1
