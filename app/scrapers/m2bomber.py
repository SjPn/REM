from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterator
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.config import get_settings
from app.domain.enums import DealType, SourceName
from app.scrapers.base import RawListing
from app.scrapers.detail import (
    detect_listing_status,
    extract_floor_area_from_text,
    extract_phones,
    og_meta,
)
from app.scrapers.enrich import enrich_listings
from app.scrapers.http_utils import HttpClient, guess_property_type, parse_area, parse_floor, parse_price
from app.scrapers.text_fix import clean_text

logger = logging.getLogger(__name__)

# Kyiv OSM id 421866 — commercial sale / rent catalogs.
M2BOMBER_SEARCH = {
    DealType.SALE: [
        "https://ua.m2bomber.com/commercial-sell/kiiv-11-421866",
    ],
    DealType.RENT: [
        "https://ua.m2bomber.com/commercial-rent/kiiv-11-421866",
    ],
}


class M2BomberScraper:
    source = SourceName.M2BOMBER.value

    def __init__(self, client: HttpClient | None = None) -> None:
        self.client = client or HttpClient()
        self.settings = get_settings()

    def crawl(
        self,
        max_pages: int | None = None,
        needs_detail: Callable[[RawListing], bool] | None = None,
    ) -> Iterator[RawListing]:
        pages = max_pages or self.settings.crawl_max_pages
        for deal_type, bases in M2BOMBER_SEARCH.items():
            batch: list[RawListing] = []
            for base_url in bases:
                for page in range(1, pages + 1):
                    url = base_url if page == 1 else f"{base_url}?page={page}"
                    logger.info("M2Bomber fetch %s", url)
                    try:
                        html = self.client.get_text(url)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("M2Bomber page failed %s: %s", url, exc)
                        break
                    items = self._parse_list(html, deal_type)
                    if not items:
                        break
                    batch.extend(items)
            if batch:
                logger.info("M2Bomber %s: %s cards from list pages", deal_type.value, len(batch))
            yield from enrich_listings(self, batch, needs_detail=needs_detail)

    def fetch_detail(self, listing: RawListing) -> RawListing:
        html = self.client.get_text(listing.url)
        return self.parse_detail(html, listing)

    def parse_detail(self, html: str, listing: RawListing) -> RawListing:
        title = clean_text(og_meta(html, "og:title"), limit=500) or listing.title
        description = clean_text(og_meta(html, "og:description"), limit=5000) or listing.description
        phones = extract_phones(html)
        status = detect_listing_status(html)
        soup = BeautifulSoup(html, "lxml")
        blob = soup.get_text(" ", strip=True)
        price, currency = parse_price(blob)
        area = listing.area_sqm or parse_area(blob)
        floor = listing.floor if listing.floor is not None else parse_floor(blob)
        area2, floor2 = extract_floor_area_from_text(title, description, blob)
        listing.title = title
        listing.description = description
        listing.price = price if price is not None else listing.price
        listing.currency = currency or listing.currency
        listing.area_sqm = area or area2
        listing.floor = floor if floor is not None else floor2
        listing.phone = phones[0] if phones else listing.phone
        listing.source_status_raw = status or listing.source_status_raw
        listing.city = listing.city or "Київ"
        listing.property_type = guess_property_type(f"{title or ''} {description or ''}")
        listing.extra = {**(listing.extra or {}), "detail": True, "phones": phones, "status": status}
        return listing

    def _parse_list(self, html: str, deal_type: DealType) -> list[RawListing]:
        soup = BeautifulSoup(html, "lxml")
        seen: set[str] = set()
        items: list[RawListing] = []
        cards = soup.select(".item-card-long")
        if not cards:
            logger.warning("M2Bomber: no listing cards parsed")
            return items

        for card in cards:
            a = card.select_one("a[href*='/obj/']")
            if not a or not a.get("href"):
                continue
            url = urljoin("https://ua.m2bomber.com", a["href"]).split("?")[0]
            ext_id = self._extract_id(url)
            if not ext_id or ext_id in seen:
                continue
            seen.add(ext_id)

            title_el = card.select_one(".item-card-long-title")
            title = clean_text(
                title_el.get_text(" ", strip=True) if title_el else a.get_text(" ", strip=True),
                limit=500,
            ) or ""
            if len(title) < 8:
                continue

            addr_el = card.select_one(".item-card-long-address")
            address = clean_text(addr_el.get_text(" ", strip=True) if addr_el else None, limit=500)
            price_el = card.select_one(".item-card-long-price")
            rooms_el = card.select_one(".item-card-long-rooms")
            desc_el = card.select_one(".item-card-long-desc, .item-card-long-description")
            price_text = price_el.get_text(" ", strip=True) if price_el else ""
            rooms_text = rooms_el.get_text(" ", strip=True) if rooms_el else ""
            desc = clean_text(desc_el.get_text(" ", strip=True) if desc_el else None, limit=5000)

            price, currency = parse_price(price_text)
            if price is None:
                price, currency = parse_price(card.get_text(" ", strip=True))
            area = parse_area(rooms_text) or parse_area(title)
            floor = parse_floor(rooms_text) or parse_floor(title)
            district = self._guess_district(address or title)

            items.append(
                RawListing(
                    source=self.source,
                    external_id=ext_id,
                    url=url,
                    deal_type=deal_type.value,
                    title=title[:500],
                    description=desc,
                    property_type=guess_property_type(f"{title} {desc or ''}"),
                    price=price,
                    currency=currency or ("USD" if deal_type == DealType.SALE else "UAH"),
                    area_sqm=area,
                    floor=floor,
                    address_raw=address or "Київ",
                    district=district,
                    city="Київ",
                    extra={"snippet": (title + " " + (address or ""))[:400]},
                )
            )
        return items

    @staticmethod
    def _extract_id(url: str) -> str | None:
        m = re.search(r"/obj/(\d+)/", url)
        return m.group(1) if m else None

    @staticmethod
    def _guess_district(text: str | None) -> str | None:
        if not text:
            return None
        m = re.search(
            r"район\s+([А-Яа-яІіЇїЄєҐґ'’\-]+(?:ський|ский|ський))",
            text,
            re.IGNORECASE,
        )
        return m.group(1).strip() if m else None
