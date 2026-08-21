from __future__ import annotations

from sqlalchemy import func, select

from app.db.models import MarketStatSnapshot, get_session_factory, init_db
from app.domain.market_history import record_market_snapshot, series_for_charts


def test_record_market_snapshot_idempotent(tmp_path, monkeypatch):
    db_path = tmp_path / "snap.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    from app.config import get_settings
    from app.db import models as db_models

    get_settings.cache_clear()
    db_models._engine = None
    db_models._SessionLocal = None
    init_db()
    SessionLocal = get_session_factory()
    with SessionLocal() as db:
        a = record_market_snapshot(db, force=True)
        b = record_market_snapshot(db, force=True)
        assert a.day == b.day
        n = db.scalar(select(func.count()).select_from(MarketStatSnapshot))
        assert n == 1
        series = series_for_charts(db)
        assert series["n"] == 1
        assert series["labels"] == [a.day]
