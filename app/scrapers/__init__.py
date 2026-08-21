from __future__ import annotations

from collections.abc import Iterator

from app.domain.enums import SourceName
from app.scrapers.base import RawListing
from app.scrapers.domria import DomriaScraper
from app.scrapers.lun import LunScraper
from app.scrapers.olx import OlxScraper
from app.scrapers.rieltor import RieltorScraper

SCRAPERS = {
    SourceName.LUN.value: LunScraper,
    SourceName.OLX.value: OlxScraper,
    SourceName.DOMRIA.value: DomriaScraper,
    SourceName.RIELTOR.value: RieltorScraper,
}


def get_scraper(name: str):
    cls = SCRAPERS.get(name)
    if not cls:
        raise KeyError(f"Unknown scraper: {name}. Available: {list(SCRAPERS)}")
    return cls()


def crawl_source(name: str, max_pages: int | None = None) -> Iterator[RawListing]:
    scraper = get_scraper(name)
    yield from scraper.crawl(max_pages=max_pages)


def crawl_all(max_pages: int | None = None) -> Iterator[tuple[str, RawListing]]:
    for name in SCRAPERS:
        for item in crawl_source(name, max_pages=max_pages):
            yield name, item
