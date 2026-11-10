from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app.domain.enums import DealType
from app.scrapers.olx import OLX_OFFICE_API, OLX_SEARCH, OlxScraper, office_feed_reached_lookback


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


def test_office_categories_are_separate_feeds():
    ids = {category_id for _deal, category_id in OLX_OFFICE_API}
    assert ids == {3681, 3690}


def test_offer_to_raw_reads_api_price_and_area():
    offer = {
        "id": 940311906,
        "url": "https://www.olx.ua/d/uk/obyavlenie/x-ID11Ds54.html",
        "title": "Офіс Саксаганського 70a",
        "description": "Ціна оренди - 15 $/м2",
        "status": "active",
        "business": True,
        "user": {"name": "Сергей"},
        "map": {"lat": 50.44, "lon": 30.50},
        "location": {"city": {"name": "Київ"}, "district": {"name": "Голосіївський"}},
        "params": [
            {"key": "price", "type": "price", "value": {"value": 15, "currency": "USD"}},
            {"key": "total_area", "value": {"key": "396.6", "label": "396.6 м²"}},
            {"key": "floor", "value": {"key": "4", "label": "4"}},
        ],
    }
    item = OlxScraper()._offer_to_raw(offer, DealType.RENT)
    assert item is not None
    assert item.external_id == "940311906"
    assert item.price == 15
    assert item.currency == "USD"
    assert item.area_sqm == 396.6
    assert item.floor == 4
    assert item.deal_type == "rent"
    assert "Голосіївський" in (item.address_raw or "")


def test_office_feed_stops_when_page_is_older_than_lookback():
    cutoff = datetime(2026, 11, 9, tzinfo=timezone.utc)
    assert office_feed_reached_lookback(["2026-11-08T18:00:00+02:00"], cutoff=cutoff)
    assert not office_feed_reached_lookback(
        ["2026-11-10T12:00:00+02:00", "2026-11-10T11:00:00+02:00"],
        cutoff=cutoff,
    )


def test_office_api_stops_after_old_page():
    now = datetime.now(timezone.utc)
    fresh = (now - timedelta(hours=1)).isoformat()
    old = (now - timedelta(hours=48)).isoformat()

    class Client:
        def __init__(self) -> None:
            self.offsets: list[int] = []

        def get_text(self, url: str, params=None) -> str:
            offset = int(params["offset"])
            self.offsets.append(offset)
            stamp = fresh if offset == 0 else old
            offer = {
                "id": offset + 1,
                "url": f"https://www.olx.ua/d/uk/obyavlenie/x-ID{offset}x.html",
                "title": "Оренда офісу тест",
                "last_refresh_time": stamp,
                "params": [
                    {"key": "price", "type": "price", "value": {"value": 1000, "currency": "USD"}}
                ],
                "location": {"city": {"name": "Київ"}},
            }
            return json.dumps(
                {
                    "data": [offer],
                    "metadata": {"promoted": []},
                    "links": {"next": {"href": f"https://www.olx.ua/api/v1/offers?offset={offset + 40}"}},
                }
            )

    client = Client()
    items = OlxScraper(client=client)._crawl_office_api(DealType.RENT, 3690)
    assert client.offsets == [0, 40]
    assert [item.external_id for item in items] == ["1", "41"]


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
