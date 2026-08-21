from __future__ import annotations

from datetime import datetime, timezone

from app.db.models import Listing
from app.web.routes import _apply_range_filters, _sort_listings_in_memory


def _lst(**kwargs) -> Listing:
    defaults = {
        "source": "test",
        "external_id": "1",
        "url": "https://example.com/1",
        "deal_type": "sale",
        "currency": "USD",
        "first_seen_at": datetime(2024, 1, 1, tzinfo=timezone.utc),
        "last_seen_at": datetime(2024, 1, 2, tzinfo=timezone.utc),
    }
    defaults.update(kwargs)
    return Listing(**defaults)


def test_sort_listings_by_price_usd():
    a = _lst(external_id="a", price=100, currency="USD", area_sqm=10)
    b = _lst(external_id="b", price=4100, currency="UAH", area_sqm=10)  # ~100 USD
    c = _lst(external_id="c", price=50, currency="USD", area_sqm=10)
    rows = [a, b, c]
    got = _sort_listings_in_memory(rows, "price_asc")
    assert [x.external_id for x in got] == ["c", "a", "b"] or [
        x.external_id for x in got
    ] == ["c", "b", "a"]
    # c cheapest; a and b roughly equal (~100) — order among equals may vary
    assert got[0].external_id == "c"


def test_apply_price_and_area_filters():
    rows = [
        _lst(external_id="1", price=150_000, currency="USD", area_sqm=80),
        _lst(external_id="2", price=90_000, currency="USD", area_sqm=120),
        _lst(external_id="3", price=200_000, currency="USD", area_sqm=50),
    ]
    got = _apply_range_filters(
        rows, price_min=100_000, price_max=180_000, area_min=60, area_max=None
    )
    assert [x.external_id for x in got] == ["1"]
