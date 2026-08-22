from __future__ import annotations

import logging
import random
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
    og_meta,
)
from app.scrapers.enrich import enrich_listings
from app.scrapers.http_utils import HttpClient, guess_property_type, parse_area, parse_floor, parse_price, parse_price_per_sqm

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

    def crawl(
        self,
        max_pages: int | None = None,
        needs_detail: Callable[[RawListing], bool] | None = None,
    ) -> Iterator[RawListing]:
        pages = max_pages or self.settings.crawl_max_pages
        deal_items = list(RIELTOR_SEARCH.items())
        if self.settings.crawl_human_mode:
            random.shuffle(deal_items)
        for deal_type, bases in deal_items:
            batch: list[RawListing] = []
            urls = list(bases)
            if self.settings.crawl_human_mode:
                random.shuffle(urls)
            for base_url in urls:
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
            yield from enrich_listings(self, batch, needs_detail=needs_detail)

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
        psm, _ = parse_price_per_sqm(title or "")
        if psm is None:
            psm, _ = parse_price_per_sqm(text[:1500])
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
        listing.price_per_sqm = psm if psm is not None else listing.price_per_sqm
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
        cards = soup.select("div.catalog-card[data-catalog-item-id]")
        if cards:
            return self._parse_catalog_cards(cards, deal_type)
        return self._parse_list_links(soup, deal_type)

    def _parse_catalog_cards(self, cards, deal_type: DealType) -> list[RawListing]:
        items: list[RawListing] = []
        for card in cards:
            ext_id = (card.get("data-catalog-item-id") or "").strip()
            if not ext_id:
                continue
            link = card.select_one("a[href*='/commercials-'][href*='/view/']")
            if not link or not link.get("href"):
                continue
            url = urljoin("https://rieltor.ua", link["href"]).split("?")[0]

            price_text = (card.get("data-label") or "").strip()
            price_el = card.select_one(".catalog-card-price-title")
            if price_el:
                price_text = price_el.get_text(" ", strip=True) or price_text
            details_el = card.select_one(".catalog-card-price-details")
            psm_text = details_el.get_text(" ", strip=True) if details_el else ""

            price, currency = parse_price(price_text)
            psm, _psm_cur = parse_price_per_sqm(psm_text or price_text)

            local = card.get_text(" ", strip=True)[:600]
            area = parse_area(local)
            floor = parse_floor(local)
            phones = extract_phones(local)

            addr_el = card.select_one(".catalog-card-address")
            address = addr_el.get_text(" ", strip=True) if addr_el else None
            desc_el = card.select_one(".catalog-card-description")
            desc = desc_el.get_text(" ", strip=True) if desc_el else ""
            title = " ".join(x for x in (address, desc) if x).strip()[:500] or f"RIELTOR {ext_id}"

            items.append(
                RawListing(
                    source=self.source,
                    external_id=ext_id,
                    url=url,
                    deal_type=deal_type.value,
                    title=title,
                    property_type=guess_property_type(f"{title} {local}"),
                    price=price,
                    currency=currency or ("USD" if deal_type == DealType.SALE else "UAH"),
                    price_per_sqm=psm,
                    area_sqm=area,
                    floor=floor,
                    address_raw=address or self._extract_address(local),
                    district=self._extract_district(local),
                    city="Київ",
                    phone=phones[0] if phones else None,
                    extra={"list_only": True},
                )
            )
        if not items:
            logger.warning("RIELTOR: catalog cards found but none parsed")
        return items

    def _parse_list_links(self, soup: BeautifulSoup, deal_type: DealType) -> list[RawListing]:
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
            parent = a.parent
            local = " ".join(
                t for t in [(title or ""), parent.get_text(" ", strip=True) if parent else ""] if t
            )[:400]
            if len(title or "") < 3:
                title = local[:180] or f"RIELTOR {ext_id}"
            price, currency = parse_price(local)
            psm, _psm_cur = parse_price_per_sqm(local)
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
                    price_per_sqm=psm,
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
