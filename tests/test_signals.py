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
    from app.db.models import Listing, Property, PropertyEvent, get_session_factory, init_db
    from app.domain.signals import listing_ids_for_price_drops, listing_ids_for_vanished

    get_settings.cache_clear()
    db_models._engine = None
    db_models._SessionLocal = None
    init_db()
    SessionLocal = get_session_factory()
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        prop = Property(
            fingerprint="fp-test-vanish",
            deal_type="sale",
            property_type="office",
        )
        db.add(prop)
        db.flush()
        lst = Listing(
            source="t",
            external_id="1",
            url="https://e/1",
            deal_type="sale",
            property_id=prop.id,
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
                property_id=prop.id,
                listing_id=lst.id,
                event_type="vanished",
                occurred_at=now,
                payload={"level": "property"},
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


def test_activity_summary_respects_deal_type(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    db_path = tmp_path / "act2.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    from app.config import get_settings
    from app.db import models as db_models
    from app.db.models import Listing, PropertyEvent, get_session_factory, init_db
    from app.domain.signals import activity_summary
    from app.domain.ttl_cache import cache_clear

    get_settings.cache_clear()
    cache_clear()
    db_models._engine = None
    db_models._SessionLocal = None
    init_db()
    SessionLocal = get_session_factory()
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        sale = Listing(
            source="t",
            external_id="s1",
            url="https://e/s1",
            deal_type="sale",
            status="active",
            first_seen_at=now,
            last_seen_at=now,
            price=100000,
            currency="USD",
        )
        rent = Listing(
            source="t",
            external_id="r1",
            url="https://e/r1",
            deal_type="rent",
            status="active",
            first_seen_at=now,
            last_seen_at=now,
            price=1000,
            currency="USD",
        )
        db.add_all([sale, rent])
        db.flush()
        db.add_all(
            [
                PropertyEvent(
                    listing_id=sale.id,
                    event_type="price_changed",
                    occurred_at=now,
                    payload={"old_price": 120000, "new_price": 100000},
                ),
                PropertyEvent(
                    listing_id=rent.id,
                    event_type="price_changed",
                    occurred_at=now,
                    payload={"old_price": 1200, "new_price": 1000},
                ),
            ]
        )
        db.commit()
        assert activity_summary(db, hours=24, deal_type="sale")["price_drops"] == 1
        assert activity_summary(db, hours=24, deal_type="rent")["price_drops"] == 1
        assert activity_summary(db, hours=24)["price_drops"] == 2
