"""Средняя и медиана считаются по переданным объявлениям, не по городу."""

from types import SimpleNamespace

from app.domain.selection_stats import selection_price_stats


def _row(price, currency="USD", area=100, psm=None, deal_type="sale"):
    return SimpleNamespace(
        price=price,
        currency=currency,
        area_sqm=area,
        price_per_sqm=psm,
        deal_type=deal_type,
    )


def test_median_and_average_follow_the_selection():
    stats = selection_price_stats(
        [_row(100_000), _row(200_000), _row(900_000), _row(None)]
    )
    assert stats.listings == 4
    assert stats.priced == 3
    assert stats.median_usd == 200_000
    assert stats.avg_usd == 400_000
    assert stats.with_psm == 3
    assert stats.median_psm == 2_000
    assert stats.avg_psm == 4_000


def test_even_count_median_is_the_middle_pair():
    stats = selection_price_stats([_row(100_000), _row(300_000, area=None)])
    assert stats.median_usd == 200_000
    assert stats.avg_usd == 200_000
    assert stats.with_psm == 1
    assert stats.median_psm == 1_000


def test_empty_selection():
    stats = selection_price_stats([])
    assert stats.listings == 0
    assert stats.priced == 0
    assert stats.median_usd is None
    assert stats.avg_usd is None
