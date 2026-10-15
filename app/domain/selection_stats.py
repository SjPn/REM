"""Средняя и медиана цен текущей выборки каталога, не всего города."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable

from app.domain.market_stats import to_usd
from app.domain.pricing import effective_listing_psm_usd


@dataclass(frozen=True)
class SelectionPriceStats:
    listings: int
    priced: int
    with_psm: int
    median_usd: float | None
    avg_usd: float | None
    median_psm: float | None
    avg_psm: float | None


def _positive(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _avg(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def selection_price_stats(rows: Iterable) -> SelectionPriceStats:
    """Цены выборки в USD. Объявление без цены в среднюю не входит."""
    prices: list[float] = []
    rates: list[float] = []
    listings = 0
    for row in rows:
        listings += 1
        price = _positive(getattr(row, "price", None))
        currency = getattr(row, "currency", None)
        if price is not None:
            usd = to_usd(price, currency)
            if usd is not None and math.isfinite(usd) and usd > 0:
                prices.append(usd)
        psm = effective_listing_psm_usd(
            getattr(row, "price", None),
            currency,
            getattr(row, "area_sqm", None),
            deal_type=getattr(row, "deal_type", None),
            price_per_sqm=getattr(row, "price_per_sqm", None),
        )
        if psm is not None and math.isfinite(psm) and psm > 0:
            rates.append(psm)
    return SelectionPriceStats(
        listings=listings,
        priced=len(prices),
        with_psm=len(rates),
        median_usd=_median(prices),
        avg_usd=_avg(prices),
        median_psm=_median(rates),
        avg_psm=_avg(rates),
    )
