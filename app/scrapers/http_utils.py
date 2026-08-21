from __future__ import annotations

import logging
import math
import random
import re
import time
from typing import Any
from urllib.parse import urlsplit

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.config import get_settings

logger = logging.getLogger(__name__)

# Sanity bounds for commercial Kyiv listings
_MAX_PRICE = 500_000_000
_MAX_AREA = 50_000


class PortalBlockedError(RuntimeError):
    """HTTP 403/429 — slow down, do not hammer."""

    def __init__(self, status_code: int, url: str) -> None:
        self.status_code = status_code
        self.url = url
        super().__init__(f"portal blocked {status_code} for {url}")


# Realistic desktop browsers (rotate per crawl session).
_UA_POOL = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:133.0) Gecko/20100101 Firefox/133.0",
)

# Module-level request counter for rare "coffee breaks" across scrapers in one process.
_pace_state: dict[str, Any] = {
    "count": 0,
    "next_break_at": random.randint(8, 15),
    "host_last": {},  # host -> monotonic timestamp
    "host_failures": {},  # host -> consecutive soft failures
    "warmed": set(),  # hosts already warmed this process
}

_BLOCK_HTML_MARKERS = (
    "cf-browser-verification",
    "cf-challenge",
    "just a moment",
    "attention required",
    "access denied",
    "request blocked",
    "captcha",
    "cloudflare",
    "проверка безопасности",
    "доступ ограничен",
)


def looks_like_block_page(html: str | None) -> bool:
    if not html:
        return False
    low = html[:8000].lower()
    # Short challenge shells are a strong signal.
    if len(html) < 2500 and any(m in low for m in _BLOCK_HTML_MARKERS):
        return True
    hits = sum(1 for m in _BLOCK_HTML_MARKERS if m in low)
    return hits >= 2


def pick_user_agent(fixed: str | None = None) -> str:
    cleaned = (fixed or "").strip()
    if cleaned:
        return cleaned
    return random.choice(_UA_POOL)


def _chrome_client_hints(ua: str) -> dict[str, str]:
    if "Firefox/" in ua:
        return {}
    ver = "131"
    m = re.search(r"Chrome/(\d+)", ua)
    if m:
        ver = m.group(1)
    brand = '"Not)A;Brand";v="99", "Google Chrome";v="%s", "Chromium";v="%s"' % (ver, ver)
    if "Edg/" in ua:
        brand = '"Not)A;Brand";v="99", "Microsoft Edge";v="%s", "Chromium";v="%s"' % (ver, ver)
    return {
        "sec-ch-ua": brand,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"' if "Windows" in ua else '"macOS"',
    }


def browser_headers(ua: str, *, referer: str | None = None, same_site: bool = False) -> dict[str, str]:
    # Slight language preference jitter — still UA-local.
    langs = [
        "uk-UA,uk;q=0.9,ru;q=0.8,en-US;q=0.7,en;q=0.6",
        "uk-UA,uk;q=0.9,en-US;q=0.8,en;q=0.7,ru;q=0.5",
        "uk,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    ]
    headers: dict[str, str] = {
        "User-Agent": ua,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8"
        ),
        "Accept-Language": random.choice(langs),
        "Accept-Encoding": "gzip, deflate",
        "Cache-Control": "max-age=0",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin" if same_site else "none",
        "Sec-Fetch-User": "?1",
        "DNT": "1",
        "Connection": "keep-alive",
    }
    headers.update(_chrome_client_hints(ua))
    if referer:
        headers["Referer"] = referer
        headers["Sec-Fetch-Site"] = "same-origin" if same_site else "cross-site"
    return headers


def human_delay_seconds(*, blocked: bool = False) -> float:
    """Seconds to wait: lognormal around crawl_delay_sec, longer after blocks."""
    settings = get_settings()
    base = float(settings.crawl_delay_sec)
    jitter = float(settings.crawl_delay_jitter_sec)
    if blocked:
        base = max(base * 5.0, float(settings.crawl_block_backoff_sec))
        jitter = max(jitter, base * 0.35)

    if settings.crawl_human_mode:
        # Lognormal: mostly near base, occasional slower "reads".
        sigma = 0.45 if not blocked else 0.55
        mu = math.log(max(base, 0.4))
        delay = random.lognormvariate(mu, sigma)
        delay = min(delay, base + jitter * 3.5)
        delay = max(0.6 if not blocked else 8.0, delay)
    else:
        delay = max(0.2, base + random.uniform(-jitter, jitter))
    return delay


def sleep_crawl_delay(*, blocked: bool = False) -> None:
    """Polite delay with human-like jitter. Longer pause after 403/429."""
    time.sleep(human_delay_seconds(blocked=blocked))


def _maybe_session_break() -> None:
    settings = get_settings()
    if not settings.crawl_human_mode:
        return
    _pace_state["count"] = int(_pace_state["count"]) + 1
    if int(_pace_state["count"]) < int(_pace_state["next_break_at"]):
        return
    lo = float(settings.crawl_break_sec_min)
    hi = float(settings.crawl_break_sec_max)
    if hi < lo:
        lo, hi = hi, lo
    pause = random.uniform(max(5.0, lo), max(lo, hi))
    logger.info("crawl coffee break %.0fs after %s requests", pause, _pace_state["count"])
    time.sleep(pause)
    every_lo = max(3, int(settings.crawl_break_every_min))
    every_hi = max(every_lo, int(settings.crawl_break_every_max))
    _pace_state["next_break_at"] = int(_pace_state["count"]) + random.randint(every_lo, every_hi)


def _normalize_proxy(url: str | None) -> str | None:
    if url is None:
        return None
    cleaned = url.strip()
    return cleaned or None


def _origin(url: str) -> str:
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return ""
    return f"{parts.scheme}://{parts.netloc}"


class HttpClient:
    """Persistent browser-like session: cookies, Referer, paced GETs."""

    def __init__(self) -> None:
        settings = get_settings()
        self.timeout = settings.http_timeout_sec
        self.verify = settings.http_verify_ssl
        self.proxy = _normalize_proxy(settings.http_proxy)
        self.human_mode = bool(settings.crawl_human_mode)
        self.warmup = bool(getattr(settings, "crawl_warmup", True))
        self.host_min_interval = float(getattr(settings, "crawl_host_min_interval_sec", 2.8))
        self.user_agent = pick_user_agent(settings.user_agent)
        self._last_url: str | None = None
        self._client = self._build_client()

    def _build_client(self) -> httpx.Client:
        kwargs: dict[str, Any] = {
            "headers": browser_headers(self.user_agent),
            "timeout": httpx.Timeout(self.timeout, connect=min(15.0, self.timeout)),
            "follow_redirects": True,
            "verify": self.verify,
        }
        if self.proxy:
            kwargs["proxy"] = self.proxy
        return httpx.Client(**kwargs)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _host(self, url: str) -> str:
        return urlsplit(url).netloc.lower()

    def _request_headers(self, url: str) -> dict[str, str]:
        referer = None
        same_site = False
        if self._last_url:
            last_o = _origin(self._last_url)
            cur_o = _origin(url)
            if last_o and cur_o and last_o == cur_o:
                referer = self._last_url
                same_site = True
            elif cur_o:
                referer = cur_o + "/"
                same_site = False
        return browser_headers(self.user_agent, referer=referer, same_site=same_site)

    def _enforce_host_gap(self, url: str) -> None:
        host = self._host(url)
        if not host:
            return
        last = float(_pace_state["host_last"].get(host) or 0.0)
        if last <= 0:
            return
        elapsed = time.monotonic() - last
        need = self.host_min_interval
        fails = int(_pace_state["host_failures"].get(host) or 0)
        if fails:
            need *= 1.0 + min(fails, 5) * 0.35
        if elapsed < need:
            time.sleep(need - elapsed + random.uniform(0.05, 0.4))

    def _warmup_host(self, url: str) -> None:
        if not (self.human_mode and self.warmup):
            return
        host = self._host(url)
        origin = _origin(url)
        if not host or not origin or host in _pace_state["warmed"]:
            return
        home = origin + "/"
        if urlsplit(url).path in ("", "/"):
            _pace_state["warmed"].add(host)
            return
        logger.info("crawl warmup %s", home)
        try:
            self._enforce_host_gap(home)
            headers = browser_headers(self.user_agent)
            resp = self._client.get(home, headers=headers)
            _pace_state["host_last"][host] = time.monotonic()
            self._last_url = str(resp.url)
            if resp.status_code in (403, 429) or looks_like_block_page(resp.text):
                _pace_state["host_failures"][host] = int(_pace_state["host_failures"].get(host) or 0) + 1
                sleep_crawl_delay(blocked=True)
            else:
                # Short "read the homepage" pause.
                time.sleep(random.uniform(1.2, 3.5))
        except Exception as exc:  # noqa: BLE001
            logger.warning("warmup failed %s: %s", home, exc)
        finally:
            _pace_state["warmed"].add(host)

    def _pace(self, url: str) -> None:
        self._warmup_host(url)
        if not self.human_mode:
            sleep_crawl_delay()
            self._enforce_host_gap(url)
            return
        _maybe_session_break()
        delay = human_delay_seconds()
        if self._last_url and _origin(self._last_url) != _origin(url):
            delay *= random.uniform(1.25, 1.75)
        host = self._host(url)
        fails = int(_pace_state["host_failures"].get(host) or 0)
        if fails:
            delay *= 1.0 + min(fails, 4) * 0.4
        time.sleep(delay)
        self._enforce_host_gap(url)

    def _mark_host_ok(self, url: str) -> None:
        host = self._host(url)
        if host:
            _pace_state["host_last"][host] = time.monotonic()
            _pace_state["host_failures"][host] = 0

    def _mark_host_fail(self, url: str) -> None:
        host = self._host(url)
        if host:
            _pace_state["host_last"][host] = time.monotonic()
            _pace_state["host_failures"][host] = int(_pace_state["host_failures"].get(host) or 0) + 1

    def _rotate_identity(self) -> None:
        self.user_agent = pick_user_agent(None)
        self._client.headers.update(browser_headers(self.user_agent))

    def _raise_for_portal(self, resp: httpx.Response, url: str) -> None:
        if resp.status_code in (403, 429, 503):
            if self.human_mode:
                self._rotate_identity()
            self._mark_host_fail(url)
            sleep_crawl_delay(blocked=True)
            raise PortalBlockedError(resp.status_code, url)
        resp.raise_for_status()

    def _check_html_block(self, html: str, url: str) -> None:
        if looks_like_block_page(html):
            if self.human_mode:
                self._rotate_identity()
            self._mark_host_fail(url)
            sleep_crawl_delay(blocked=True)
            raise PortalBlockedError(403, url)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=3, max=45),
        retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
        reraise=True,
    )
    def get_text(self, url: str, params: dict[str, Any] | None = None) -> str:
        self._pace(url)
        headers = self._request_headers(url)
        try:
            resp = self._client.get(url, params=params, headers=headers)
            self._raise_for_portal(resp, url)
            text = resp.text
            self._check_html_block(text, url)
            self._mark_host_ok(url)
            self._last_url = str(resp.url)
            return text
        except PortalBlockedError:
            raise
        except Exception:
            self._mark_host_fail(url)
            raise

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=3, max=45),
        retry=retry_if_exception_type((httpx.TransportError, httpx.TimeoutException)),
        reraise=True,
    )
    def get_json(self, url: str, params: dict[str, Any] | None = None) -> Any:
        self._pace(url)
        headers = self._request_headers(url)
        headers["Accept"] = "application/json, text/plain, */*"
        headers["Sec-Fetch-Dest"] = "empty"
        headers["Sec-Fetch-Mode"] = "cors"
        try:
            resp = self._client.get(url, params=params, headers=headers)
            self._raise_for_portal(resp, url)
            self._mark_host_ok(url)
            self._last_url = str(resp.url)
            return resp.json()
        except PortalBlockedError:
            raise
        except Exception:
            self._mark_host_fail(url)
            raise




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
