from __future__ import annotations

import json
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
    first_offer,
    geo_from_json_ld,
    iter_json_ld_types,
    load_json_ld,
    og_meta,
    parse_offer_price,
)
from app.scrapers.enrich import enrich_listings
from app.scrapers.http_utils import HttpClient, guess_property_type, parse_area, parse_price
from app.scrapers.text_fix import clean_text, decode_js_escaped_json, fix_mojibake

logger = logging.getLogger(__name__)

# OLX moved from *-pomescheniy/* to *-kommercheskoy-nedvizhimosti/* (2025–2026).
OLX_SEARCH = {
    DealType.SALE: [
        "https://www.olx.ua/uk/nedvizhimost/kommercheskaya-nedvizhimost/prodazha-kommercheskoy-nedvizhimosti/kiev/",
        "https://www.olx.ua/uk/nedvizhimost/kiev/kommercheskaya-nedvizhimost/prodazha-kommercheskoy-nedvizhimosti/",
    ],
    DealType.RENT: [
        "https://www.olx.ua/uk/nedvizhimost/kommercheskaya-nedvizhimost/arenda-kommercheskoy-nedvizhimosti/kiev/",
        "https://www.olx.ua/uk/nedvizhimost/kiev/kommercheskaya-nedvizhimost/arenda-kommercheskoy-nedvizhimosti/",
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
                if not self.settings.crawl_tls_impersonate:
                    logger.error(
                        "OLX: все URL недоступны. Задайте CRAWL_TLS_IMPERSONATE=chrome131 "
                        "(CloudFront блокирует httpx) или HTTP_PROXY."
                    )
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
            if batch:
                logger.info("OLX %s: %s cards from list pages", deal_type.value, len(batch))
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
        items = self._parse_prerendered_state(html, deal_type)
        if items:
            return items
        items = self._parse_list_links(html, deal_type)
        if not items:
            logger.warning("OLX: no listing cards parsed (markup/anti-bot may block)")
        return items

    def _parse_prerendered_state(self, html: str, deal_type: DealType) -> list[RawListing]:
        m = re.search(r'window\.__PRERENDERED_STATE__=\s*"(\{.*\})"\s*;', html)
        if not m:
            return []
        try:
            raw = m.group(1)
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                # JS-escaped JSON inside a string: unicode_escape alone mangles UTF-8 Cyrillic.
                data = json.loads(decode_js_escaped_json(raw))
            ads = data.get("listing", {}).get("listing", {}).get("ads") or []
        except (json.JSONDecodeError, UnicodeError, KeyError, TypeError, ValueError):
            logger.warning("OLX: failed to decode __PRERENDERED_STATE__")
            return []
        items: list[RawListing] = []
        for ad in ads:
            if not isinstance(ad, dict):
                continue
            listing = self._ad_to_raw(ad, deal_type)
            if listing is not None:
                items.append(listing)
        return items

    def _ad_to_raw(self, ad: dict, deal_type: DealType) -> RawListing | None:
        ext_id = ad.get("id")
        url = ad.get("url") or ad.get("urlPath")
        title = clean_text(ad.get("title"), limit=500) or ""
        if not ext_id or not url or len(title) < 8:
            return None
        if not str(url).startswith("http"):
            url = urljoin("https://www.olx.ua", str(url))

        price_obj = ad.get("price") or {}
        regular = price_obj.get("regularPrice") or {}
        price = regular.get("value")
        currency = regular.get("currencyCode") or "UAH"

        area = None
        floor = None
        for param in ad.get("params") or []:
            if not isinstance(param, dict):
                continue
            key = param.get("key")
            norm = param.get("normalizedValue")
            if key == "total_area" and norm not in (None, ""):
                try:
                    area = float(norm)
                except (TypeError, ValueError):
                    area = parse_area(str(param.get("value") or ""))
            elif key == "floor" and norm not in (None, ""):
                try:
                    floor = int(float(norm))
                except (TypeError, ValueError):
                    floor = None

        loc = ad.get("location") or {}
        district = fix_mojibake(loc.get("districtName"))
        city = fix_mojibake(loc.get("cityName")) or "Київ"
        address_parts = [p for p in (district, city) if p]
        address_raw = ", ".join(address_parts) if address_parts else city

        geo = ad.get("map") or {}
        lat = geo.get("lat")
        lon = geo.get("lon")
        description = clean_text(ad.get("description"), limit=5000)

        return RawListing(
            source=self.source,
            external_id=str(ext_id),
            url=str(url).split("?")[0],
            deal_type=deal_type.value,
            title=title[:500],
            description=description,
            property_type=guess_property_type(f"{title} {url}"),
            price=float(price) if price is not None else None,
            currency=str(currency).upper() if currency else "UAH",
            area_sqm=area,
            floor=floor,
            address_raw=address_raw,
            city=city,
            district=district,
            lat=float(lat) if lat is not None else None,
            lon=float(lon) if lon is not None else None,
            extra={
                "snippet": title[:400],
                "olx_status": ad.get("status"),
                "seller": fix_mojibake((ad.get("user") or {}).get("name")),
            },
        )

    def _parse_list_links(self, html: str, deal_type: DealType) -> list[RawListing]:
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
