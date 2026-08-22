import pytest

from app.domain.pricing import normalize_listing_price, sale_psm_suspicious, sanitize_price_per_sqm
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


def test_parse_price_rieltor_photo_count_prefix():
    """Photo counter '17' must not glue to '250 000 $'."""
    blob = "17 17 250 000 $ 2 790 $/м² 90 м² Фортечний тупик"
    price, cur = parse_price(blob)
    assert cur == "USD"
    assert price == 250_000
    psm, _ = parse_price_per_sqm(blob)
    assert psm == 2790


def test_parse_price_prefers_usd_over_nbu_uah():
    blob = "250 000 $ 1 645 $/м² Київ 152 м² 11 207 750грн"
    price, cur = parse_price(blob)
    assert cur == "USD"
    assert price == 250_000


def test_strip_leading_price_junk():
    from app.scrapers.http_utils import strip_leading_price_junk

    raw = "17 17 250 000 $ 2 790 $/м² Фортечний тупик, 6/8"
    assert strip_leading_price_junk(raw).startswith("Фортечний")


def test_normalize_rieltor_glued_total():
    norm = normalize_listing_price(
        price=17_250_000,
        currency="USD",
        area_sqm=90,
        deal_type="sale",
        price_per_sqm=2790,
        title="17 17 250 000 $ 2 790 $/м² Фортечний тупик 90 м²",
    )
    assert norm.price == pytest.approx(250_000, rel=0.01)
    assert norm.price_per_sqm == pytest.approx(2790, rel=0.01)
    assert not norm.suspicious_psm


def test_repair_absurd_from_text_psm_when_field_is_wrong():
    norm = normalize_listing_price(
        price=22_120_000,
        currency="USD",
        area_sqm=60,
        deal_type="sale",
        price_per_sqm=368_666,
        title="22 22 120 000 $ 2 000 $/м² вул. Тестова",
    )
    assert norm.price == pytest.approx(120_000, rel=0.01)
    assert norm.price_per_sqm == pytest.approx(2000, rel=0.01)
    assert not norm.suspicious_psm


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


def test_sanitize_price_per_sqm_when_field_equals_total():
    from app.domain.pricing import sanitize_price_per_sqm

    fixed = sanitize_price_per_sqm(
        price=781,
        currency="USD",
        area_sqm=140,
        deal_type="rent",
        price_per_sqm=781,
    )
    assert fixed == pytest.approx(5.5786, rel=1e-3)


def test_effective_listing_psm_usd_ignores_mislabeled_field():
    from app.domain.pricing import effective_listing_psm_usd

    psm = effective_listing_psm_usd(
        781,
        "USD",
        140,
        deal_type="rent",
        price_per_sqm=781,
    )
    assert psm == pytest.approx(5.58, rel=0.01)


def test_maybe_fix_rent_currency_usd_labelled_uah():
    from app.domain.pricing import maybe_fix_rent_currency

    price, cur = maybe_fix_rent_currency(559260, "USD", 286, "rent")
    assert cur == "UAH"
    assert price == 559260


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
