from app.domain.pricing import normalize_listing_price
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
    # polluted card text with many IDs must not become inf
    blob = "вул. Тестова 1 " + " ".join(str(i) for i in range(100000, 100050)) + " грн"
    price, cur = parse_price(blob)
    assert price is None or price < 1_000_000_000


def test_normalize_rent_price_that_is_actually_psm():
    # 15 USD for 467 m² → ~0.03 $/m² — treat as $/m²
    norm = normalize_listing_price(
        price=15,
        currency="USD",
        area_sqm=467,
        deal_type="rent",
        title="7 005 $/міс 15 $/м² Нижньоключова",
    )
    assert norm.reinterpreted_as_psm
    assert norm.price == 15 * 467
    assert norm.price_per_sqm == 15


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
