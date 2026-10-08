"""Стоп ленты, потолок добора и лёгкий день."""

from __future__ import annotations

import threading

from app.pipeline.ensure_ready import next_fill_pages
from app.scrapers.base import RawListing
from app.scrapers.paging import FeedPager, iter_feed_pages


def test_feed_pager_stops_after_two_duplicate_pages():
    pager = FeedPager(2)
    assert pager.note(["a", "b"]) is False
    assert pager.note(["b", "c"]) is False
    assert pager.note(["b", "c"]) is False
    assert pager.stale == 1
    assert pager.note(["c"]) is True
    assert pager.stale == 2


def test_feed_pager_resets_when_new_id_appears():
    pager = FeedPager(1)
    assert pager.note(["a"]) is False
    assert pager.note(["a"]) is True


def test_iter_feed_pages_stops_and_keeps_duplicate_cards():
    class Client:
        def __init__(self) -> None:
            self.urls: list[str] = []

        def get_list_text(self, url: str, params=None) -> str:
            self.urls.append(url)
            return url

    client = Client()

    def parse(html: str) -> list[RawListing]:
        page = 1 if "page=" not in html else int(html.rsplit("page=", 1)[-1])
        ext = "a" if page == 1 else "a"
        if page >= 2:
            ext = "a"
        return [
            RawListing(
                source="olx",
                external_id=ext if page > 1 else "a",
                url=f"https://example/{page}",
                deal_type="sale",
            )
        ]

    batch = iter_feed_pages(
        client,
        "https://example/list/",
        6,
        parse,
        stale_page_limit=1,
        log_label="TEST",
    )
    assert [u.split("page=")[-1] if "page=" in u else "1" for u in client.urls] == ["1", "2"]
    assert len(batch) == 2


def test_next_fill_pages_stops_when_seen_does_not_grow():
    nxt, stuck = next_fill_pages(
        pages=28,
        seen=1699,
        prev_seen=1699,
        ratio=0.7,
        target=0.85,
        page_ceiling=120,
    )
    assert nxt is None
    assert stuck == "seen_plateau"


def test_next_fill_pages_grows_only_after_new_ids():
    nxt, stuck = next_fill_pages(
        pages=15,
        seen=1200,
        prev_seen=1000,
        ratio=0.5,
        target=0.85,
        page_ceiling=120,
    )
    assert stuck is None
    assert nxt is not None and nxt > 15


def test_next_fill_pages_respects_ceiling():
    nxt, stuck = next_fill_pages(
        pages=120,
        seen=2000,
        prev_seen=1000,
        ratio=0.5,
        target=0.85,
        page_ceiling=120,
    )
    assert nxt is None
    assert stuck == "page_ceiling"


def test_lun_crawl_stops_on_repeated_ids(monkeypatch):
    from app.config import get_settings
    from app.scrapers.lun import LunScraper

    settings = get_settings()
    monkeypatch.setattr(settings, "enrich_details", False)
    monkeypatch.setattr(settings, "crawl_human_mode", False)

    class Client:
        def __init__(self) -> None:
            self.urls: list[str] = []

        def get_list_text(self, url: str, params=None) -> str:
            self.urls.append(url)
            return "html"

    client = Client()
    scraper = LunScraper(client=client)

    def parse(html, deal_type, zone):
        return [
            RawListing(
                source="lun",
                external_id="same",
                url="https://lun.ua/a",
                deal_type="sale",
            )
        ]

    scraper._parse_list = parse
    list(scraper.crawl(max_pages=6, stale_page_limit=2))
    assert len(client.urls) == 4 * 3
    assert all("page=4" not in url for url in client.urls)


def test_light_needs_detail_does_not_hold_a_read_transaction(tmp_path, monkeypatch):
    db_path = tmp_path / "light.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    from app.config import get_settings
    from app.db import models as db_models
    from app.db.models import get_session_factory, init_db
    from app.pipeline.light_day import _light_needs_detail

    get_settings.cache_clear()
    db_models._engine = None
    db_models._SessionLocal = None
    init_db()
    with get_session_factory()() as db:
        needs = _light_needs_detail(db)
        assert needs(
            RawListing(
                source="lun",
                external_id="missing",
                url="https://lun.ua/x",
                deal_type="sale",
            )
        ) is True
        assert db.in_transaction() is False


def test_light_day_fetches_sources_in_parallel(monkeypatch):
    from app.pipeline import light_day
    from app.scrapers import SCRAPERS

    started: list[str] = []
    lock = threading.Lock()
    ready = threading.Event()
    total = len(SCRAPERS)

    def fake_fetch(source, pages, stale, details):
        assert pages == 6
        assert stale == 1
        assert details == 30
        with lock:
            started.append(source)
            if len(started) == total:
                ready.set()
        assert ready.wait(3), "sources did not run in parallel"
        return source, [], None

    monkeypatch.setattr(light_day, "_fetch_one", fake_fetch)
    monkeypatch.setattr(
        light_day,
        "ingest_many",
        lambda db, items: {
            "upserted": 0,
            "skipped_irrelevant": 0,
            "snapshots_skipped": 0,
            "upserted_external_ids": set(),
        },
    )

    class Snap:
        day = "2026-10-08"

    monkeypatch.setattr(light_day, "record_market_snapshot", lambda db, force=True: Snap())
    summary = light_day.run_light_day(
        object(),
        max_pages=6,
        max_details=30,
        stale_page_limit=1,
    )
    assert summary["mode"] == "light"
    assert summary["vanish"] is False
    assert summary["ok"] is True
    assert set(summary["sources"]) == set(SCRAPERS)
    assert all("error" not in row for row in summary["sources"].values())
