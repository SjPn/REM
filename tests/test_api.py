from __future__ import annotations

import os
from pathlib import Path

# Isolate API tests from the working SQLite DB BEFORE app imports engine.
_TEST_DB = Path(__file__).resolve().parent / "test_api.db"
if _TEST_DB.exists():
    _TEST_DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite:///{_TEST_DB}"

from app.config import get_settings

get_settings.cache_clear()

from fastapi.testclient import TestClient

from app.db.models import Base, get_engine
from app.main import app


def setup_module():
    engine = get_engine()
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def test_health_and_demo_seed():
    client = TestClient(app)
    assert client.get("/api/health").json()["status"] == "ok"
    seeded = client.post("/api/demo/seed").json()
    assert seeded["listings"] >= 4
    stats = client.get("/api/stats").json()
    assert stats["properties"] >= 1
    deals = client.get("/api/deals").json()
    assert isinstance(deals, list)
    assert client.get("/").status_code == 200
    assert client.get("/stats").status_code == 200
    assert "медиана" in client.get("/").text.lower()
