from __future__ import annotations

from app.domain.signals import (
    below_market_hint,
    classify_seller,
    parse_cap_and_noi,
)
from app.scrapers.http_utils import PortalBlockedError, sleep_crawl_delay


def test_parse_cap_and_noi_explicit_only():
    text = "Офіс, cap rate 8.5%, NOI 120 000 USD на рік"
    got = parse_cap_and_noi(text)
    assert got["cap_rate_pct"] == 8.5
    assert got["noi"] == 120000.0
    assert parse_cap_and_noi("просто офіс без цифр") == {}


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



def test_sleep_crawl_delay_runs(monkeypatch):
    called = {}

    def fake_sleep(sec):
        called["sec"] = sec

    monkeypatch.setattr("app.scrapers.http_utils.time.sleep", fake_sleep)
    monkeypatch.setattr(
        "app.scrapers.http_utils.get_settings",
        lambda: type(
            "S",
            (),
            {
                "crawl_delay_sec": 1.0,
                "crawl_delay_jitter_sec": 0.0,
                "crawl_block_backoff_sec": 8.0,
            },
        )(),
    )
    sleep_crawl_delay()
    assert called["sec"] >= 0.2
    sleep_crawl_delay(blocked=True)
    assert called["sec"] >= 7.0
