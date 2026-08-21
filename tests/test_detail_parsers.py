from pathlib import Path

from app.scrapers.base import RawListing
from app.scrapers.detail import detect_listing_status, extract_phones
from app.scrapers.domria import DomriaScraper
from app.scrapers.lun import LunScraper

FIXTURES = Path(__file__).parent / "fixtures"


def test_lun_list_json_ld_and_ids():
    html = (FIXTURES / "lun_list.html").read_text(encoding="utf-8")
    items = LunScraper()._parse_list(html, deal_type=__import__("app.domain.enums", fromlist=["DealType"]).DealType.RENT, zone="kyiv")
    assert len(items) == 2
    assert items[0].external_id == "4717797520"
    assert items[0].area_sqm == 308
    assert items[0].price == 55000
    assert items[0].address_raw and "Бориспільська" in items[0].address_raw
    # swapped geo corrected to Kyiv-ish
    assert items[0].lat and 44 <= items[0].lat <= 54
    assert items[0].lon and 20 <= items[0].lon <= 40


def test_lun_detail_phone_and_district():
    html = (FIXTURES / "lun_detail.html").read_text(encoding="utf-8")
    base = RawListing(
        source="lun",
        external_id="4717797520",
        url="https://lun.ua/realty/4717797520",
        deal_type="rent",
        title="tmp",
    )
    detailed = LunScraper().parse_detail(html, base)
    assert detailed.phone == "0988453959" or detailed.phone.endswith("988453959")
    assert detailed.area_sqm == 308
    assert detailed.district == "Дарницький"


def test_domria_detail_enrichment():
    html = (FIXTURES / "domria_detail.html").read_text(encoding="utf-8")
    base = RawListing(
        source="domria",
        external_id="34361445",
        url="https://dom.ria.com/uk/realty-x-34361445.html",
        deal_type="rent",
    )
    detailed = DomriaScraper().parse_detail(html, base)
    assert detailed.price == 2043
    assert detailed.currency == "USD"
    assert detailed.agency == "Андрій"
    assert detailed.floor == 2
    assert detailed.lat and detailed.lon
    assert detailed.address_raw and "Бажана" in detailed.address_raw


def test_detect_sold_status():
    html = (FIXTURES / "status_sold.html").read_text(encoding="utf-8")
    assert detect_listing_status(html) == "sold"


def test_extract_phones():
    phones = extract_phones("call +38 (050) 111-22-33 or 380671234567")
    assert "0501112233" in phones
    assert "0671234567" in phones
