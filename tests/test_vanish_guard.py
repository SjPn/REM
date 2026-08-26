from __future__ import annotations

from datetime import datetime, timedelta, timezone


def test_vanish_blocked_on_partial_crawl(tmp_path, monkeypatch):
    db_path = tmp_path / "vg.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("MIN_SEEN_FOR_VANISH", "50")
    monkeypatch.setenv("VANISH_MIN_ACTIVE_RATIO", "0.85")
    monkeypatch.setenv("COVERAGE_LOOKBACK_DAYS", "14")
    from app.config import get_settings
    from app.db import models as db_models
    from app.db.models import Listing, get_session_factory, init_db
    from app.domain.enums import ListingStatus
    from app.pipeline.vanish_guard import vanish_allowed

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
        assert "fresh" in reason.lower() or "%" in reason


def test_vanish_allowed_on_full_coverage(tmp_path, monkeypatch):
    db_path = tmp_path / "vg2.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("MIN_SEEN_FOR_VANISH", "50")
    monkeypatch.setenv("VANISH_MIN_ACTIVE_RATIO", "0.55")
    monkeypatch.setenv("COVERAGE_LOOKBACK_DAYS", "14")
    from app.config import get_settings
    from app.db import models as db_models
    from app.db.models import Listing, get_session_factory, init_db
    from app.domain.enums import ListingStatus
    from app.pipeline.vanish_guard import vanish_allowed

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


def test_stale_ghosts_ignored_in_coverage(tmp_path, monkeypatch):
    """68k ghosts must not block vanish if fresh window is well covered."""
    db_path = tmp_path / "vg_stale.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("MIN_SEEN_FOR_VANISH", "50")
    monkeypatch.setenv("VANISH_MIN_ACTIVE_RATIO", "0.85")
    monkeypatch.setenv("COVERAGE_LOOKBACK_DAYS", "14")
    from app.config import get_settings
    from app.db import models as db_models
    from app.db.models import Listing, get_session_factory, init_db
    from app.domain.enums import ListingStatus
    from app.pipeline.vanish_guard import count_fresh_active, vanish_allowed

    get_settings.cache_clear()
    db_models._engine = None
    db_models._SessionLocal = None
    init_db()
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=40)
    with get_session_factory()() as db:
        for i in range(100):
            db.add(
                Listing(
                    source="lun",
                    external_id=f"fresh{i}",
                    url=f"https://e/f{i}",
                    deal_type="sale",
                    status=ListingStatus.ACTIVE.value,
                    price=100_000,
                    currency="USD",
                    first_seen_at=now,
                    last_seen_at=now,
                    raw_extra={"zone": "kyiv"},
                )
            )
        for i in range(500):
            db.add(
                Listing(
                    source="lun",
                    external_id=f"stale{i}",
                    url=f"https://e/s{i}",
                    deal_type="sale",
                    status=ListingStatus.ACTIVE.value,
                    price=100_000,
                    currency="USD",
                    first_seen_at=old,
                    last_seen_at=old,
                    raw_extra={"zone": "kyiv"},
                )
            )
        db.commit()
        assert count_fresh_active(db, "lun") == 100
        # 90/100 fresh = 90% >= 85%
        ok, reason = vanish_allowed(db, "lun", 90)
        assert ok, reason
        assert "stale_ignored=500" in reason


def test_lun_zone_scope(tmp_path, monkeypatch):
    db_path = tmp_path / "vg_zone.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("MIN_SEEN_FOR_VANISH", "10")
    monkeypatch.setenv("VANISH_MIN_ACTIVE_RATIO", "0.85")
    monkeypatch.setenv("COVERAGE_LOOKBACK_DAYS", "14")
    from app.config import get_settings
    from app.db import models as db_models
    from app.db.models import Listing, get_session_factory, init_db
    from app.domain.enums import ListingStatus
    from app.pipeline.vanish_guard import count_fresh_active, vanish_allowed

    get_settings.cache_clear()
    db_models._engine = None
    db_models._SessionLocal = None
    init_db()
    now = datetime.now(timezone.utc)
    with get_session_factory()() as db:
        for i in range(20):
            db.add(
                Listing(
                    source="lun",
                    external_id=f"k{i}",
                    url=f"https://e/k{i}",
                    deal_type="sale",
                    status=ListingStatus.ACTIVE.value,
                    price=1,
                    currency="USD",
                    city="Київ",
                    first_seen_at=now,
                    last_seen_at=now,
                    raw_extra={"zone": "kyiv"},
                )
            )
        for i in range(20):
            db.add(
                Listing(
                    source="lun",
                    external_id=f"r{i}",
                    url=f"https://e/r{i}",
                    deal_type="sale",
                    status=ListingStatus.ACTIVE.value,
                    price=1,
                    currency="USD",
                    city="Київська область",
                    first_seen_at=now,
                    last_seen_at=now,
                    raw_extra={"zone": "region"},
                )
            )
        db.commit()
        assert count_fresh_active(db, "lun", zone="kyiv") == 20
        assert count_fresh_active(db, "lun", zone="region") == 20
        ok_k, _ = vanish_allowed(db, "lun", 18, zone="kyiv")
        assert ok_k
        ok_r, reason_r = vanish_allowed(db, "lun", 10, zone="region")
        assert not ok_r
