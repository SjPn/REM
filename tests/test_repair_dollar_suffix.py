from datetime import datetime, timezone

from sqlalchemy import select

from app.db.models import Listing, ListingSnapshot, PropertyEvent
from app.domain.enums import EventType
from app.domain.repair_dollar_suffix import (
    find_dollar_suffix_repairs,
    repair_dollar_suffix_prices,
)


def _init(tmp_path, monkeypatch, name="suf.db"):
    db_path = tmp_path / name
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    from app.config import get_settings
    from app.db import models as db_models
    from app.db.models import get_session_factory, init_db

    get_settings.cache_clear()
    db_models._engine = None
    db_models._SessionLocal = None
    init_db()
    return get_session_factory()


def test_repair_dollar_suffix_sale(tmp_path, monkeypatch):
    SessionLocal = _init(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        bad = Listing(
            source="m2bomber",
            external_id="1402687927",
            url="https://ua.m2bomber.com/obj/1402687927/view/x",
            deal_type="sale",
            title="Продаж офісу 163.1 кв. м",
            description=(
                "223 000 $ ₴ $ € 1 368 $ за м² "
                "зміна ціни 4 жовт. 2026 - 223 000 $ 19 вер. 2026 - 225 000 $"
            ),
            price=19.0,
            currency="USD",
            area_sqm=163.0,
            price_per_sqm=0.1166,
            price_drop_count=1,
            exclude_from_stats=True,
            status="active",
            raw_extra={"price_suspicious": True, "stats_auto_excluded": True},
        )
        ok = Listing(
            source="m2bomber",
            external_id="ok",
            url="https://ua.m2bomber.com/obj/2/view/x",
            deal_type="sale",
            title="Продаж 250 000 $ 90 м²",
            price=250_000.0,
            currency="USD",
            area_sqm=90.0,
            status="active",
        )
        db.add_all([bad, ok])
        db.commit()
        db.add(
            ListingSnapshot(
                listing_id=bad.id,
                crawled_at=now,
                price=19.0,
                currency="USD",
                status="active",
            )
        )
        db.add(
            PropertyEvent(
                listing_id=bad.id,
                event_type=EventType.PRICE_CHANGED.value,
                occurred_at=now,
                payload={"old_price": 223000.0, "new_price": 19.0, "currency": "USD"},
            )
        )
        db.commit()

        found = find_dollar_suffix_repairs(db)
        ids = {item["listing_id"] for item in found}
        assert bad.id in ids
        assert ok.id not in ids

        summary = repair_dollar_suffix_prices(db, dry_run=False)
        assert summary["count"] == 1
        assert summary["events_removed"] == 1
        db.refresh(bad)
        assert bad.price == 223_000.0
        assert abs(float(bad.price_per_sqm) - 1368.0) < 1
        assert bad.exclude_from_stats is False
        assert bad.price_drop_count == 0
        assert (bad.raw_extra or {}).get("price_repair") == "dollar_suffix"
        snap = db.scalars(
            select(ListingSnapshot).where(ListingSnapshot.listing_id == bad.id)
        ).first()
        assert snap.price == 223_000.0
        left = db.scalars(
            select(PropertyEvent).where(PropertyEvent.listing_id == bad.id)
        ).all()
        assert left == []
