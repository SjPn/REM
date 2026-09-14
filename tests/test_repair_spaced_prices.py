from app.db.models import Listing
from app.domain.repair_spaced_prices import find_spaced_price_repairs, repair_spaced_price_bugs


def _init(tmp_path, monkeypatch, name="sp.db"):
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


def test_repair_spaced_rent_price(tmp_path, monkeypatch):
    SessionLocal = _init(tmp_path, monkeypatch)
    with SessionLocal() as db:
        bad = Listing(
            source="rieltor",
            external_id="t1",
            url="https://rieltor.ua/commercials-rent/view/1/",
            deal_type="rent",
            title="Саксаганського 70/16 5 300 $/міс 20 $/м² офіс",
            price=300.0,
            currency="USD",
            area_sqm=265.0,
            price_per_sqm=1.13,
            status="active",
        )
        # sale chip that must NOT be rewritten to tiny "total"
        ok_sale = Listing(
            source="m2bomber",
            external_id="t2",
            url="https://example/sale/2",
            deal_type="sale",
            title="Продаж 3 188 $/м² площа 80",
            price=188.0,
            currency="USD",
            area_sqm=80.0,
            status="active",
        )
        db.add_all([bad, ok_sale])
        db.commit()

        found = find_spaced_price_repairs(db)
        ids = {f["listing_id"] for f in found}
        assert bad.id in ids
        assert ok_sale.id not in ids

        summary = repair_spaced_price_bugs(db, dry_run=False)
        assert summary["count"] == 1
        db.refresh(bad)
        assert bad.price == 5300.0
        assert abs(float(bad.price_per_sqm) - 20.0) < 0.01
