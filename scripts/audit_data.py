"""One-off data quality audit for REM."""
from __future__ import annotations

from sqlalchemy import func, select

from app.db import get_session_factory, init_db
from app.db.models import Listing
from app.domain.pricing import effective_listing_psm_usd
from app.domain.signals import activity_summary
from app.domain.ttl_cache import cache_clear
from app.scrapers.http_utils import is_kyiv_region_url
from fastapi.testclient import TestClient
from app.main import app


def main() -> None:
    init_db()
    db = get_session_factory()()
    issues: dict[str, object] = {}

    bad_psm_field = 0
    for lst in db.scalars(
        select(Listing).where(
            Listing.price.is_not(None),
            Listing.price_per_sqm.is_not(None),
            Listing.area_sqm.is_not(None),
        )
    ):
        p, psm, a = float(lst.price), float(lst.price_per_sqm), float(lst.area_sqm)
        if a > 0 and abs(psm - p) / max(p, 1) < 0.02:
            bad_psm_field += 1
    issues["price_per_sqm_equals_total"] = bad_psm_field

    rent_high = 0
    rent_samples: list[tuple] = []
    for lst in db.scalars(
        select(Listing).where(
            Listing.deal_type == "rent",
            Listing.status.in_(["active", "relisted"]),
            Listing.price.is_not(None),
            Listing.area_sqm.is_not(None),
        )
    ):
        psm = effective_listing_psm_usd(
            lst.price,
            lst.currency,
            lst.area_sqm,
            deal_type="rent",
            price_per_sqm=lst.price_per_sqm,
        )
        if psm and psm > 70:
            rent_high += 1
            if len(rent_samples) < 5:
                rent_samples.append(
                    (lst.id, lst.source, lst.price, lst.currency, lst.area_sqm, lst.price_per_sqm, round(psm, 1))
                )
    issues["rent_active_psm_over_70"] = rent_high

    sale_bad = 0
    for lst in db.scalars(
        select(Listing).where(
            Listing.deal_type == "sale",
            Listing.status.in_(["active", "relisted"]),
            Listing.price.is_not(None),
            Listing.area_sqm.is_not(None),
        )
    ):
        psm = effective_listing_psm_usd(
            lst.price,
            lst.currency,
            lst.area_sqm,
            deal_type="sale",
            price_per_sqm=lst.price_per_sqm,
        )
        if psm and (psm < 450 or psm > 10000):
            sale_bad += 1
    issues["sale_active_psm_out_of_band"] = sale_bad

    suspicious_rent = (
        db.scalar(
            select(func.count())
            .select_from(Listing)
            .where(Listing.deal_type == "rent", Listing.raw_extra.like('%"price_suspicious": true%'))
        )
        or 0
    )
    issues["rent_price_suspicious_flagged"] = suspicious_rent

    cache_clear()
    sale_a = activity_summary(db, hours=24, deal_type="sale")
    rent_a = activity_summary(db, hours=24, deal_type="rent")
    issues["activity_price_drops_sale"] = sale_a["price_drops"]
    issues["activity_price_drops_rent"] = rent_a["price_drops"]

    no_area = (
        db.scalar(
            select(func.count())
            .select_from(Listing)
            .where(
                Listing.status.in_(["active", "relisted"]),
                Listing.price.is_not(None),
                Listing.area_sqm.is_(None),
            )
        )
        or 0
    )
    issues["active_with_price_no_area"] = no_area

    non_kyiv = 0
    for lst in db.scalars(
        select(Listing).where(Listing.source == "domria", Listing.status.in_(["active", "relisted"]))
    ):
        if lst.url and not is_kyiv_region_url(lst.url):
            non_kyiv += 1
    issues["domria_non_kyiv_active"] = non_kyiv

    db.close()

    c = TestClient(app)
    routes = [
        "/?deal_type=sale",
        "/?deal_type=rent",
        "/?deal_type=rent&activity=price_drop",
        "/market?mode=rent",
        "/stats?mode=sale",
    ]
    http: dict[str, int] = {}
    for path in routes:
        http[path] = c.get(path).status_code

    print("=== DB AUDIT ===")
    for k, v in issues.items():
        print(f"{k}: {v}")
    if rent_samples:
        print("rent_high_samples:", rent_samples)
    print("=== HTTP ===")
    for path, code in http.items():
        print(f"{code} {path}")

    problems = []
    if bad_psm_field:
        problems.append("price_per_sqm still equals total")
    if any(code != 200 for code in http.values()):
        problems.append("HTTP errors")
    if problems:
        print("PROBLEMS:", problems)
    else:
        print("PROBLEMS: none critical in automated checks")


if __name__ == "__main__":
    main()
