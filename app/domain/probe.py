"""Прямая проверка карточки: решение по ответу страницы, без обхода ленты."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

from app.scrapers.detail import detect_listing_status
from app.scrapers.http_utils import looks_like_block_page

# Суффиксы хостов площадок. Чужой URL из базы не открываем.
_SOURCE_HOSTS: dict[str, tuple[str, ...]] = {
    "lun": ("lun.ua",),
    "olx": ("olx.ua",),
    "domria": ("dom.ria.com",),
    "rieltor": ("rieltor.ua",),
    "m2bomber": ("m2bomber.com",),
}


@dataclass(frozen=True)
class ProbeDecision:
    miss: int
    miss_on: str | None
    action: str  # alive | miss | vanish | sold | rented


def utc_day(now: datetime) -> str:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc).date().isoformat()


def probe_stamp(raw_extra: dict | None) -> str:
    """Метка последней проверки. Пустая строка — карточку ещё не открывали."""
    if not isinstance(raw_extra, dict):
        return ""
    probe = raw_extra.get("probe")
    if not isinstance(probe, dict):
        return ""
    at = probe.get("at")
    return str(at) if at else ""


def probe_miss(raw_extra: dict | None) -> tuple[int, str | None]:
    if not isinstance(raw_extra, dict):
        return 0, None
    probe = raw_extra.get("probe")
    if not isinstance(probe, dict):
        return 0, None
    try:
        miss = int(probe.get("miss") or 0)
    except (TypeError, ValueError):
        miss = 0
    miss_on = probe.get("miss_on")
    if not isinstance(miss_on, str) or not miss_on.strip():
        miss_on = None
    return max(0, miss), miss_on


def decide_probe(
    prev_miss: int,
    prev_miss_on: str | None,
    kind: str,
    today: str,
) -> ProbeDecision:
    """Снятие подтверждается вторым ответом «снято» в другой день.

    Повтор в тот же день не считается вторым подтверждением.
    Живая страница и явный статус обнуляют промахи.
    """
    if kind == "alive":
        return ProbeDecision(0, None, "alive")
    if kind == "sold":
        return ProbeDecision(0, None, "sold")
    if kind == "rented":
        return ProbeDecision(0, None, "rented")
    if kind != "removed":
        raise ValueError(f"unknown probe kind: {kind}")
    if prev_miss >= 1 and prev_miss_on and prev_miss_on != today:
        return ProbeDecision(prev_miss + 1, today, "vanish")
    if prev_miss >= 1 and prev_miss_on == today:
        return ProbeDecision(prev_miss, prev_miss_on, "miss")
    return ProbeDecision(1, today, "miss")


def classify_probe_body(status_code: int, html: str) -> str:
    """alive | removed | sold | rented | blocked | skip.

    skip — ответ не доказал ни жизнь, ни снятие (5xx, пустая страница).
    """
    if status_code in (404, 410):
        return "removed"
    if status_code != 200 or not html:
        return "skip"
    if looks_like_block_page(html):
        return "blocked"
    status = detect_listing_status(html)
    if status:
        label = status.lower()
        if "sold" in label:
            return "sold"
        if "rent" in label:
            return "rented"
        if "inactive" in label or "404" in label:
            return "removed"
        if label == "active":
            return "alive"
    low = html.lower()
    if "og:title" in low or "application/ld+json" in low:
        return "alive"
    return "skip"


def listing_url_allowed(source: str, url: str) -> bool:
    """Только https на хост своей площадки. Без логина в URL и без IP."""
    raw = (url or "").strip()
    if not raw or len(raw) > 1000:
        return False
    try:
        parts = urlsplit(raw)
    except ValueError:
        return False
    if parts.scheme != "https" or parts.username or parts.password:
        return False
    host = (parts.hostname or "").lower().rstrip(".")
    if not host or host == "localhost" or host.endswith(".local"):
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        pass
    suffixes = _SOURCE_HOSTS.get(source)
    if not suffixes:
        return False
    return any(host == suffix or host.endswith("." + suffix) for suffix in suffixes)


def write_probe_meta(
    raw_extra: dict | None,
    *,
    at: str,
    miss: int,
    miss_on: str | None,
    kind: str,
) -> dict:
    extra = dict(raw_extra or {})
    extra["probe"] = {
        "at": at,
        "miss": int(miss),
        "miss_on": miss_on,
        "kind": kind,
    }
    return extra
