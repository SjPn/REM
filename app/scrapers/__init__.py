from __future__ import annotations

from collections.abc import Callable, Iterator

from app.domain.enums import SourceName
from app.scrapers.base import RawListing
from app.scrapers.domria import DomriaScraper
from app.scrapers.http_utils import HttpClient
from app.scrapers.lun import LunScraper
from app.scrapers.olx import OlxScraper
from app.scrapers.rieltor import RieltorScraper

SCRAPERS = {
    SourceName.LUN.value: LunScraper,
    SourceName.OLX.value: OlxScraper,
    SourceName.DOMRIA.value: DomriaScraper,
    SourceName.RIELTOR.value: RieltorScraper,
}


def get_scraper(name: str, client: HttpClient | None = None):
    cls = SCRAPERS.get(name)
    if not cls:
        raise KeyError(f"Unknown scraper: {name}. Available: {list(SCRAPERS)}")
    return cls(client=client) if client is not None else cls()


def crawl_source(
    name: str,
    max_pages: int | None = None,
    client: HttpClient | None = None,
    needs_detail: Callable[[RawListing], bool] | None = None,
) -> Iterator[RawListing]:
    scraper = get_scraper(name, client=client)
    yield from scraper.crawl(max_pages=max_pages, needs_detail=needs_detail)


def crawl_all(max_pages: int | None = None) -> Iterator[tuple[str, RawListing]]:
    with HttpClient() as client:
        for name in SCRAPERS:
            for item in crawl_source(name, max_pages=max_pages, client=client):
                yield name, item
