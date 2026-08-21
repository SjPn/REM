from app.domain.market_stats import extract_district, normalize_district, to_usd


def test_normalize_district():
    assert normalize_district("Дарницький район") == "Дарницький"
    assert normalize_district("шевченковский") == "Шевченківський"


def test_extract_district_from_text():
    assert extract_district("Офіс, Печерський р-н") == "Печерський"


def test_to_usd():
    assert to_usd(4100, "UAH") == 100.0
    assert to_usd(100, "USD") == 100.0
