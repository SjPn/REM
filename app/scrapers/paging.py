"""Остановка ленты, когда следующие страницы не приносят новых объявлений."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable

from app.scrapers.base import RawListing

logger = logging.getLogger(__name__)


class FeedPager:
    """Считает страницы подряд без новых external_id."""

    def __init__(self, stale_page_limit: int = 2) -> None:
        self.limit = max(1, int(stale_page_limit))
        self.seen: set[str] = set()
        self.stale = 0

    def note(self, external_ids: Iterable[str]) -> bool:
        """True — следующую страницу этой ленты запрашивать не нужно."""
        fresh = 0
        for raw_id in external_ids:
            ext = str(raw_id or "").strip()
            if not ext or ext in self.seen:
                continue
            self.seen.add(ext)
            fresh += 1
        if fresh == 0:
            self.stale += 1
            return self.stale >= self.limit
        self.stale = 0
        return False


def iter_feed_pages(
    client,
    base_url: str,
    max_pages: int,
    parse_page: Callable[[str], list[RawListing]],
    *,
    stale_page_limit: int = 2,
    log_label: str,
    first_html: str | None = None,
) -> list[RawListing]:
    """Страницы одной ленты. Пустая выдача или повтор id останавливают ленту."""
    pager = FeedPager(stale_page_limit)
    batch: list[RawListing] = []
    for page in range(1, max(1, int(max_pages)) + 1):
        url = base_url if page == 1 else f"{base_url}?page={page}"
        logger.info("%s fetch %s", log_label, url)
        try:
            if page == 1 and first_html is not None:
                html = first_html
            else:
                html = client.get_list_text(url)
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s page failed %s: %s", log_label, url, exc)
            break
        items = parse_page(html)
        if not items:
            break
        batch.extend(items)
        if pager.note(item.external_id for item in items):
            logger.info(
                "%s stop %s: %s pages in a row without new ids",
                log_label,
                url,
                pager.stale,
            )
            break
    return batch
