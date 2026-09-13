"""Unit tests for portal readiness snapshot (no live crawl)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def test_snapshot_readiness_counts_primary_only(tmp_path, monkeypatch):
    db_path = tmp_path / "ready.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("MIN_SEEN_FOR_VANISH", "10")
    monkeypatch.setenv("VANISH_MIN_ACTIVE_RATIO", "0.5")
    from app.config import get_settings
    from app.db import models as db_models
    from app.db.models import CrawlRun, Listing, get_session_factory, init_db
    from app.domain.ttl_cache import cache_clear
    from app.pipeline.ensure_ready import snapshot_readiness

    get_settings.cache_clear()
    cache_clear()
    db_models._engine = None
    db_models._SessionLocal = None
    init_db()
    now = datetime.now(timezone.utc)

    with get_session_factory()() as db:
        for i in range(12):
            db.add(
                Listing(
                    source="olx",
                    external_id=f"o{i}",
                    url=f"https://e/o{i}",
                    deal_type="sale",
                    status="active",
                    last_seen_at=now,
                )
            )
        db.add(
            CrawlRun(
                source="olx",
                status="ok",
                pages_fetched=5,
                listings_seen=12,
                started_at=now,
                finished_at=now,
            )
        )
        db.commit()

        snap = snapshot_readiness(db, sources=["olx"])
        assert snap.total == 1
        assert snap.ready == 1
        assert snap.all_ready is True
        assert snap.sources[0].source == "olx"
        assert snap.sources[0].vanish_ok is True


def test_snapshot_marks_stale_ok_as_needs_refresh(tmp_path, monkeypatch):
    db_path = tmp_path / "stale.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("MIN_SEEN_FOR_VANISH", "5")
    from app.config import get_settings
    from app.db import models as db_models
    from app.db.models import CrawlRun, Listing, get_session_factory, init_db
    from app.domain.ttl_cache import cache_clear
    from app.pipeline.ensure_ready import snapshot_readiness

    get_settings.cache_clear()
    cache_clear()
    db_models._engine = None
    db_models._SessionLocal = None
    init_db()
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=30)

    with get_session_factory()() as db:
        for i in range(8):
            db.add(
                Listing(
                    source="m2bomber",
                    external_id=f"m{i}",
                    url=f"https://e/m{i}",
                    deal_type="rent",
                    status="active",
                    last_seen_at=old,
                )
            )
        db.add(
            CrawlRun(
                source="m2bomber",
                status="ok",
                pages_fetched=40,
                listings_seen=8,
                started_at=old,
                finished_at=old,
            )
        )
        db.commit()

        snap = snapshot_readiness(db, sources=["m2bomber"], stale_crawl_days=14)
        assert snap.sources[0].vanish_ok is True
        assert snap.sources[0].needs_refresh is True
        assert snap.all_ready is False
