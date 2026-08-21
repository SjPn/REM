from __future__ import annotations

import math
import random
import re
import time
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import get_settings

# Sanity bounds for commercial Kyiv listings
_MAX_PRICE = 500_000_000
_MAX_AREA = 50_000


class PortalBlockedError(RuntimeError):
    """HTTP 403/429 — slow down, do not hammer."""

    def __init__(self, status_code: int, url: str) -> None:
        self.status_code = status_code
        self.url = url
        super().__init__(f"portal blocked {status_code} for {url}")


def sleep_crawl_delay(*, blocked: bool = False) -> None:
    """Polite delay with jitter. Longer pause after 403/429."""
    settings = get_settings()
    base = settings.crawl_delay_sec
    jitter = settings.crawl_delay_jitter_sec
    if blocked:
        base = max(base * 4, settings.crawl_block_backoff_sec)
        jitter = max(jitter, base * 0.3)
    delay = max(0.2, base + random.uniform(-jitter, jitter))
    time.sleep(delay)


def parse_price(text: str | None) -> tuple[float | None, str | None]:
    """Parse a single *total* price near a currency marker. Skip $/м² chips."""
    if not text:
        return None, None

    patterns = [
        (r"(?:USD|\$|дол(?:\.|арі|ларов)?)\s*[:\s]*(\d{1,3}(?:[ \u00a0]?\d{3})+|\d+(?:[.,]\d+)?)", "USD"),
        (r"(\d{1,3}(?:[ \u00a0]?\d{3})+|\d+(?:[.,]\d+)?)\s*(?:USD|\$|дол)", "USD"),
        (r"(?:EUR|€|євро)\s*[:\s]*(\d{1,3}(?:[ \u00a0]?\d{3})+|\d+(?:[.,]\d+)?)", "EUR"),
        (r"(\d{1,3}(?:[ \u00a0]?\d{3})+|\d+(?:[.,]\d+)?)\s*(?:EUR|€|євро)", "EUR"),
        (r"(?:UAH|₴|грн)\s*[:\s]*(\d{1,3}(?:[ \u00a0]?\d{3})+|\d+(?:[.,]\d+)?)", "UAH"),
        (r"(\d{1,3}(?:[ \u00a0]?\d{3})+|\d+(?:[.,]\d+)?)\s*(?:UAH|₴|грн)", "UAH"),
    ]
    candidates: list[tuple[float, str, int]] = []
    for pat, cur in patterns:
        for m in re.finditer(pat, text, flags=re.IGNORECASE):
            # Skip "115 $/м²" — rate, not total. Keep "2 500 $ / міс" (monthly total).
            tail = text[m.end() : m.end() + 12]
            if re.match(
                r"\s*/\s*м(?:²|2)|\s*/\s*m2|\s*/\s*sqm|\s*за\s*м(?:²|2)",
                tail,
                flags=re.IGNORECASE,
            ):
                continue
            value = _to_price_number(m.group(1))
            if value is not None:
                candidates.append((value, cur, m.start()))

    if candidates:
        # Prefer the largest plausible total (rieltor cards often show total + $/м²)
        candidates.sort(key=lambda x: (-x[0], x[2]))
        return candidates[0][0], candidates[0][1]

    lower = text.lower()
    currency = None
    if "$" in text or "usd" in lower or "дол" in lower:
        currency = "USD"
    elif "€" in text or "eur" in lower or "євро" in lower:
        currency = "EUR"
    elif "₴" in text or "грн" in lower or "uah" in lower:
        currency = "UAH"
    if not currency:
        return None, None

    m = re.search(r"\b(\d{1,3}(?:[ \u00a0]\d{3}){1,4}|\d{4,9})(?:[.,]\d+)?\b", text)
    if not m:
        return None, currency
    return _to_price_number(m.group(1)), currency


def parse_price_per_sqm(text: str | None) -> tuple[float | None, str | None]:
    """Parse explicit N $/м² (or грн/м²) from card text. Not $/міс."""
    if not text:
        return None, None
    m = re.search(
        r"(\d{1,3}(?:[ \u00a0]?\d{3})*(?:[.,]\d+)?|\d+(?:[.,]\d+)?)\s*"
        r"(USD|\$|UAH|₴|грн|EUR|€)?\s*/\s*м(?:²|2)\b",
        text,
        flags=re.IGNORECASE,
    )
    if not m:
        m = re.search(
            r"(\d{1,3}(?:[ \u00a0]?\d{3})*(?:[.,]\d+)?|\d+(?:[.,]\d+)?)\s*"
            r"(USD|\$|UAH|₴|грн|EUR|€)?\s*/\s*sqm\b",
            text,
            flags=re.IGNORECASE,
        )
    if not m:
        return None, None
    value = _to_price_number(m.group(1))
    cur_raw = (m.group(2) or "").lower()
    if cur_raw in ("$", "usd") or "$" in (m.group(0) or ""):
        cur = "USD"
    elif cur_raw in ("€", "eur") or "€" in (m.group(0) or ""):
        cur = "EUR"
    elif cur_raw in ("uah", "грн", "₴") or "грн" in (m.group(0) or "").lower():
        cur = "UAH"
    else:
        cur = "USD"
    return value, cur


def _to_price_number(raw: str) -> float | None:
    cleaned = raw.replace("\xa0", "").replace(" ", "").replace(",", ".")
    if re.fullmatch(r"\d{1,3}(\.\d{3})+", cleaned):
        cleaned = cleaned.replace(".", "")
    try:
        value = float(cleaned)
    except ValueError:
        return None
    if not math.isfinite(value) or value <= 0 or value > _MAX_PRICE:
        return None
    return value


def parse_area(text: str | None) -> float | None:
    if not text:
        return None
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:м²|м2|кв\.?\s*м)", text, re.IGNORECASE)
    if not m:
        return None
    try:
        value = float(m.group(1).replace(",", "."))
    except ValueError:
        return None
    if not math.isfinite(value) or value <= 0 or value > _MAX_AREA:
        return None
    return value


def parse_floor(text: str | None) -> int | None:
    if not text:
        return None
    m = re.search(r"(-?\d+)\s*(?:поверх|эт\.?|floor)", text, re.IGNORECASE)
    if not m:
        m = re.search(r"(?:поверх|эт\.?|floor)\s*[:\-]?\s*(-?\d+)", text, re.IGNORECASE)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def guess_property_type(text: str | None) -> str:
    t = (text or "").lower()
    if any(x in t for x in ("шоурум", "showroom", "show-room")):
        return "showroom"
    if any(x in t for x in ("бізнес-центр", "бизнес-центр", "бізнес центр", "бизнес центр")) or re.search(
        r"\bбц\b", t
    ):
        return "business_center"
    if any(x in t for x in ("окрема будівля", "отдельно стоящ", "окремо стояч")):
        return "building"
    if any(x in t for x in ("офіс", "офис", "office", "ofis")):
        return "office"
    if any(x in t for x in ("склад", "warehouse", "логіст", "логист", "ангар")):
        return "warehouse"
    if any(x in t for x in ("торг", "магазин", "рітейл", "ритейл", "retail")):
        if any(x in t for x in ("1 поверх", "перший поверх", "первый этаж", "фасад")):
            return "street_retail"
        return "retail"
    if any(x in t for x in ("вироб", "промисл", "industrial", "цех", "завод")):
        return "industrial"
    if any(x in t for x in ("земл", "ділянка", "участок", "land")):
        return "land_commercial"
    if any(x in t for x in ("вільного призначення", "свободного назначения", "free")):
        return "free_purpose"
    return "other"


def is_kyiv_region_url(url: str) -> bool:
    """Accept only Kyiv city / Kyiv oblast listings by URL slug."""
    low = url.lower()
    foreign = (
        "lvov", "lviv", "odessa", "odesa", "kharkov", "kharkiv", "dnipro", "dnepr",
        "vinnitsa", "vinnyts", "ternopol", "ternopil", "ivano", "uzhgorod", "chernovts",
        "chernigov", "chernihiv", "poltava", "zaporozh", "zaporizh", "nikolaev", "mykolaiv",
        "krivoy", "kryvyi", "rovno", "rivne", "lutsk", "sumy", "zhytomyr", "khmeln",
        "cherkass", "kropivn", "kropyvnytskyi", "mariupol", "kherson",
    )
    if any(x in low for x in foreign):
        return False
    kyiv_markers = (
        "kiev", "kyiv", "киев", "київ", "brovary", "irpen", "irpin", "bucha", "vyshneve",
        "boyarka", "obukhov", "borschagov", "borshchahiv", "sofievsk", "sofiivsk",
        "petropavlovsk", "vyshenki", "hatne", "glevakha", "kotsyubinsk", "kotsiubyns",
    )
    return any(x in low for x in kyiv_markers)


def _normalize_proxy(url: str | None) -> str | None:
    if url is None:
        return None
    cleaned = url.strip()
    return cleaned or None


class HttpClient:
    def __init__(self) -> None:
        settings = get_settings()
        self.timeout = settings.http_timeout_sec
        self.verify = settings.http_verify_ssl
        self.proxy = _normalize_proxy(settings.http_proxy)
        self.headers = {
            "User-Agent": settings.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8,ru;q=0.7",
        }

    def _client(self, **extra_headers: str) -> httpx.Client:
        kwargs: dict[str, Any] = {
            "headers": {**self.headers, **extra_headers},
            "timeout": self.timeout,
            "follow_redirects": True,
            "verify": self.verify,
        }
        if self.proxy:
            kwargs["proxy"] = self.proxy
        return httpx.Client(**kwargs)

    def _raise_for_portal(self, resp: httpx.Response, url: str) -> None:
        if resp.status_code in (403, 429):
            sleep_crawl_delay(blocked=True)
            raise PortalBlockedError(resp.status_code, url)
        resp.raise_for_status()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
        reraise=True,
    )
    def get_text(self, url: str, params: dict[str, Any] | None = None) -> str:
        with self._client() as client:
            resp = client.get(url, params=params)
            self._raise_for_portal(resp, url)
            return resp.text

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
        reraise=True,
    )
    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        with self._client(Accept="application/json") as client:
            resp = client.get(url, params=params)
            self._raise_for_portal(resp, url)
            return resp.json()
