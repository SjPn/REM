from __future__ import annotations

from datetime import datetime, timezone


def test_coverage_vanish_gate(tmp_path, monkeypatch):
    db_path = tmp_path / "cov.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    from app.config import get_settings
    from app.db import models as db_models
    from app.db import get_session_factory, init_db
    from app.db.models import CrawlRun, Listing
    from app.domain.coverage import coverage_for_source, coverage_report
    from app.domain.ttl_cache import cache_clear

    get_settings.cache_clear()
    cache_clear()
    db_models._engine = None
    db_models._SessionLocal = None
    init_db()
    db = get_session_factory()()
    now = datetime.now(timezone.utc)
    try:
        for i in range(10):
            db.add(
                Listing(
                    source="lun",
                    external_id=f"c{i}",
                    url=f"https://e/{i}",
                    deal_type="sale",
                    status="active",
                    price=100000,
                    currency="USD",
                    first_seen_at=now,
                    last_seen_at=now,
                )
            )
        db.add(
            CrawlRun(
                source="lun",
                status="ok",
                started_at=now,
                finished_at=now,
                pages_fetched=2,
                listings_seen=3,
            )
        )
        db.commit()
        row = coverage_for_source(db, "lun")
        assert row.active == 10
        assert row.last_seen == 3
        assert row.ratio == 0.3
        assert row.vanish_ok is False
        report = coverage_report(db, sources=["lun"])
        assert report["sources_ready_for_vanish"] == 0
        assert report["all_ready"] is False
    finally:
        db.close()
