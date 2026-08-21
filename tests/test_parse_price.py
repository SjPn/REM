from app.scrapers.http_utils import is_kyiv_region_url, parse_price


def test_parse_price_currency_adjacent():
    price, cur = parse_price("Оренда офісу 2 500 $ / міс")
    assert cur == "USD"
    assert price == 2500


def test_parse_price_rejects_digit_soup():
    # polluted card text with many IDs must not become inf
    blob = "вул. Тестова 1 " + " ".join(str(i) for i in range(100000, 100050)) + " грн"
    price, cur = parse_price(blob)
    assert price is None or price < 1_000_000_000


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
