from datetime import datetime, timedelta, timezone

from app.db.models import Listing
from app.domain.market_stats import (
    DistrictAvg,
    MarketSlice,
    extract_district,
    normalize_district,
    rough_yield_by_district,
    to_usd,
)
from app.web.routes import _days_on_market, _sort_listings_in_memory


def test_normalize_district():
    assert normalize_district("Дарницький район") == "Дарницький"
    assert normalize_district("шевченковский") == "Шевченківський"


def test_extract_district_from_text():
    assert extract_district("Офіс, Печерський р-н") == "Печерський"


def test_to_usd():
    assert to_usd(4100, "UAH") == 100.0
    assert to_usd(100, "USD") == 100.0


def _empty_rent() -> MarketSlice:
    return MarketSlice(
        deal_type="rent",
        city_avg_psm=None,
        city_median_psm=None,
        city_count=0,
        districts=[],
    )


def test_rough_yield_by_district():
    pechersk_sale = DistrictAvg(district="Печерський", avg_psm=4000, median_psm=4000, count=20)
    pechersk_rent = DistrictAvg(district="Печерський", avg_psm=25, median_psm=25, count=15)
    market = {
        "sale": MarketSlice(
            deal_type="sale",
            city_avg_psm=4000,
            city_median_psm=4000,
            city_count=20,
            districts=[pechersk_sale],
        ),
        "rent": MarketSlice(
            deal_type="rent",
            city_avg_psm=25,
            city_median_psm=25,
            city_count=15,
            districts=[pechersk_rent],
        ),
        "rent_without_opex": _empty_rent(),
        "rent_with_opex": _empty_rent(),
        "rent_opex_unknown": _empty_rent(),
    }
    got = rough_yield_by_district(market)
    assert got["city_yield_pct"] == 7.5  # 25*12/4000*100
    assert len(got["rows"]) == 1
    assert got["rows"][0]["yield_pct"] == 7.5
    assert got["rows"][0]["payback_years"] == round(100 / 7.5, 1)


def test_days_on_market():
    assert _days_on_market(None) is None
    ago = datetime.now(timezone.utc) - timedelta(days=5, hours=3)
    assert _days_on_market(ago) == 5


def test_sort_by_dom():
    old = Listing(
        source="t",
        external_id="old",
        url="https://e/1",
        deal_type="sale",
        first_seen_at=datetime.now(timezone.utc) - timedelta(days=30),
        last_seen_at=datetime.now(timezone.utc),
    )
    new = Listing(
        source="t",
        external_id="new",
        url="https://e/2",
        deal_type="sale",
        first_seen_at=datetime.now(timezone.utc) - timedelta(days=2),
        last_seen_at=datetime.now(timezone.utc),
    )
    got = _sort_listings_in_memory([new, old], "dom_desc")
    assert [x.external_id for x in got] == ["old", "new"]
