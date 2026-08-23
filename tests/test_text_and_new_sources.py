from __future__ import annotations

from app.domain.enums import DealType
from app.scrapers.domria import DOMRIA_SEARCH
from app.scrapers.m2bomber import M2BOMBER_SEARCH, M2BomberScraper
from app.scrapers.olx import OlxScraper
from app.scrapers.text_fix import decode_js_escaped_json, fix_mojibake, looks_like_mojibake


def test_fix_mojibake_ukrainian_title():
    bad = "ÐÑÐ¾Ð´Ð°Ð¶ ÐºÐ¾Ð¼ÐµÑÑÑÐ¹Ð½Ð¾Ð³Ð¾"
    # Use a real latin-1 mangled sample
    good = "Продаж комерційного"
    mangled = good.encode("utf-8").decode("latin-1")
    assert looks_like_mojibake(mangled)
    assert fix_mojibake(mangled) == good


def test_decode_js_escaped_json_keeps_cyrillic():
    import json

    payload = {"listing": {"listing": {"ads": [{"title": "Продаж офісу", "id": 1}]}}}
    # Simulate JS string escaping of JSON
    inner = json.dumps(payload, ensure_ascii=False)
    escaped = inner.replace("\\", "\\\\").replace('"', '\\"')
    data = json.loads(decode_js_escaped_json(escaped))
    assert data["listing"]["listing"]["ads"][0]["title"] == "Продаж офісу"


def test_olx_prerendered_mojibake_roundtrip():
    import json

    ad = {
        "id": 111,
        "title": "Продаж офісу 100 м²",
        "url": "https://www.olx.ua/d/uk/obyavlenie/test-ID1.html",
        "price": {"regularPrice": {"value": 100000, "currencyCode": "UAH"}},
        "location": {"cityName": "Київ", "districtName": "Печерський"},
        "params": [],
        "map": {},
        "user": {"name": "Тест"},
    }
    payload = {"listing": {"listing": {"ads": [ad]}}}
    inner = json.dumps(payload, ensure_ascii=False)
    escaped = inner.replace("\\", "\\\\").replace('"', '\\"')
    html = f'<html><script>window.__PRERENDERED_STATE__="{escaped}";</script></html>'
    items = OlxScraper()._parse_list(html, DealType.SALE)
    assert len(items) == 1
    assert items[0].title.startswith("Продаж")
    assert "Ð" not in items[0].title


def test_domria_urls_are_kyiv_scoped():
    for urls in DOMRIA_SEARCH.values():
        assert all("/kiev/" in u for u in urls)


def test_m2bomber_parse_cards():
    html = """
    <html><body>
      <div class="item-card-long">
        <a href="/obj/1397264909/view/commercial-sell/kiiv-11-421866/x-id-1">
          <div class="item-card-long-title">Продаж офісу 110 кв. м. на Печерську</div>
        </a>
        <div class="item-card-long-address">район Печерський, Київ • ID 34396503</div>
        <div class="item-card-long-price">295 000 $</div>
        <div class="item-card-long-rooms">110 м² 19/19 п</div>
        <div class="item-card-long-desc">Офіс у ЖК</div>
      </div>
    </body></html>
    """
    items = M2BomberScraper()._parse_list(html, DealType.SALE)
    assert len(items) == 1
    x = items[0]
    assert x.external_id == "1397264909"
    assert x.price == 295000
    assert x.currency == "USD"
    assert x.area_sqm == 110
    assert x.floor == 19
    assert "Печерськ" in (x.title or "")
    assert M2BOMBER_SEARCH[DealType.RENT][0].endswith("commercial-rent/kiiv-11-421866")
