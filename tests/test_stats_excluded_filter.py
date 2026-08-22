from __future__ import annotations

import html
import re
from datetime import datetime, timezone

from fastapi.testclient import TestClient


def _seed_listings(db_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    from app.config import get_settings
    from app.db import models as db_models
    from app.db.models import Listing, get_session_factory, init_db
    from app.domain.ttl_cache import cache_clear

    get_settings.cache_clear()
    cache_clear()
    db_models._engine = None
    db_models._SessionLocal = None
    init_db()
    now = datetime.now(timezone.utc)
    with get_session_factory()() as db:
        rows = [
            Listing(
                source="t",
                external_id="in1",
                url="https://e/in1",
                deal_type="sale",
                status="active",
                price=100_000,
                currency="USD",
                area_sqm=50,
                city="Київ",
                exclude_from_stats=False,
                first_seen_at=now,
                last_seen_at=now,
            ),
            Listing(
                source="t",
                external_id="in2",
                url="https://e/in2",
                deal_type="sale",
                status="active",
                price=200_000,
                currency="USD",
                area_sqm=80,
                city="Київ",
                exclude_from_stats=False,
                first_seen_at=now,
                last_seen_at=now,
            ),
            Listing(
                source="t",
                external_id="ex1",
                url="https://e/ex1",
                deal_type="sale",
                status="active",
                price=300_000,
                currency="USD",
                area_sqm=60,
                city="Київ",
                exclude_from_stats=True,
                first_seen_at=now,
                last_seen_at=now,
            ),
            Listing(
                source="t",
                external_id="ex2",
                url="https://e/ex2",
                deal_type="sale",
                status="active",
                price=400_000,
                currency="USD",
                area_sqm=70,
                city="Київ",
                exclude_from_stats=True,
                first_seen_at=now,
                last_seen_at=now,
            ),
        ]
        db.add_all(rows)
        db.commit()


def _total_from_html(html: str) -> int | None:
    m = re.search(r"(\d+)\s*·\s*стр\.", html)
    return int(m.group(1)) if m else None


def test_stats_excluded_filter_limits_list(tmp_path, monkeypatch):
    db_path = tmp_path / "stats_filter.db"
    _seed_listings(db_path, monkeypatch)
    from app.main import app

    client = TestClient(app)
    all_resp = client.get("/?deal_type=sale")
    filt_resp = client.get("/?deal_type=sale&stats_excluded=1")

    assert _total_from_html(all_resp.text) == 4
    assert _total_from_html(filt_resp.text) == 2
    assert "вне статистики" in filt_resp.text
    assert filt_resp.text.count('type="checkbox" checked') == 2
    assert all_resp.text.count('type="checkbox" checked') == 2


def test_stats_excluded_pagination_keeps_filter(tmp_path, monkeypatch):
    db_path = tmp_path / "stats_page.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    from app.config import get_settings
    from app.db import models as db_models
    from app.db.models import Listing, get_session_factory, init_db
    from app.domain.ttl_cache import cache_clear

    get_settings.cache_clear()
    cache_clear()
    db_models._engine = None
    db_models._SessionLocal = None
    init_db()
    now = datetime.now(timezone.utc)
    with get_session_factory()() as db:
        for i in range(55):
            db.add(
                Listing(
                    source="t",
                    external_id=f"ex{i}",
                    url=f"https://e/ex{i}",
                    deal_type="sale",
                    status="active",
                    price=100_000 + i,
                    currency="USD",
                    area_sqm=50,
                    city="Київ",
                    exclude_from_stats=True,
                    first_seen_at=now,
                    last_seen_at=now,
                )
            )
        db.commit()

    from app.main import app

    client = TestClient(app)
    page1 = client.get("/?deal_type=sale&stats_excluded=1&per_page=50")
    assert _total_from_html(page1.text) == 55
    m = re.search(r'href="\?page=2&([^"]+)"', page1.text)
    assert m, "pager link missing"
    qs = html.unescape(m.group(1))
    assert "stats_excluded=1" in qs
    assert "stats_excluded=True" not in qs

    page2 = client.get(f"/?page=2&{qs}")
    assert _total_from_html(page2.text) == 55
    assert "вне статистики" in page2.text
    assert page2.text.count('type="checkbox" checked') == 5


def test_stats_excluded_true_string_rejected(tmp_path, monkeypatch):
    db_path = tmp_path / "stats_true.db"
    _seed_listings(db_path, monkeypatch)
    from app.main import app

    client = TestClient(app)
    resp = client.get("/?deal_type=sale&stats_excluded=True")
    assert resp.status_code == 422
