from datetime import datetime, timezone

from app.db.models import Listing
from app.domain.repair_glued_prices import (
    find_glued_house_price_repairs,
    repair_glued_house_prices,
)


def _init(tmp_path, monkeypatch, name="glue.db"):
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


def test_repair_rent_glued_to_nearby_house(tmp_path, monkeypatch):
    SessionLocal = _init(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        bad = Listing(
            source="m2bomber",
            external_id="1403992387",
            url="https://ua.m2bomber.com/obj/1403992387/view/x",
            deal_type="rent",
            title="Оренда офісу на вул. Саксаганського 70А",
            description=(
                "1 093 675 ₴ 970 м² "
                "ЖК Podil ул. Глубочицкая, 73-79 100 900 ₴"
            ),
            price=32_636_999.0,
            currency="UAH",
            area_sqm=970.0,
            price_per_sqm=33646.0,
            exclude_from_stats=True,
            status="active",
            first_seen_at=now,
            last_seen_at=now,
            raw_extra={"price_suspicious": True, "stats_auto_excluded": True},
        )
        db.add(bad)
        db.commit()
        found = find_glued_house_price_repairs(db)
        assert [item["listing_id"] for item in found] == [bad.id]
        summary = repair_glued_house_prices(db, dry_run=False)
        assert summary["count"] == 1
        db.refresh(bad)
        assert bad.price == 1_093_675.0
        assert bad.currency == "UAH"
        assert bad.exclude_from_stats is False


def test_keeps_price_that_is_still_in_the_text(tmp_path, monkeypatch):
    SessionLocal = _init(tmp_path, monkeypatch, "glue2.db")
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        row = Listing(
            source="rieltor",
            external_id="keep",
            url="https://rieltor.ua/x",
            deal_type="sale",
            title="Офіс 550 м²",
            description="9 600 000 $ - БЕЗ КОМІСІЇ - 1100 м² і ще 600 000 $",
            price=9_600_000.0,
            currency="USD",
            area_sqm=550.0,
            status="active",
            first_seen_at=now,
            last_seen_at=now,
        )
        db.add(row)
        db.commit()
        assert find_glued_house_price_repairs(db) == []
