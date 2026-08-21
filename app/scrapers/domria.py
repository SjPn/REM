from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterator
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.config import get_settings
from app.domain.enums import DealType, SourceName
from app.scrapers.base import RawListing
from app.scrapers.detail import (
    detect_listing_status,
    extract_floor_area_from_text,
    extract_phones,
    first_offer,
    geo_from_json_ld,
    iter_json_ld_types,
    load_json_ld,
    og_meta,
    parse_offer_price,
    postal_address_to_str,
)
from app.scrapers.enrich import enrich_listings
from app.scrapers.http_utils import (
    HttpClient,
    guess_property_type,
    is_kyiv_region_url,
    parse_area,
    parse_price,
    sleep_crawl_delay,
)

logger = logging.getLogger(__name__)

DOMRIA_SEARCH = {
    DealType.RENT: [
        "https://dom.ria.com/uk/arenda-ofisov/",
        "https://dom.ria.com/uk/arenda-kommercheskih-pomescheniy/",
        "https://dom.ria.com/uk/arenda-kom-nedvizhimosti/",
    ],
    DealType.SALE: [
        "https://dom.ria.com/uk/prodazha-ofisov/",
        "https://dom.ria.com/uk/prodazha-kommercheskih-pomescheniy/",
        "https://dom.ria.com/uk/prodazha-kom-nedvizhimosti/",
    ],
}


class DomriaScraper:
    source = SourceName.DOMRIA.value

    def __init__(self, client: HttpClient | None = None) -> None:
        self.client = client or HttpClient()
        self.settings = get_settings()

    def crawl(self, max_pages: int | None = None) -> Iterator[RawListing]:
        pages = max_pages or self.settings.crawl_max_pages
        for deal_type, bases in DOMRIA_SEARCH.items():
            batch: list[RawListing] = []
            for base_url in bases:
                for page in range(1, pages + 1):
                    url = base_url if page == 1 else f"{base_url}?page={page}"
                    logger.info("DOM.RIA fetch %s", url)
                    try:
                        html = self.client.get_text(url)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("DOM.RIA page failed %s: %s", url, exc)
                        break
                    items = self._parse_list(html, deal_type)
                    if not items:
                        break
                    batch.extend(items)
                    sleep_crawl_delay()
            yield from enrich_listings(self, batch)

    def fetch_detail(self, listing: RawListing) -> RawListing:
        html = self.client.get_text(listing.url)
        return self.parse_detail(html, listing)

    def parse_detail(self, html: str, listing: RawListing) -> RawListing:
        blocks = load_json_ld(html)
        title = og_meta(html, "og:title") or listing.title
        description = None
        phones = extract_phones(html)
        status = detect_listing_status(html, blocks)
        address = listing.address_raw
        district = listing.district
        city = listing.city or "Київ"
        lat, lon = listing.lat, listing.lon
        price, currency = listing.price, listing.currency
        area = listing.area_sqm
        floor = listing.floor
        agency = listing.agency

        for block in iter_json_ld_types(blocks, "Product", "LocalBusiness"):
            if block.get("name"):
                title = str(block["name"])[:500]
            if block.get("description"):
                description = str(block["description"])
            if block.get("address"):
                address = postal_address_to_str(block.get("address")) or str(
                    block.get("address")
                )
            g_lat, g_lon = geo_from_json_ld(block.get("geo"))
            if g_lat is not None:
                lat, lon = g_lat, g_lon
            offer = first_offer(block)
            if offer:
                p, c = parse_offer_price(offer)
                if p is not None:
                    price, currency = p, (c or currency)
                if offer.get("areaServed") and isinstance(offer["areaServed"], str):
                    address = address or offer["areaServed"]
                    if "район" in offer["areaServed"].lower() or "район" in offer["areaServed"]:
                        # e.g. "... район Дарницкий ..."
                        m = re.search(
                            r"район\s+([A-Za-zА-Яа-яІіЇїЄєҐґ'’\-]+)",
                            offer["areaServed"],
                            re.I,
                        )
                        if m:
                            district = m.group(1)
                seller = offer.get("seller")
                if isinstance(seller, dict) and seller.get("name"):
                    agency = str(seller["name"])
                avail = str(offer.get("availability") or "")
                if "SoldOut" in avail or "OutOfStock" in avail:
                    status = status or "sold_or_unavailable"
                elif "InStock" in avail:
                    status = status or "active"
                item_offered = offer.get("itemOffered")
                if isinstance(item_offered, str):
                    area2, floor2 = extract_floor_area_from_text(item_offered)
                    area = area or area2
                    floor = floor if floor is not None else floor2

        area2, floor2 = extract_floor_area_from_text(title, description)
        area = area or area2
        floor = floor if floor is not None else floor2

        listing.title = title
        listing.description = description
        listing.address_raw = address
        listing.district = district
        listing.city = city
        listing.lat = lat
        listing.lon = lon
        listing.price = price
        listing.currency = currency
        listing.area_sqm = area
        listing.floor = floor
        listing.agency = agency
        listing.phone = phones[0] if phones else listing.phone
        listing.source_status_raw = status or listing.source_status_raw
        listing.property_type = guess_property_type(f"{title or ''} {description or ''}")
        listing.extra = {**(listing.extra or {}), "detail": True, "phones": phones, "status": status}
        return listing

    def _parse_list(self, html: str, deal_type: DealType) -> list[RawListing]:
        soup = BeautifulSoup(html, "lxml")
        seen: set[str] = set()
        items: list[RawListing] = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "realty-" not in href and not re.search(r"realty.*\d{6,}", href):
                continue
            url = urljoin("https://dom.ria.com", href).split("?")[0]
            if not is_kyiv_region_url(url):
                continue
            ext_id = self._extract_id(url)
            if not ext_id or ext_id in seen:
                continue
            seen.add(ext_id)

            title = a.get_text(" ", strip=True)
            if len(title) < 5:
                continue

            # Use ONLY the anchor text for list-level fields to avoid parent-card pollution
            # (DOM.RIA wraps many cards; walking up parents merges neighbors → fake dupes).
            price, currency = parse_price(title)
            area = parse_area(title)
            address = self._guess_address(title) or title[:180]

            items.append(
                RawListing(
                    source=self.source,
                    external_id=ext_id,
                    url=url,
                    deal_type=deal_type.value,
                    title=title[:500],
                    property_type=guess_property_type(f"{title} {url}"),
                    price=price,
                    currency=currency or ("USD" if deal_type == DealType.SALE else "UAH"),
                    area_sqm=area,
                    address_raw=address,
                    city="Київ",
                    extra={"list_only": True},
                )
            )
        if not items:
            logger.warning("DOM.RIA: no listing cards parsed")
        return items

    @staticmethod
    def _extract_id(url: str) -> str | None:
        m = re.search(r"(\d{6,})(?:\.html)?(?:$|\?)", url)
        return m.group(1) if m else None

    @staticmethod
    def _guess_address(text: str) -> str | None:
        m = re.search(
            r"((?:вул\.|просп\.|пр-т|б-р|проспект|улица)\s+[^\d,]{2,40},?\s*\d+\w?)",
            text,
            re.IGNORECASE,
        )
        return m.group(1).strip() if m else None
