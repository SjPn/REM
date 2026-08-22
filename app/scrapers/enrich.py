from __future__ import annotations

import logging
import random
from collections.abc import Callable, Iterator
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
    needs_detail: Callable[[RawListing], bool] | None = None,
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
    skipped_unchanged = 0
    for item in work:
        if (item.extra or {}).get("skip_detail"):
            yield item
            continue
        if needs_detail is not None and not needs_detail(item):
            skipped_unchanged += 1
            yield item
            continue
        if enriched >= limit:
            yield item
            continue
        try:
            detailed = scraper.fetch_detail(item)
            enriched += 1
            yield detailed
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "%s detail failed %s: %s", scraper.source, item.url, exc
            )
            yield item
    if skipped_unchanged:
        logger.info(
            "%s watch: skipped %s unchanged detail fetches",
            scraper.source,
            skipped_unchanged,
        )
