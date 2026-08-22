from __future__ import annotations

from datetime import datetime, timezone

from app.db.models import Listing
from app.domain.enums import ListingStatus
from app.pipeline.vanish_guard import vanish_allowed


def test_vanish_blocked_on_partial_crawl(tmp_path, monkeypatch):
    db_path = tmp_path / "vg.db"
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
        for i in range(200):
            db.add(
                Listing(
                    source="lun",
                    external_id=f"a{i}",
                    url=f"https://e/{i}",
                    deal_type="sale",
                    status=ListingStatus.ACTIVE.value,
                    price=100_000,
                    currency="USD",
                    first_seen_at=now,
                    last_seen_at=now,
                )
            )
        db.commit()

        ok, reason = vanish_allowed(db, "lun", 80)
        assert not ok
        assert "partial" in reason.lower() or "%" in reason

        ok2, _ = vanish_allowed(db, "lun", 50)
        assert not ok2


def test_vanish_allowed_on_full_coverage(tmp_path, monkeypatch):
    db_path = tmp_path / "vg2.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("MIN_SEEN_FOR_VANISH", "50")
    monkeypatch.setenv("VANISH_MIN_ACTIVE_RATIO", "0.55")
    from app.config import get_settings
    from app.db import models as db_models
    from app.db.models import get_session_factory, init_db

    get_settings.cache_clear()
    db_models._engine = None
    db_models._SessionLocal = None
    init_db()
    now = datetime.now(timezone.utc)
    with get_session_factory()() as db:
        for i in range(100):
            db.add(
                Listing(
                    source="domria",
                    external_id=f"d{i}",
                    url=f"https://e/{i}",
                    deal_type="rent",
                    status=ListingStatus.ACTIVE.value,
                    price=5000,
                    currency="USD",
                    first_seen_at=now,
                    last_seen_at=now,
                )
            )
        db.commit()

        ok, reason = vanish_allowed(db, "domria", 60)
        assert ok
        assert "ratio" in reason
