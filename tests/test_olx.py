from __future__ import annotations

import json

from app.domain.enums import DealType
from app.scrapers.olx import OLX_SEARCH, OlxScraper


def _sample_state(ad: dict) -> str:
    payload = {"listing": {"listing": {"ads": [ad]}}}
    inner = json.dumps(payload, ensure_ascii=True)
    escaped = inner.replace("\\", "\\\\").replace('"', '\\"')
    return f'<html><script>window.__PRERENDERED_STATE__="{escaped}";</script></html>'


def test_olx_search_urls_updated():
    sale = OLX_SEARCH[DealType.SALE][0]
    rent = OLX_SEARCH[DealType.RENT][0]
    assert "prodazha-kommercheskoy-nedvizhimosti" in sale
    assert "arenda-kommercheskoy-nedvizhimosti" in rent
    assert "pomescheniy" not in sale


def test_parse_prerendered_state_extracts_price_and_id():
    ad = {
        "id": 931580177,
        "title": "Офіс 100 м² центр",
        "url": "https://www.olx.ua/d/uk/obyavlenie/test-ID112OyJ.html",
        "description": "Оренда офісу",
        "status": "active",
        "params": [
            {"key": "total_area", "normalizedValue": "91.5"},
            {"key": "floor", "normalizedValue": "1"},
        ],
        "price": {
            "regularPrice": {"value": 79999, "currencyCode": "UAH"},
        },
        "location": {
            "cityName": "\u041a\u0438\u0457\u0432",
            "districtName": "\u0428\u0435\u0432\u0447\u0435\u043d\u043a\u0456\u0432\u0441\u044c\u043a\u0438\u0439",
        },
        "map": {"lat": 50.47, "lon": 30.46},
        "user": {"name": "Тест"},
    }
    html = _sample_state(ad)
    items = OlxScraper()._parse_list(html, DealType.RENT)
    assert len(items) == 1
    x = items[0]
    assert x.external_id == "931580177"
    assert x.price == 79999
    assert x.currency == "UAH"
    assert x.area_sqm == 91.5
    assert x.floor == 1
    assert "\u0428\u0435\u0432" in (x.address_raw or "")


def test_tls_impersonate_enabled_for_olx(monkeypatch):
    monkeypatch.setattr(
        "app.scrapers.http_utils.get_settings",
        lambda: type(
            "S",
            (),
            {
                "http_timeout_sec": 5.0,
                "http_verify_ssl": False,
                "http_proxy": None,
                "user_agent": "",
                "crawl_human_mode": False,
                "crawl_warmup": False,
                "crawl_host_min_interval_sec": 0.0,
                "crawl_tls_impersonate": "chrome131",
            },
        )(),
    )
    from app.scrapers.http_utils import HttpClient

    client = HttpClient()
    assert client._use_tls_impersonate("https://www.olx.ua/uk/nedvizhimost/")
    assert not client._use_tls_impersonate("https://rieltor.ua/x")
    client.close()
