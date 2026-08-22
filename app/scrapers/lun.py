from __future__ import annotations

import logging
import hashlib
import random
import re
import time
from collections.abc import Callable, Iterator

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
from app.scrapers.http_utils import HttpClient, guess_property_type, parse_area

logger = logging.getLogger(__name__)

LUN_SEARCH = {
    (DealType.RENT, "kyiv"): "https://lun.ua/rent/kyiv/commercial",
    (DealType.SALE, "kyiv"): "https://lun.ua/sale/kyiv/commercial",
    (DealType.RENT, "region"): "https://lun.ua/rent/kyiv/region-commercial",
    (DealType.SALE, "region"): "https://lun.ua/sale/kyiv/region-commercial",
}


class LunScraper:
    source = SourceName.LUN.value

    def __init__(self, client: HttpClient | None = None) -> None:
        self.client = client or HttpClient()
        self.settings = get_settings()

    def crawl(
        self,
        max_pages: int | None = None,
        needs_detail: Callable[[RawListing], bool] | None = None,
    ) -> Iterator[RawListing]:
        pages = max_pages or self.settings.crawl_max_pages
        entries = list(LUN_SEARCH.items())
        if self.settings.crawl_human_mode:
            random.shuffle(entries)
        for (deal_type, zone), base_url in entries:
            batch: list[RawListing] = []
            for page in range(1, pages + 1):
                url = base_url if page == 1 else f"{base_url}?page={page}"
                logger.info("LUN fetch %s", url)
                try:
                    html = self.client.get_text(url)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("LUN page failed %s: %s", url, exc)
                    break
                items = self._parse_list(html, deal_type, zone)
                if not items:
                    break
                batch.extend(items)
            yield from enrich_listings(self, batch, needs_detail=needs_detail)

    def parse_detail(self, html: str, listing: RawListing) -> RawListing:
        blocks = load_json_ld(html)
        title = og_meta(html, "og:title") or listing.title
        description = og_meta(html, "og:description") or listing.description
        phones = extract_phones(html)
        status = detect_listing_status(html, blocks)

        address = listing.address_raw
        district = listing.district
        city = listing.city
        lat, lon = listing.lat, listing.lon
        price, currency = listing.price, listing.currency
        area = listing.area_sqm
        floor = listing.floor

        for block in blocks:
            if not isinstance(block, dict):
                continue
            addr = postal_address_to_str(block.get("address"))
            if addr:
                address = addr
            g_lat, g_lon = geo_from_json_ld(block.get("geo"))
            if g_lat is not None:
                lat, lon = g_lat, g_lon
            offer = first_offer(block)
            p, c = parse_offer_price(offer)
            if p is not None:
                price, currency = p, c or currency

        # Breadcrumb district
        for block in iter_json_ld_types(blocks, "BreadcrumbList"):
            for el in block.get("itemListElement") or []:
                if not isinstance(el, dict):
                    continue
                name = el.get("name")
                item = el.get("item")
                if not name and isinstance(item, dict):
                    name = item.get("name")
                if not isinstance(name, str):
                    continue
                low = name.lower()
                if "район" in low or low.endswith("кий") or low.endswith("ський"):
                    district = name

        area2, floor2 = extract_floor_area_from_text(title, description, html[:8000])
        area = area or area2
        floor = floor if floor is not None else floor2

        # Title often: "Street, City, price — ЛУН"
        if title:
            head = title.split("—")[0].strip()
            head = re.sub(
                r",?\s*\d[\d\s.,]*\s*(грн|\$|usd|uah|євро|eur).*$",
                "",
                head,
                flags=re.I,
            ).strip(" ,")
            if head and (not address or len(head) > len(address)):
                address = head

        listing.title = (title or listing.title or "")[:500] or listing.title
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
        listing.phone = phones[0] if phones else listing.phone
        listing.source_status_raw = status or listing.source_status_raw
        listing.property_type = guess_property_type(
            f"{listing.title or ''} {listing.description or ''}"
        )
        listing.extra = {
            **(listing.extra or {}),
            "detail": True,
            "phones": phones,
            "status": status,
        }
        return listing

    def _parse_list(
        self, html: str, deal_type: DealType, zone: str
    ) -> list[RawListing]:
        blocks = load_json_ld(html)
        realty_ids = list(dict.fromkeys(re.findall(r"/realty/(\d{6,})", html)))
        items: list[RawListing] = []

        for block in blocks:
            if not isinstance(block, dict):
                continue
            types = block.get("@type")
            if "ItemList" not in str(types):
                continue
            elements = block.get("itemListElement") or []
            for idx, el in enumerate(elements):
                item = el.get("item") if isinstance(el, dict) else None
                if not isinstance(item, dict):
                    continue
                ext_id = realty_ids[idx] if idx < len(realty_ids) else None
                if not ext_id:
                    # synthetic stable id from image+name when no public realty link
                    img = item.get("image")
                    if isinstance(img, list):
                        img = img[0] if img else ""
                    seed = f"{item.get('name')}|{img}|{idx}|{zone}|{deal_type.value}"
                    ext_id = "ld-" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]
                    url = f"https://lun.ua/{deal_type.value}/kyiv/commercial#item-{ext_id}"
                    skip_detail = True
                else:
                    url = f"https://lun.ua/realty/{ext_id}"
                    skip_detail = False

                address = postal_address_to_str(item.get("address"))
                city = None
                district = None
                if isinstance(item.get("address"), dict):
                    city = item["address"].get("addressLocality")
                    offer = first_offer(item) or {}
                    area_served = offer.get("areaServed")
                    if isinstance(area_served, dict):
                        district = area_served.get("name")
                lat, lon = geo_from_json_ld(item.get("geo"))
                offer = first_offer(item)
                price, currency = parse_offer_price(offer)
                floor_size = item.get("floorSize") or {}
                area = None
                if isinstance(floor_size, dict) and floor_size.get("value") is not None:
                    try:
                        area = float(floor_size["value"])
                    except (TypeError, ValueError):
                        area = parse_area(str(floor_size.get("value")))
                status = None
                if offer and "InStock" in str(offer.get("availability") or ""):
                    status = "active"
                elif offer:
                    status = str(offer.get("availability") or "")

                name = item.get("name") or address or f"LUN {ext_id}"
                raw = RawListing(
                    source=self.source,
                    external_id=str(ext_id),
                    url=url,
                    deal_type=deal_type.value,
                    title=str(name)[:500],
                    property_type=guess_property_type(str(name)),
                    price=price,
                    currency=(currency or "UAH").upper(),
                    area_sqm=area,
                    address_raw=address,
                    district=district,
                    city=city or ("Київ" if zone == "kyiv" else "Київська область"),
                    lat=lat,
                    lon=lon,
                    source_status_raw=status,
                    extra={
                        "zone": zone,
                        "skip_detail": skip_detail,
                        "from_json_ld": True,
                    },
                )
                items.append(raw)
            break

        if not items:
            logger.warning("LUN: JSON-LD ItemList empty; markup may have changed")
        return items

    def fetch_detail(self, listing: RawListing) -> RawListing:  # noqa: F811
        if (listing.extra or {}).get("skip_detail"):
            return listing
        if "/realty/" not in listing.url:
            return listing
        html = self.client.get_text(listing.url)
        return self.parse_detail(html, listing)
