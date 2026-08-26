from __future__ import annotations

import math
import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
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

KYIV_DISTRICT_RU = {
    "Печерський": "Печерский",
    "Шевченківський": "Шевченковский",
    "Голосіївський": "Голосеевский",
    "Подільський": "Подольский",
    "Дарницький": "Дарницкий",
    "Дніпровський": "Днепровский",
    "Солом'янський": "Соломенский",
    "Оболонський": "Оболонский",
    "Святошинський": "Святошинский",
    "Деснянський": "Деснянский",
}


def district_label_ru(name: str | None) -> str:
    if not name:
        return ""
    return KYIV_DISTRICT_RU.get(name, name)

# Outlier guards (USD / m²). Kyiv commercial sale: below ~$450 or above ~$10k is noise.
_SALE_PSM_MIN, _SALE_PSM_MAX = 450.0, 10_000.0
# Kyiv commercial rent: >~$50/m²·мес is almost only prime retail; treat as outlier.
_RENT_PSM_MIN, _RENT_PSM_MAX = 3.0, 70.0


def to_usd(price: float, currency: str | None) -> float | None:
    cur = (currency or "USD").upper()
    if cur == "USD":
        return price
    settings = get_settings()
    if cur == "UAH":
        rate = float(settings.uah_per_usd) or 44.61
        return price / rate
    if cur == "EUR":
        return price * float(settings.usd_per_eur or 1.169)
    return None


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
    opex_filter: str | None = None,
) -> MarketSlice:
    """opex_filter for rent only: with | without | unknown | None (all)."""
    from app.domain.listing_stats import is_excluded_from_stats
    from app.domain.signals import resolve_listing_opex

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
        if is_excluded_from_stats(lst):
            continue
        if deal_type == "rent" and opex_filter:
            if resolve_listing_opex(lst) != opex_filter:
                continue
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


def _slice_from_buckets(
    deal_type: str,
    city_vals: list[float],
    by_district: dict[str, list[float]],
) -> MarketSlice:
    districts = [
        DistrictAvg(
            district=name,
            avg_psm=round(_avg(vals), 1),
            median_psm=round(_median(vals), 1),
            count=len(vals),
        )
        for name, vals in by_district.items()
        if vals
    ]
    districts.sort(key=lambda d: (-d.count, d.district))
    return MarketSlice(
        deal_type=deal_type,
        city_avg_psm=round(_avg(city_vals), 1) if city_vals else None,
        city_median_psm=round(_median(city_vals), 1) if city_vals else None,
        city_count=len(city_vals),
        districts=districts,
    )


def compute_all_market_stats(db: Session) -> dict[str, MarketSlice]:
    """One DB pass for sale + all rent OPEX cohorts (was 5 full scans)."""
    from app.domain.listing_stats import is_excluded_from_stats
    from app.domain.signals import resolve_listing_opex
    from app.domain.ttl_cache import cache_get

    def _build() -> dict[str, MarketSlice]:
        sale_city: list[float] = []
        sale_by = {d: [] for d in KYIV_DISTRICTS}
        rent_city: list[float] = []
        rent_by = {d: [] for d in KYIV_DISTRICTS}
        rent_with_city: list[float] = []
        rent_with_by = {d: [] for d in KYIV_DISTRICTS}
        rent_without_city: list[float] = []
        rent_without_by = {d: [] for d in KYIV_DISTRICTS}
        rent_unknown_city: list[float] = []
        rent_unknown_by = {d: [] for d in KYIV_DISTRICTS}

        from sqlalchemy.orm import load_only

        q = (
            select(Listing)
            .where(
                Listing.status.in_(["active", "relisted"]),
                Listing.price.is_not(None),
                Listing.area_sqm.is_not(None),
                Listing.area_sqm > 0,
                Listing.deal_type.in_(["sale", "rent"]),
            )
            .options(
                load_only(
                    Listing.id,
                    Listing.deal_type,
                    Listing.price,
                    Listing.currency,
                    Listing.area_sqm,
                    Listing.district,
                    Listing.address_raw,
                    Listing.title,
                    Listing.description,
                    Listing.city,
                    Listing.raw_extra,
                    Listing.exclude_from_stats,
                )
            )
        )
        for lst in db.scalars(q):
            if is_excluded_from_stats(lst):
                continue
            usd = to_usd(float(lst.price), lst.currency)
            if usd is None or not math.isfinite(usd):
                continue
            area = float(lst.area_sqm)
            if area <= 0:
                continue
            psm = usd / area
            if not math.isfinite(psm):
                continue
            district = normalize_district(lst.district) or extract_district(
                lst.address_raw, lst.title, lst.city
            )
            city = (lst.city or "").lower()
            is_kyiv_city = "київ" in city or "киев" in city or "kyiv" in city

            if lst.deal_type == "sale":
                if psm < _SALE_PSM_MIN or psm > _SALE_PSM_MAX:
                    continue
                if is_kyiv_city or district:
                    sale_city.append(psm)
                if district in sale_by:
                    sale_by[district].append(psm)
                continue

            if psm < _RENT_PSM_MIN or psm > _RENT_PSM_MAX:
                continue
            if is_kyiv_city or district:
                rent_city.append(psm)
            if district in rent_by:
                rent_by[district].append(psm)
            opex = resolve_listing_opex(lst)
            if opex == "with":
                if is_kyiv_city or district:
                    rent_with_city.append(psm)
                if district in rent_with_by:
                    rent_with_by[district].append(psm)
            elif opex == "without":
                if is_kyiv_city or district:
                    rent_without_city.append(psm)
                if district in rent_without_by:
                    rent_without_by[district].append(psm)
            else:
                if is_kyiv_city or district:
                    rent_unknown_city.append(psm)
                if district in rent_unknown_by:
                    rent_unknown_by[district].append(psm)

        return {
            "sale": _slice_from_buckets("sale", sale_city, sale_by),
            "rent": _slice_from_buckets("rent", rent_city, rent_by),
            "rent_without_opex": _slice_from_buckets(
                "rent", rent_without_city, rent_without_by
            ),
            "rent_with_opex": _slice_from_buckets("rent", rent_with_city, rent_with_by),
            "rent_opex_unknown": _slice_from_buckets(
                "rent", rent_unknown_city, rent_unknown_by
            ),
        }

    # Shared by sale/rent UI toggle — keep warm across mode switches.
    return cache_get("market_stats_all", 180.0, _build)


def pick_rent_market_slice(market: dict, opex: str | None = None) -> MarketSlice:
    """Default rent view: without OPEX if enough sample, else mixed."""
    if opex == "with":
        return market["rent_with_opex"]
    if opex == "without":
        return market["rent_without_opex"]
    if opex == "unknown":
        return market["rent_opex_unknown"]
    # auto: prefer net rent if we have a usable sample
    net = market["rent_without_opex"]
    if (net.city_count or 0) >= 15:
        return net
    return market["rent"]


def count_active_inventory(db: Session) -> dict:
    """How many active listings are for sale vs rent (city + by district)."""
    from app.domain.ttl_cache import cache_get

    def _build() -> dict:
        sale_by = {d: 0 for d in KYIV_DISTRICTS}
        rent_by = {d: 0 for d in KYIV_DISTRICTS}
        sale_total = 0
        rent_total = 0
        sale_no_district = 0
        rent_no_district = 0

        q = select(
            Listing.deal_type,
            Listing.district,
            Listing.address_raw,
            Listing.title,
            Listing.city,
        ).where(Listing.status.in_(["active", "relisted"]))
        for deal_type, district_raw, address_raw, title, city in db.execute(q):
            district = normalize_district(district_raw) or extract_district(
                address_raw, title, city
            )
            if deal_type == "sale":
                sale_total += 1
                if district in sale_by:
                    sale_by[district] += 1
                else:
                    sale_no_district += 1
            elif deal_type == "rent":
                rent_total += 1
                if district in rent_by:
                    rent_by[district] += 1
                else:
                    rent_no_district += 1

        districts = []
        for name in KYIV_DISTRICTS:
            s, r = sale_by[name], rent_by[name]
            if s or r:
                districts.append({"district": name, "sale": s, "rent": r})

        return {
            "sale_total": sale_total,
            "rent_total": rent_total,
            "sale_no_district": sale_no_district,
            "rent_no_district": rent_no_district,
            "districts": districts,
        }

    return cache_get("inventory_active", 180.0, _build)


def rough_yield_by_district(market: dict) -> dict:
    """
    Gross yield proxy: (median rent $/m²·мес × 12) / (median sale $/m²) × 100.
    No OPEX/vacancy — labelled as rough in UI.
    """
    sale_by = {d.district: d for d in market["sale"].districts}
    rent_slice = pick_rent_market_slice(market)
    rent_by = {d.district: d for d in rent_slice.districts}
    rows: list[dict] = []
    for name in KYIV_DISTRICTS:
        s = sale_by.get(name)
        r = rent_by.get(name)
        if not s or not r:
            continue
        if not s.median_psm or not r.median_psm or s.median_psm <= 0:
            continue
        yield_pct = round((r.median_psm * 12.0) / s.median_psm * 100.0, 1)
        payback = round(100.0 / yield_pct, 1) if yield_pct > 0 else None
        rows.append(
            {
                "district": name,
                "sale_median_psm": s.median_psm,
                "rent_median_psm": r.median_psm,
                "sale_n": s.count,
                "rent_n": r.count,
                "yield_pct": yield_pct,
                "payback_years": payback,
            }
        )
    rows.sort(key=lambda x: (-x["yield_pct"], x["district"]))

    city_sale = market["sale"].city_median_psm
    city_rent = rent_slice.city_median_psm
    city_yield = None
    city_payback = None
    if city_sale and city_rent and city_sale > 0:
        city_yield = round((city_rent * 12.0) / city_sale * 100.0, 1)
        city_payback = round(100.0 / city_yield, 1) if city_yield > 0 else None

    return {
        "rows": rows,
        "city_yield_pct": city_yield,
        "city_payback_years": city_payback,
        "note": "Грубо: аренда×12 / цена продажи по медиане $/м². Без OPEX, простоя и налогов.",
    }
