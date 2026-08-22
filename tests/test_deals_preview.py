from __future__ import annotations

from datetime import datetime, timezone

from app.db.models import DealHypothesis, Listing, Property
from app.domain.deals_preview import deal_bucket_counts, recent_deal_hypotheses


def test_recent_deal_hypotheses_filters_deal_type(tmp_path, monkeypatch):
    db_path = tmp_path / "dp.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    from app.config import get_settings
    from app.db import models as db_models
    from app.db.models import get_session_factory, init_db

    get_settings.cache_clear()
    db_models._engine = None
    db_models._SessionLocal = None
    init_db()
    now = datetime.now(timezone.utc)
    with get_session_factory()() as db:
        sale_lst = Listing(
            source="t",
            external_id="s1",
            url="https://e/s1",
            deal_type="sale",
            status="vanished",
            price=100_000,
            currency="USD",
            area_sqm=50,
            city="Київ",
            first_seen_at=now,
            last_seen_at=now,
        )
        rent_lst = Listing(
            source="t",
            external_id="r1",
            url="https://e/r1",
            deal_type="rent",
            status="vanished",
            price=2000,
            currency="USD",
            area_sqm=80,
            city="Київ",
            first_seen_at=now,
            last_seen_at=now,
        )
        db.add_all([sale_lst, rent_lst])
        db.flush()
        sale_prop = Property(
            fingerprint="fp-sale",
            title="Sale",
            address_norm="addr",
            deal_type="sale",
            is_active=False,
        )
        rent_prop = Property(
            fingerprint="fp-rent",
            title="Rent",
            address_norm="addr2",
            deal_type="rent",
            is_active=False,
        )
        db.add_all([sale_prop, rent_prop])
        db.flush()
        db.add_all(
            [
                DealHypothesis(
                    property_id=sale_prop.id,
                    listing_id=sale_lst.id,
                    score=80,
                    bucket="likely_deal",
                    features={"features": []},
                    created_at=now,
                ),
                DealHypothesis(
                    property_id=rent_prop.id,
                    listing_id=rent_lst.id,
                    score=70,
                    bucket="likely_deal",
                    features={"features": []},
                    created_at=now,
                ),
            ]
        )
        db.commit()
        sale_only = recent_deal_hypotheses(db, deal_type="sale", hours=0, limit=10)
        assert len(sale_only) == 1
        assert sale_only[0].listing_id == sale_lst.id
        counts = deal_bucket_counts(db, deal_type="sale", hours=0)
        assert counts["likely_deal"] == 1
        assert counts["all"] == 1
