from __future__ import annotations

import math
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Listing

KYIV_DISTRICTS = [
    "Печерський",
    "Шевченківський",
    "Голосіївський",
    "Подільський",
    "Дарницький",
    "Дніпровський",
    "Солом'янський",
    "Оболонський",
    "Святошинський",
    "Деснянський",
]

# Approximate FX for market averages (overridable via settings later)
_UAH_PER_USD = 41.0
_EUR_PER_USD = 0.92

# Outlier guards (USD / m²)
_SALE_PSM_MIN, _SALE_PSM_MAX = 200.0, 50_000.0
_RENT_PSM_MIN, _RENT_PSM_MAX = 3.0, 800.0


@dataclass
class DistrictAvg:
    district: str
    avg_psm: float
    median_psm: float
    count: int
    currency: str = "USD"


@dataclass
class MarketSlice:
    deal_type: str  # sale | rent
    city_avg_psm: float | None
    city_median_psm: float | None
    city_count: int
    districts: list[DistrictAvg]
    currency: str = "USD"


def normalize_district(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip()
    low = text.lower().replace(" район", "").replace(" р-н", "").strip()
    for d in KYIV_DISTRICTS:
        if d.lower() in low or low in d.lower():
            return d
    # common translit / russian forms
    aliases = {
        "печерск": "Печерський",
        "шевченк": "Шевченківський",
        "голосеев": "Голосіївський",
        "голосіїв": "Голосіївський",
        "подол": "Подільський",
        "поділь": "Подільський",
        "дарниц": "Дарницький",
        "днепр": "Дніпровський",
        "дніпр": "Дніпровський",
        "соломен": "Солом'янський",
        "солом": "Солом'янський",
        "оболон": "Оболонський",
        "святошин": "Святошинський",
        "деснян": "Деснянський",
    }
    for key, name in aliases.items():
        if key in low:
            return name
    return None


def extract_district(*parts: str | None) -> str | None:
    blob = " ".join(p for p in parts if p)
    if not blob:
        return None
    # "Дарницький район" / "район Дарницький"
    m = re.search(
        r"(Печерськ\w*|Шевченківськ\w*|Голосіївськ\w*|Подільськ\w*|Дарницьк\w*|"
        r"Дніпровськ\w*|Солом['’]?янськ\w*|Оболонськ\w*|Святошинськ\w*|Деснянськ\w*)",
        blob,
        re.IGNORECASE,
    )
    if m:
        return normalize_district(m.group(1))
    return normalize_district(blob)


def to_usd(price: float, currency: str | None) -> float | None:
    cur = (currency or "USD").upper()
    if cur == "USD":
        return price
    if cur == "UAH":
        return price / _UAH_PER_USD
    if cur == "EUR":
        return price / _EUR_PER_USD
    return None


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    vals = sorted(values)
    n = len(vals)
    mid = n // 2
    if n % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2


def _avg(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def compute_market_stats(
    db: Session,
    *,
    deal_type: str,
    status_active_only: bool = True,
) -> MarketSlice:
    q = select(Listing).where(
        Listing.deal_type == deal_type,
        Listing.price.is_not(None),
        Listing.area_sqm.is_not(None),
        Listing.area_sqm > 0,
    )
    if status_active_only:
        q = q.where(Listing.status.in_(["active", "relisted"]))

    lo, hi = (_SALE_PSM_MIN, _SALE_PSM_MAX) if deal_type == "sale" else (_RENT_PSM_MIN, _RENT_PSM_MAX)

    by_district: dict[str, list[float]] = {d: [] for d in KYIV_DISTRICTS}
    city_vals: list[float] = []

    for lst in db.scalars(q):
        usd = to_usd(float(lst.price), lst.currency)
        if usd is None or not math.isfinite(usd):
            continue
        area = float(lst.area_sqm)
        if area <= 0:
            continue
        psm = usd / area
        if not math.isfinite(psm) or psm < lo or psm > hi:
            continue

        district = normalize_district(lst.district) or extract_district(
            lst.address_raw, lst.title, lst.city
        )
        # Only Kyiv city districts for district table; oblast goes to city average only if city is Kyiv
        city = (lst.city or "").lower()
        is_kyiv_city = "київ" in city or "киев" in city or "kyiv" in city
        if is_kyiv_city or district:
            city_vals.append(psm)
        if district in by_district:
            by_district[district].append(psm)

    districts: list[DistrictAvg] = []
    for name in KYIV_DISTRICTS:
        vals = by_district[name]
        if not vals:
            continue
        districts.append(
            DistrictAvg(
                district=name,
                avg_psm=round(_avg(vals) or 0, 1),
                median_psm=round(_median(vals) or 0, 1),
                count=len(vals),
            )
        )
    districts.sort(key=lambda d: d.avg_psm, reverse=True)

    return MarketSlice(
        deal_type=deal_type,
        city_avg_psm=round(_avg(city_vals), 1) if city_vals else None,
        city_median_psm=round(_median(city_vals), 1) if city_vals else None,
        city_count=len(city_vals),
        districts=districts,
    )


def compute_all_market_stats(db: Session) -> dict[str, MarketSlice]:
    return {
        "sale": compute_market_stats(db, deal_type="sale"),
        "rent": compute_market_stats(db, deal_type="rent"),
    }
