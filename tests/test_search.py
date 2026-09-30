from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from app.domain.search import (
    listing_text_search_filter,
    parse_address_search_query,
    search_query_variants,
    search_relevance_tier,
    text_has_house_number,
)


def test_search_query_variants_ru_ua():
    variants = search_query_variants("саксаганского")
    assert "саксаган" in variants
    assert "Саксаган" in variants
    assert any("сакс" in v.lower() for v in variants)


def test_parse_address_search_query_street_and_house():
    p = parse_address_search_query("саксаганського 70")
    assert p is not None
    assert "саксаганського" in p.street.lower()
    assert p.house == "70"


def test_text_has_house_number_variants():
    assert text_has_house_number("вул. Саксаганського, 70А", "70")
    assert text_has_house_number("Саксаганського 70/1", "70")
    assert not text_has_house_number("Саксаганського, 40/85", "70")
    assert not text_has_house_number("Саксаганського 121", "70")
    assert not text_has_house_number("буд. 700", "70")


def test_search_relevance_tier_street_then_house():
    hit = SimpleNamespace(
        title="Оренда на Саксаганського 70А",
        address_raw="вулиця Саксаганського, 70А",
        district="Голосіївський",
        city="Київ",
    )
    other = SimpleNamespace(
        title="Офіс",
        address_raw="вулиця Саксаганського, 40/85",
        district="Голосіївський",
        city="Київ",
    )
    far = SimpleNamespace(
        title="Хрещатик 1",
        address_raw="Хрещатик 1",
        district="Печерський",
        city="Київ",
    )
    q = "саксаганського 70"
    assert search_relevance_tier(hit, q) == 2
    assert search_relevance_tier(other, q) == 1
    assert search_relevance_tier(far, q) == 0


def test_listing_text_search_filter_matches_ukrainian_address(tmp_path, monkeypatch):
    db_path = tmp_path / "search.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    from app.config import get_settings
    from app.db import models as db_models
    from app.db.models import Listing, get_session_factory, init_db
    from sqlalchemy import func, select

    get_settings.cache_clear()
    db_models._engine = None
    db_models._SessionLocal = None
    init_db()
    now = datetime.now(timezone.utc)
    with get_session_factory()() as db:
        db.add(
            Listing(
                source="t",
                external_id="saks-1",
                url="https://example.test/saks",
                deal_type="rent",
                status="active",
                price=1000,
                currency="USD",
                area_sqm=50,
                address_raw="вул. Саксаганського, 70",
                title="Офіс",
                first_seen_at=now,
                last_seen_at=now,
            )
        )
        db.commit()
        clause = listing_text_search_filter("саксаганского")
        n = db.scalar(select(func.count()).select_from(Listing).where(clause)) or 0
        assert n == 1
