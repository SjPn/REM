from __future__ import annotations

from app.domain.signals import (
    below_market_hint,
    classify_seller,
    detect_opex,
    parse_cap_and_noi,
)
from app.scrapers.http_utils import PortalBlockedError, sleep_crawl_delay


def test_parse_cap_and_noi_explicit_only():
    text = "Офіс, cap rate 8.5%, NOI 120 000 USD на рік"
    got = parse_cap_and_noi(text)
    assert got["cap_rate_pct"] == 8.5
    assert got["noi"] == 120000.0
    assert parse_cap_and_noi("просто офіс без цифр") == {}


def test_detect_opex():
    assert detect_opex("Аренда офиса, без OPEX") == "without"
    assert detect_opex("Оренда, + OPEX окремо") == "without"
    assert detect_opex("Ставка с OPEX включена") == "with"
    assert detect_opex("Все включено, all inclusive") == "with"
    assert detect_opex("Просто офис 100 м2") == "unknown"
    assert detect_opex("с opex", "но без opex") == "unknown"


def test_below_market_hint():
    hint = below_market_hint(
        price=80000,
        currency="USD",
        area=100,
        deal_type="sale",
        district="Печерський",
        address="Печерський",
        title="офіс",
        city="Київ",
        median_by_district={"Печерський": 1200.0},
        city_median=1000.0,
        threshold=0.12,
    )
    assert hint.below_market is True
    assert hint.discount_pct is not None and hint.discount_pct > 12


def test_classify_seller():
    assert classify_seller(agency="АН Київ", phone="380501112233", phone_listing_count=1) == "agency"
    assert classify_seller(agency=None, phone="380501112233", phone_listing_count=5) == "agency"
    assert classify_seller(agency=None, phone="380501112233", title="Офіс власника", phone_listing_count=1) == "owner"


def test_count_active_inventory_shape():
    from app.db import init_db, get_session_factory
    from app.domain.market_stats import count_active_inventory

    init_db()
    db = get_session_factory()()
    try:
        inv = count_active_inventory(db)
        assert "sale_total" in inv and "rent_total" in inv
        assert isinstance(inv["districts"], list)
    finally:
        db.close()


def test_portal_blocked_error():
    err = PortalBlockedError(429, "https://example.com")
    assert err.status_code == 429


def test_listing_ids_for_activity(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    db_path = tmp_path / "act.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    from app.config import get_settings
    from app.db import models as db_models
    from app.db.models import Listing, PropertyEvent, get_session_factory, init_db
    from app.domain.signals import listing_ids_for_price_drops, listing_ids_for_vanished

    get_settings.cache_clear()
    db_models._engine = None
    db_models._SessionLocal = None
    init_db()
    SessionLocal = get_session_factory()
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        lst = Listing(
            source="t",
            external_id="1",
            url="https://e/1",
            deal_type="sale",
            status="vanished",
            first_seen_at=now - timedelta(days=3),
            last_seen_at=now,
            vanished_at=now,
            price=100000,
            currency="USD",
        )
        db.add(lst)
        db.flush()
        db.add(
            PropertyEvent(
                listing_id=lst.id,
                event_type="vanished",
                occurred_at=now,
                payload={},
            )
        )
        db.add(
            PropertyEvent(
                listing_id=lst.id,
                event_type="price_changed",
                occurred_at=now,
                payload={"old_price": 120000, "new_price": 100000},
            )
        )
        db.commit()
        since = now - timedelta(hours=24)
        assert listing_ids_for_vanished(db, since=since, deal_type="sale") == [lst.id]
        assert listing_ids_for_price_drops(db, since=since, deal_type="sale") == [lst.id]
        assert listing_ids_for_vanished(db, since=since, deal_type="rent") == []
    called = {}

    def fake_sleep(sec):
        called["sec"] = sec

    monkeypatch.setattr("app.scrapers.http_utils.time.sleep", fake_sleep)
    monkeypatch.setattr("app.scrapers.http_utils.random.uniform", lambda a, b: 0.0)
    monkeypatch.setattr(
        "app.scrapers.http_utils.get_settings",
        lambda: type(
            "S",
            (),
            {
                "crawl_delay_sec": 1.0,
                "crawl_delay_jitter_sec": 0.0,
                "crawl_block_backoff_sec": 8.0,
                "crawl_human_mode": False,
            },
        )(),
    )
    sleep_crawl_delay()
    assert called["sec"] == 1.0
    sleep_crawl_delay(blocked=True)
    assert called["sec"] == 8.0
