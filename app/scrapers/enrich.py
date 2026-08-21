from __future__ import annotations

import logging
import random
from collections.abc import Iterator
from typing import Protocol

from app.config import get_settings
from app.scrapers.base import RawListing

logger = logging.getLogger(__name__)


class SupportsDetail(Protocol):
    source: str

    def fetch_detail(self, listing: RawListing) -> RawListing: ...


def enrich_listings(
    scraper: SupportsDetail,
    listings: list[RawListing],
    *,
    max_details: int | None = None,
) -> Iterator[RawListing]:
    settings = get_settings()
    if not settings.enrich_details:
        yield from listings
        return

    work = list(listings)
    if settings.crawl_human_mode and len(work) > 1:
        random.shuffle(work)

    limit = max_details if max_details is not None else settings.max_detail_pages
    enriched = 0
    for item in work:
        if enriched >= limit or (item.extra or {}).get("skip_detail"):
            yield item
            continue
        try:
            # Pace lives in HttpClient.get_text (human-like delays).
            detailed = scraper.fetch_detail(item)
            enriched += 1
            yield detailed
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "%s detail failed %s: %s", scraper.source, item.url, exc
            )
            yield item
