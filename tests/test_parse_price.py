from app.domain.pricing import normalize_listing_price, sale_psm_suspicious
from app.scrapers.http_utils import is_kyiv_region_url, parse_price, parse_price_per_sqm


def test_parse_price_currency_adjacent():
    price, cur = parse_price("Оренда офісу 2 500 $ / міс")
    assert cur == "USD"
    assert price == 2500


def test_parse_price_skips_per_sqm_chip():
    price, cur = parse_price("3 181 860 $ 495 $/м² Куренівський пров.")
    assert cur == "USD"
    assert price == 3_181_860


def test_parse_price_per_sqm():
    psm, cur = parse_price_per_sqm("21 095 $/міс 22 $/м² Миколи Грінченка")
    assert psm == 22
    assert cur == "USD"


def test_parse_price_rejects_digit_soup():
    blob = "вул. Тестова 1 " + " ".join(str(i) for i in range(100000, 100050)) + " грн"
    price, cur = parse_price(blob)
    assert price is None or price < 1_000_000_000


def test_normalize_rent_price_that_is_actually_psm():
    norm = normalize_listing_price(
        price=15,
        currency="USD",
        area_sqm=467,
        deal_type="rent",
        title="7 005 $/міс 15 $/м² Нижньоключова",
    )
    # title has both total and psm → trust text total
    assert not norm.reinterpreted_as_psm
    assert norm.price == 7005
    assert norm.price_per_sqm == 15


def test_normalize_expands_bare_psm_without_total():
    norm = normalize_listing_price(
        price=15,
        currency="USD",
        area_sqm=467,
        deal_type="rent",
        title="Оренда 15 $/м²",
    )
    assert norm.reinterpreted_as_psm
    assert norm.price == 15 * 467
    assert norm.price_per_sqm == 15


def test_normalize_does_not_multiply_monthly_total():
    # 31196 $/міс for 2166 m² ≈ 14 $/m² — already a total
    norm = normalize_listing_price(
        price=31196,
        currency="USD",
        area_sqm=2166,
        deal_type="rent",
        title="31 196 $/міс 14 $/м² Гетьмана",
    )
    assert not norm.reinterpreted_as_psm
    assert norm.price == 31196
    assert norm.price_per_sqm == 14


def test_normalize_leaves_sane_total():
    norm = normalize_listing_price(
        price=2500,
        currency="USD",
        area_sqm=100,
        deal_type="rent",
        title="Офіс 2500$",
    )
    assert not norm.reinterpreted_as_psm
    assert norm.price == 2500


def test_sale_expands_clear_psm_rate():
    norm = normalize_listing_price(
        price=1800,
        currency="USD",
        area_sqm=120,
        deal_type="sale",
        title="Продаж офісу",
    )
    # 1800/120 = 15 $/м² → stored figure is the rate
    assert norm.reinterpreted_as_psm
    assert norm.price == 1800 * 120
    assert not norm.suspicious_psm


def test_sale_high_psm_suspicious():
    norm = normalize_listing_price(
        price=2_500_000,
        currency="USD",
        area_sqm=100,
        deal_type="sale",
        title="Офіс 2.5 млн",
    )
    assert norm.suspicious_psm
    assert sale_psm_suspicious(25_000)


def test_kyiv_url_filter():
    assert is_kyiv_region_url(
        "https://dom.ria.com/uk/realty-arenda-ofisnye-pomescheniya-kiev-darnitskiy-x-1.html"
    )
    assert not is_kyiv_region_url(
        "https://dom.ria.com/uk/realty-prodaja-spetsialnoe-pomeschenie-lvov-bodnarovka-x-1.html"
    )
    assert not is_kyiv_region_url(
        "https://dom.ria.com/uk/realty-prodaja-ternopol-tsentr-x-1.html"
    )
