from __future__ import annotations

import json
import re
from typing import Any

from bs4 import BeautifulSoup

from app.domain.fingerprint import phone_digits
from app.scrapers.http_utils import parse_area, parse_floor, parse_price

SOLD_MARKERS = (
    "продано",
    "продано!",
    "объект продан",
    "об'єкт продано",
    "sold",
    "was sold",
)
RENTED_MARKERS = (
    "здано",
    "здано в оренду",
    "арендовано",
    "орендовано",
    "rented",
    "leased",
)
INACTIVE_MARKERS = (
    "неактуальн",
    "оголошення неактуальне",
    "знято з публікації",
    "знято з публікації",
    "архив",
    "архів",
    "в архиве",
    "оголошення видалено",
    "объявление удалено",
    "page not found",
    "404",
)


def load_json_ld(html: str) -> list[Any]:
    soup = BeautifulSoup(html, "lxml")
    out: list[Any] = []
    for script in soup.find_all("script", type="application/ld+json"):
        raw = script.string or script.get_text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            out.extend(data)
        else:
            out.append(data)
    return out


def iter_json_ld_types(blocks: list[Any], *type_names: str):
    wanted = {t.lower() for t in type_names}
    for block in blocks:
        if not isinstance(block, dict):
            continue
        types = block.get("@type")
        if isinstance(types, list):
            type_set = {str(t).lower() for t in types}
        else:
            type_set = {str(types).lower()} if types else set()
        if type_set & wanted:
            yield block


def extract_phones(html: str) -> list[str]:
    """Extract UA phone numbers; ignore random digit noise."""
    patterns = [
        # disallow digits/dot before/after so currency floats don't match
        r"(?<![\d.])(?:\+?38[\s\-]?)?\(?(?:0(?:39|50|63|66|67|68|73|91|92|93|94|95|96|97|98|99|44))\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}(?![\d.])",
        r"tel:([+\d\-\s()]+)",
        r"number=%2B(380\d{9})",
        r"(?<!\d)380(?:39|50|63|66|67|68|73|91|92|93|94|95|96|97|98|99|44)\d{7}(?!\d)",
    ]
    found: list[str] = []
    for pat in patterns:
        for m in re.finditer(pat, html, flags=re.I):
            found.append(m.group(0))

    normalized: list[str] = []
    seen: set[str] = set()
    for raw in found:
        digits = phone_digits(raw)
        if not digits or digits in seen:
            continue
        if not digits.startswith("0") or len(digits) != 10:
            continue
        prefix = digits[:3]
        if prefix not in {
            "039",
            "050",
            "063",
            "066",
            "067",
            "068",
            "073",
            "091",
            "092",
            "093",
            "094",
            "095",
            "096",
            "097",
            "098",
            "099",
            "044",
        }:
            continue
        seen.add(digits)
        normalized.append(digits)
    return normalized


def detect_listing_status(html: str, json_ld: list[Any] | None = None) -> str | None:
    """Return raw status label if listing looks sold/rented/inactive."""
    text = html.lower()
    blocks = json_ld or load_json_ld(html)

    for block in blocks:
        if not isinstance(block, dict):
            continue
        offer = block.get("offers")
        offers = offer if isinstance(offer, list) else [offer] if isinstance(offer, dict) else []
        for off in offers:
            if not isinstance(off, dict):
                continue
            avail = str(off.get("availability") or "").lower()
            if "soldout" in avail or "discontinued" in avail:
                return "sold_or_unavailable"

    # Strong textual markers first
    if re.search(r"\b(продано|об.?єкт продано|объект продан|was sold)\b", text):
        return "sold"
    if re.search(r"\b(здано|здано в оренду|орендовано|арендовано|rented|leased)\b", text):
        return "rented"

    # Soft-404 from LUN Next payload / explicit inactive copy
    if '"statuscode":404' in text.replace(" ", "") or '"statusCode":404' in html:
        return "inactive_404"
    if re.search(
        r"(оголошення неактуальне|объявление неактуально|знято з публікації|"
        r"оголошення видалено|объявление удалено)",
        text,
    ):
        return "inactive"

    for block in blocks:
        if not isinstance(block, dict):
            continue
        offer = block.get("offers")
        if isinstance(offer, dict) and "InStock" in str(offer.get("availability") or ""):
            return "active"
    return None


def postal_address_to_str(addr: Any) -> str | None:
    if not addr:
        return None
    if isinstance(addr, str):
        return addr.strip() or None
    if not isinstance(addr, dict):
        return None
    parts = [
        addr.get("streetAddress"),
        addr.get("addressLocality"),
        addr.get("addressRegion"),
    ]
    text = ", ".join(p.strip() for p in parts if isinstance(p, str) and p.strip())
    return text or None


def geo_from_json_ld(geo: Any) -> tuple[float | None, float | None]:
    if not isinstance(geo, dict):
        return None, None
    lat = geo.get("latitude")
    lon = geo.get("longitude")
    try:
        lat_f = float(lat) if lat is not None else None
        lon_f = float(lon) if lon is not None else None
    except (TypeError, ValueError):
        return None, None
    # LUN sometimes swaps lat/lon for UA (lon~30, lat~50)
    if lat_f is not None and lon_f is not None:
        if 20 <= lat_f <= 40 and 44 <= lon_f <= 54:
            lat_f, lon_f = lon_f, lat_f
    return lat_f, lon_f


def first_offer(block: dict[str, Any]) -> dict[str, Any] | None:
    offer = block.get("offers")
    if isinstance(offer, list) and offer:
        return offer[0] if isinstance(offer[0], dict) else None
    if isinstance(offer, dict):
        return offer
    return None


def parse_offer_price(offer: dict[str, Any] | None) -> tuple[float | None, str | None]:
    if not offer:
        return None, None
    price = offer.get("price")
    currency = offer.get("priceCurrency")
    cur = str(currency).upper() if currency else None
    if cur == "GRN":
        cur = "UAH"
    try:
        value = float(price) if price is not None else None
    except (TypeError, ValueError):
        return parse_price(str(price) if price is not None else None)
    if value is None:
        return None, cur
    import math

    if not math.isfinite(value) or value <= 0 or value > 500_000_000:
        return None, cur
    return value, cur


def extract_floor_area_from_text(*texts: str | None) -> tuple[float | None, int | None]:
    blob = " ".join(t for t in texts if t)
    return parse_area(blob), parse_floor(blob)


def og_meta(html: str, prop: str) -> str | None:
    soup = BeautifulSoup(html, "lxml")
    tag = soup.find("meta", property=prop) or soup.find("meta", attrs={"name": prop})
    if not tag:
        return None
    content = tag.get("content")
    return content.strip() if content else None


def merge_nonempty(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in patch.items():
        if v is None or v == "" or v == []:
            continue
        out[k] = v
    return out
