from __future__ import annotations

import logging
import re
import time
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
    first_offer,
    geo_from_json_ld,
    iter_json_ld_types,
    load_json_ld,
    og_meta,
    parse_offer_price,
)
from app.scrapers.enrich import enrich_listings
from app.scrapers.http_utils import HttpClient, guess_property_type, parse_area, parse_price

logger = logging.getLogger(__name__)

# OLX often blocks datacenter IPs; keep multiple URL candidates.
OLX_SEARCH = {
    DealType.SALE: [
        "https://www.olx.ua/uk/nedvizhimost/kommercheskaya-nedvizhimost/prodazha-pomescheniy/kiev/",
        "https://www.olx.ua/uk/nedvizhimost/kiev/kommercheskaya-nedvizhimost/prodazha-pomescheniy/",
    ],
    DealType.RENT: [
        "https://www.olx.ua/uk/nedvizhimost/kommercheskaya-nedvizhimost/arenda-pomescheniy/kiev/",
        "https://www.olx.ua/uk/nedvizhimost/kiev/kommercheskaya-nedvizhimost/arenda-pomescheniy/",
    ],
}


class OlxScraper:
    source = SourceName.OLX.value

    def __init__(self, client: HttpClient | None = None) -> None:
        self.client = client or HttpClient()
        self.settings = get_settings()

    def crawl(
        self,
        max_pages: int | None = None,
        needs_detail: Callable[[RawListing], bool] | None = None,
    ) -> Iterator[RawListing]:
        pages = max_pages or self.settings.crawl_max_pages
        for deal_type, bases in OLX_SEARCH.items():
            batch: list[RawListing] = []
            html = None
            base_url = bases[0]
            for candidate in bases:
                try:
                    html = self.client.get_text(candidate)
                    base_url = candidate
                    break
                except Exception as exc:  # noqa: BLE001
                    logger.warning("OLX list candidate failed %s: %s", candidate, exc)
            if not html:
                continue
            for page in range(1, pages + 1):
                url = base_url if page == 1 else f"{base_url}?page={page}"
                logger.info("OLX fetch %s", url)
                try:
                    page_html = html if page == 1 else self.client.get_text(url)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("OLX page failed %s: %s", url, exc)
                    break
                items = self._parse_list(page_html, deal_type)
                if not items:
                    break
                batch.extend(items)
            yield from enrich_listings(self, batch, needs_detail=needs_detail)

    def fetch_detail(self, listing: RawListing) -> RawListing:
        html = self.client.get_text(listing.url)
        return self.parse_detail(html, listing)

    def parse_detail(self, html: str, listing: RawListing) -> RawListing:
        blocks = load_json_ld(html)
        title = og_meta(html, "og:title") or listing.title
        description = og_meta(html, "og:description") or listing.description
        phones = extract_phones(html)
        status = detect_listing_status(html, blocks)
        address = listing.address_raw
        lat, lon = listing.lat, listing.lon
        price, currency = listing.price, listing.currency
        area = listing.area_sqm
        floor = listing.floor

        for block in iter_json_ld_types(blocks, "Product", "Offer", "Apartment", "Place", "LocalBusiness"):
            if block.get("name"):
                title = str(block["name"])[:500]
            if block.get("description"):
                description = str(block["description"])
            if block.get("address"):
                addr = block["address"]
                address = addr if isinstance(addr, str) else str(addr)
            g_lat, g_lon = geo_from_json_ld(block.get("geo"))
            if g_lat is not None:
                lat, lon = g_lat, g_lon
            offer = first_offer(block) or (block if block.get("@type") == "Offer" else None)
            p, c = parse_offer_price(offer)
            if p is not None:
                price, currency = p, c or currency

        area2, floor2 = extract_floor_area_from_text(title, description)
        listing.title = title
        listing.description = description
        listing.address_raw = address
        listing.lat = lat
        listing.lon = lon
        listing.price = price
        listing.currency = currency
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
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/d/" not in href and "ID" not in href:
                continue
            url = urljoin("https://www.olx.ua", href).split("?")[0]
            ext_id = self._extract_id(url)
            if not ext_id or ext_id in seen:
                continue
            seen.add(ext_id)
            card = a
            for _ in range(5):
                if card.parent:
                    card = card.parent
            text = card.get_text(" ", strip=True)
            title = a.get_text(" ", strip=True) or text[:180]
            if len(title) < 8:
                continue
            price, currency = parse_price(text)
            area = parse_area(text)
            items.append(
                RawListing(
                    source=self.source,
                    external_id=ext_id,
                    url=url,
                    deal_type=deal_type.value,
                    title=title[:500],
                    property_type=guess_property_type(f"{title} {url}"),
                    price=price,
                    currency=currency or "UAH",
                    area_sqm=area,
                    address_raw=self._guess_location(text),
                    city="Київ",
                    extra={"snippet": text[:400]},
                )
            )
        if not items:
            logger.warning("OLX: no listing cards parsed (markup/anti-bot may block)")
        return items

    @staticmethod
    def _extract_id(url: str) -> str | None:
        m = re.search(r"-ID([A-Za-z0-9]+)\.html", url)
        if m:
            return m.group(1)
        m = re.search(r"/(\d{6,})", url)
        return m.group(1) if m else None

    @staticmethod
    def _guess_location(text: str) -> str | None:
        m = re.search(r"(Київ|Киев|Київська область)[^|]{0,80}", text)
        return m.group(0).strip() if m else "Київ"
