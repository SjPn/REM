from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urljoin, urlsplit

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
from app.scrapers.http_utils import (
    HttpClient,
    PortalBlockedError,
    guess_property_type,
    parse_area,
    parse_price,
)
from app.scrapers.paging import iter_feed_pages
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

# Office lists are separate categories. The HTML "newest" order mixes promoted
# cards, so these feeds use the offers API, which sorts by last refresh.
# Sale: .../prodazha-kommercheskoy-nedvizhimosti/ofisy/kiev/ (category 3681)
# Rent: .../arenda-kommercheskoy-nedvizhimosti/ofisy/kiev/ (category 3690)
OLX_OFFICE_API = (
    (DealType.SALE, 3681),
    (DealType.RENT, 3690),
)
_OLX_KYIV_CITY_ID = 268
_OLX_OFFERS_URL = "https://www.olx.ua/api/v1/offers/"
_OFFICE_PAGE_SIZE = 40
_OFFICE_MAX_PAGES = 25
_OFFICE_LOOKBACK = timedelta(hours=36)


class OlxScraper:
    source = SourceName.OLX.value

    def __init__(self, client: HttpClient | None = None) -> None:
        self.client = client or HttpClient()
        self.settings = get_settings()

    def crawl(
        self,
        max_pages: int | None = None,
        needs_detail: Callable[[RawListing], bool] | None = None,
        *,
        stale_page_limit: int = 2,
        max_details: int | None = None,
    ) -> Iterator[RawListing]:
        pages = max_pages or self.settings.crawl_max_pages
        seen: set[str] = set()
        collected: list[RawListing] = []
        for deal_type, bases in OLX_SEARCH.items():
            batch = self._crawl_html_feed(
                deal_type, bases, pages, stale_page_limit=stale_page_limit
            )
            collected.extend(self._dedupe(batch, seen))
        for deal_type, category_id in OLX_OFFICE_API:
            try:
                batch = self._crawl_office_api(deal_type, category_id)
            except PortalBlockedError as exc:
                logger.warning("OLX offices %s blocked: %s", deal_type.value, exc)
                break
            except Exception as exc:  # noqa: BLE001
                logger.warning("OLX offices %s failed: %s", deal_type.value, exc)
                continue
            fresh = self._dedupe(batch, seen)
            if fresh:
                logger.info("OLX offices %s: %s cards", deal_type.value, len(fresh))
            collected.extend(fresh)
        if collected:
            yield from enrich_listings(
                self, collected, needs_detail=needs_detail, max_details=max_details
            )

    def _crawl_html_feed(
        self,
        deal_type: DealType,
        bases: list[str],
        pages: int,
        *,
        stale_page_limit: int,
    ) -> list[RawListing]:
        html = None
        base_url = bases[0]
        for candidate in bases:
            try:
                html = self.client.get_list_text(candidate)
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
            return []
        batch = iter_feed_pages(
            self.client,
            base_url,
            pages,
            lambda page_html, dt=deal_type: self._parse_list(page_html, dt),
            stale_page_limit=stale_page_limit,
            log_label="OLX",
            first_html=html,
        )
        if batch:
            logger.info("OLX %s: %s cards from list pages", deal_type.value, len(batch))
        return batch

    def _crawl_office_api(self, deal_type: DealType, category_id: int) -> list[RawListing]:
        """Newest office cards. Stop once the page is older than the lookback."""
        cutoff = datetime.now(timezone.utc) - _OFFICE_LOOKBACK
        offset = 0
        items: list[RawListing] = []
        for _page in range(_OFFICE_MAX_PAGES):
            payload = self._fetch_offers(category_id, offset)
            offers = payload.get("data") or []
            if not isinstance(offers, list) or not offers:
                break
            promoted = set((payload.get("metadata") or {}).get("promoted") or [])
            refresh_times: list[str] = []
            for index, offer in enumerate(offers):
                if not isinstance(offer, dict):
                    continue
                listing = self._offer_to_raw(offer, deal_type)
                if listing is not None:
                    items.append(listing)
                if index not in promoted:
                    stamp = offer.get("last_refresh_time")
                    if isinstance(stamp, str):
                        refresh_times.append(stamp)
            if office_feed_reached_lookback(refresh_times, cutoff=cutoff):
                break
            nxt = ((payload.get("links") or {}).get("next") or {}).get("href")
            if not nxt:
                break
            offset = _offset_from_href(str(nxt), fallback=offset + _OFFICE_PAGE_SIZE)
        return items

    def _fetch_offers(self, category_id: int, offset: int) -> dict:
        text = self.client.get_text(
            _OLX_OFFERS_URL,
            params={
                "offset": str(offset),
                "limit": str(_OFFICE_PAGE_SIZE),
                "category_id": str(category_id),
                "city_id": str(_OLX_KYIV_CITY_ID),
                "sort_by": "created_at:desc",
            },
        )
        data = json.loads(text)
        if not isinstance(data, dict):
            return {}
        return data

    def _offer_to_raw(self, offer: dict, deal_type: DealType) -> RawListing | None:
        params: list[dict] = []
        price = None
        currency = None
        for param in offer.get("params") or []:
            if not isinstance(param, dict):
                continue
            key = str(param.get("key") or "")
            if key == "price" or param.get("type") == "price":
                value = param.get("value") or {}
                if isinstance(value, dict):
                    price = value.get("value")
                    currency = value.get("currency")
                continue
            if key in ("total_area", "floor"):
                number = _olx_param_number(param.get("value"))
                if number is not None:
                    params.append({"key": key, "normalizedValue": number})
        user = offer.get("user") if isinstance(offer.get("user"), dict) else {}
        business = offer.get("business")
        geo = offer.get("map") if isinstance(offer.get("map"), dict) else {}
        loc = offer.get("location") if isinstance(offer.get("location"), dict) else {}
        city = loc.get("city") if isinstance(loc.get("city"), dict) else {}
        district = loc.get("district") if isinstance(loc.get("district"), dict) else {}
        ad = {
            "id": offer.get("id"),
            "url": offer.get("url"),
            "title": offer.get("title"),
            "description": offer.get("description"),
            "status": offer.get("status"),
            "price": {"regularPrice": {"value": price, "currencyCode": currency or "UAH"}},
            "params": params,
            "location": {
                "cityName": city.get("name"),
                "districtName": district.get("name"),
            },
            "map": {"lat": geo.get("lat"), "lon": geo.get("lon")},
            "user": {"name": user.get("name")},
            "isBusiness": business if isinstance(business, bool) else None,
        }
        return self._ad_to_raw(ad, deal_type)

    @staticmethod
    def _dedupe(items: list[RawListing], seen: set[str]) -> list[RawListing]:
        fresh: list[RawListing] = []
        for item in items:
            ext = str(item.external_id or "").strip()
            if not ext or ext in seen:
                continue
            seen.add(ext)
            fresh.append(item)
        return fresh

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
        is_business = ad.get("isBusiness")
        if is_business is True:
            seller_type = "agency"
        elif is_business is False:
            seller_type = "private"
        else:
            seller_type = None

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
            agency=fix_mojibake((ad.get("user") or {}).get("name")) if is_business else None,
            extra={
                "snippet": title[:400],
                "olx_status": ad.get("status"),
                "seller": fix_mojibake((ad.get("user") or {}).get("name")),
                "seller_type": seller_type,
                "isBusiness": is_business,
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


def _olx_param_number(value: object) -> str | None:
    raw = value.get("key") if isinstance(value, dict) else value
    label = value.get("label") if isinstance(value, dict) else None
    for candidate in (raw, label):
        if candidate is None:
            continue
        match = re.search(r"\d+(?:[.,]\d+)?", str(candidate))
        if match:
            return match.group(0).replace(",", ".")
    return None


def _parse_olx_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def office_feed_reached_lookback(refresh_times: list[str], *, cutoff: datetime) -> bool:
    """True when the oldest non-promoted card on the page is past the lookback."""
    stamps = [stamp for stamp in (_parse_olx_time(raw) for raw in refresh_times) if stamp]
    if not stamps:
        return False
    return min(stamps) <= cutoff


def _offset_from_href(href: str, *, fallback: int) -> int:
    try:
        raw = parse_qs(urlsplit(href).query).get("offset", [None])[0]
        if raw is None:
            return fallback
        return max(0, int(raw))
    except (TypeError, ValueError):
        return fallback
