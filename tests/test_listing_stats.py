from __future__ import annotations

from datetime import datetime, timezone

from app.db.models import Listing
from app.domain.listing_stats import (
    apply_auto_stats_exclusion,
    is_excluded_from_stats,
    set_stats_exclusion,
)
from app.domain.market_stats import compute_market_stats


def test_auto_exclude_and_user_clear(tmp_path, monkeypatch):
    db_path = tmp_path / "ex.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    from app.config import get_settings
    from app.db import models as db_models
    from app.db.models import get_session_factory, init_db
    from app.domain.ttl_cache import cache_clear

    get_settings.cache_clear()
    cache_clear()
    db_models._engine = None
    db_models._SessionLocal = None
    init_db()
    now = datetime.now(timezone.utc)
    with get_session_factory()() as db:
        good = Listing(
            source="t",
            external_id="g1",
            url="https://e/g1",
            deal_type="rent",
            status="active",
            price=7000,
            currency="USD",
            area_sqm=100,
            city="Київ",
            district="Печерський",
            first_seen_at=now,
            last_seen_at=now,
        )
        bad = Listing(
            source="t",
            external_id="b1",
            url="https://e/b1",
            deal_type="rent",
            status="active",
            price=781,
            currency="USD",
            area_sqm=140,
            city="Київ",
            district="Печерський",
            price_per_sqm=781,
            raw_extra={"price_suspicious": True},
            first_seen_at=now,
            last_seen_at=now,
        )
        db.add_all([good, bad])
        db.flush()
        apply_auto_stats_exclusion(bad, suspicious=True)
        db.commit()
        cache_clear()

        stats = compute_market_stats(db, deal_type="rent")
        assert stats.city_count == 1
        assert is_excluded_from_stats(bad)

        set_stats_exclusion(bad, excluded=False, user_action=True)
        assert not bad.exclude_from_stats
        apply_auto_stats_exclusion(bad, suspicious=True)
        assert not bad.exclude_from_stats
