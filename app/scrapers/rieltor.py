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
    og_meta,
)
from app.scrapers.enrich import enrich_listings
from app.scrapers.http_utils import HttpClient, guess_property_type, parse_area, parse_floor, parse_price, sleep_crawl_delay

logger = logging.getLogger(__name__)

RIELTOR_SEARCH = {
    DealType.SALE: [
        "https://rieltor.ua/commercials-sale/office/",
        "https://rieltor.ua/commercials-sale/",
    ],
    DealType.RENT: [
        "https://rieltor.ua/commercials-rent/office/",
        "https://rieltor.ua/commercials-rent/",
    ],
}


class RieltorScraper:
    source = SourceName.RIELTOR.value

    def __init__(self, client: HttpClient | None = None) -> None:
        self.client = client or HttpClient()
        self.settings = get_settings()

    def crawl(self, max_pages: int | None = None) -> Iterator[RawListing]:
        pages = max_pages or self.settings.crawl_max_pages
        for deal_type, bases in RIELTOR_SEARCH.items():
            batch: list[RawListing] = []
            for base_url in bases:
                for page in range(1, pages + 1):
                    url = base_url if page == 1 else f"{base_url}?page={page}"
                    logger.info("RIELTOR fetch %s", url)
                    try:
                        html = self.client.get_text(url)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("RIELTOR page failed %s: %s", url, exc)
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
        title = og_meta(html, "og:title") or listing.title
        description = og_meta(html, "og:description") or listing.description
        phones = extract_phones(html)
        status = detect_listing_status(html)
        soup = BeautifulSoup(html, "lxml")
        text = soup.get_text(" ", strip=True)

        price, currency = parse_price(title or "")
        if price is None:
            price, currency = parse_price(text[:1500])
        area, floor = extract_floor_area_from_text(title, description, text[:3000])
        address = self._extract_address(title) or self._extract_address(text[:2000])
        district = self._extract_district(text[:2500])

        # Agency / realtor name heuristics
        agency = listing.agency
        m = re.search(r"(?:агентство|АН|рієлтор|риелтор)[:\s]+([^\n|]{3,60})", text, re.I)
        if m:
            agency = m.group(1).strip()

        listing.title = (title or listing.title or "")[:500] or listing.title
        listing.description = description
        listing.price = price if price is not None else listing.price
        listing.currency = currency or listing.currency
        listing.area_sqm = area or listing.area_sqm
        listing.floor = floor if floor is not None else listing.floor
        listing.address_raw = address or listing.address_raw
        listing.district = district or listing.district
        listing.city = listing.city or "Київ"
        listing.phone = phones[0] if phones else listing.phone
        listing.agency = agency
        listing.source_status_raw = status or listing.source_status_raw
        listing.property_type = guess_property_type(f"{listing.title or ''} {description or ''}")
        listing.extra = {**(listing.extra or {}), "detail": True, "phones": phones, "status": status}
        return listing

    def _parse_list(self, html: str, deal_type: DealType) -> list[RawListing]:
        soup = BeautifulSoup(html, "lxml")
        seen: set[str] = set()
        items: list[RawListing] = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            m = re.search(r"/commercials-(?:sale|rent)/view/(\d+)/?", href)
            if not m:
                continue
            ext_id = m.group(1)
            if ext_id in seen:
                continue
            seen.add(ext_id)
            url = urljoin("https://rieltor.ua", href).split("?")[0]
            title = a.get_text(" ", strip=True)
            # one level up only — enough for price chip, avoids merging neighbors
            parent = a.parent
            local = " ".join(
                t for t in [(title or ""), parent.get_text(" ", strip=True) if parent else ""] if t
            )[:400]
            if len(title or "") < 3:
                title = local[:180] or f"RIELTOR {ext_id}"
            price, currency = parse_price(local)
            area = parse_area(local)
            floor = parse_floor(local)
            phones = extract_phones(local)
            items.append(
                RawListing(
                    source=self.source,
                    external_id=ext_id,
                    url=url,
                    deal_type=deal_type.value,
                    title=(title or f"RIELTOR {ext_id}")[:500],
                    property_type=guess_property_type(f"{title} {url}"),
                    price=price,
                    currency=currency or ("USD" if deal_type == DealType.SALE else "UAH"),
                    area_sqm=area,
                    floor=floor,
                    address_raw=self._extract_address(title) or self._extract_address(local),
                    district=self._extract_district(local),
                    city="Київ",
                    phone=phones[0] if phones else None,
                    extra={"list_only": True},
                )
            )
        if not items:
            logger.warning("RIELTOR: no listing cards parsed")
        return items

    @staticmethod
    def _extract_address(text: str | None) -> str | None:
        if not text:
            return None
        m = re.search(
            r"((?:вул\.|просп\.|пр-т|б-р|улица|проспект)\s+[^\d,]{2,50},?\s*\d+\w?)",
            text,
            re.IGNORECASE,
        )
        if m:
            return m.group(1).strip()
        # title style: "Киевская обл., ..."
        m = re.search(r"(Ки[єе]в[^\|]{5,80})", text)
        return m.group(1).strip() if m else None

    @staticmethod
    def _extract_district(text: str | None) -> str | None:
        if not text:
            return None
        districts = [
            "Печерський",
            "Шевченківський",
            "Голосіївський",
            "Подільський",
            "Дарницький",
            "Дніпровський",
            "Солом'янський",
            "Оболонський",
            "Святошинський",
            "Деснянський",
        ]
        low = text.lower()
        for d in districts:
            if d.lower() in low:
                return d
        m = re.search(r"([А-Яа-яІіЇїЄєҐґ'’\-]+)\s*р-н", text)
        return m.group(1) if m else None
