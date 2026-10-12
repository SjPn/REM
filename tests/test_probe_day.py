"""Прямая проверка карточек: два дня до сделки, лимит времени, без чужих URL."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.domain.probe import (
    classify_probe_body,
    decide_probe,
    listing_url_allowed,
    probe_miss,
)
from app.scrapers.http_utils import PortalBlockedError


DAY1 = datetime(2026, 10, 10, 12, tzinfo=timezone.utc)
DAY2 = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
ALIVE_HTML = (
    "<html><head><meta property=\"og:title\" content=\"Офис\"></head>"
    "<script type=\"application/ld+json\">"
    '{"@type":"Product","offers":{"availability":"https://schema.org/InStock"}}'
    "</script></html>"
)


def test_decide_probe_confirms_only_on_another_day():
    first = decide_probe(0, None, "removed", "2026-10-10")
    assert first.action == "miss"
    assert first.miss == 1
    assert first.miss_on == "2026-10-10"

    same = decide_probe(first.miss, first.miss_on, "removed", "2026-10-10")
    assert same.action == "miss"
    assert same.miss == 1

    nxt = decide_probe(first.miss, first.miss_on, "removed", "2026-10-11")
    assert nxt.action == "vanish"
    assert nxt.miss == 2

    alive = decide_probe(1, "2026-10-10", "alive", "2026-10-11")
    assert alive.action == "alive"
    assert alive.miss == 0
    assert alive.miss_on is None


def test_classify_probe_body_statuses():
    assert classify_probe_body(404, "") == "removed"
    assert classify_probe_body(410, "gone") == "removed"
    assert classify_probe_body(500, "oops") == "skip"
    assert classify_probe_body(200, "<html>об'єкт продано</html>") == "sold"
    assert classify_probe_body(200, "<html>оголошення здано в оренду</html>") == "rented"
    assert classify_probe_body(200, "<html>оголошення видалено</html>") == "removed"
    assert (
        classify_probe_body(
            200,
            '<html><meta property="og:title" content="Офис">'
            "оголошення неактуальне чи інформація неточна?</html>",
        )
        == "alive"
    )
    assert classify_probe_body(200, ALIVE_HTML) == "alive"
    assert classify_probe_body(200, "<html>just a moment</html>") == "blocked"
    assert classify_probe_body(200, "<html><body>hello</body></html>") == "skip"


def test_listing_url_allowed_only_own_https_host():
    assert listing_url_allowed("lun", "https://lun.ua/realty/1")
    assert listing_url_allowed("olx", "https://www.olx.ua/d/uk/obyavlenie/x.html")
    assert listing_url_allowed("domria", "https://dom.ria.com/uk/realty-1.html")
    assert listing_url_allowed("m2bomber", "https://ua.m2bomber.com/obj/1/view/x")
    assert not listing_url_allowed("lun", "https://olx.ua/d/1")
    assert not listing_url_allowed("lun", "http://lun.ua/realty/1")
    assert not listing_url_allowed("lun", "https://user:pass@lun.ua/realty/1")
    assert not listing_url_allowed("lun", "https://127.0.0.1/realty/1")
    assert not listing_url_allowed("lun", "https://evil.example/lun.ua")


def test_probe_url_returns_404_and_stops_on_block(monkeypatch):
    from app.scrapers.http_utils import HttpClient

    slept = {"n": 0}
    monkeypatch.setattr(
        "app.scrapers.http_utils.sleep_crawl_delay",
        lambda **kwargs: slept.__setitem__("n", slept["n"] + 1),
    )
    client = HttpClient()
    client._pace = lambda *args, **kwargs: None
    client._use_tls_impersonate = lambda url: False

    class Resp:
        def __init__(self, code: int, text: str) -> None:
            self.status_code = code
            self.text = text

    client._client.get = lambda *args, **kwargs: Resp(404, "gone")
    assert client.probe_url("https://lun.ua/realty/1") == (404, "gone")
    assert slept["n"] == 0

    client._client.get = lambda *args, **kwargs: Resp(200, "<html>just a moment</html>")
    with pytest.raises(PortalBlockedError):
        client.probe_url("https://lun.ua/realty/1")
    assert slept["n"] == 0
    client.close()


def _init_db(tmp_path, monkeypatch):
    db_path = tmp_path / "probe.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    from app.config import get_settings
    from app.db import models as db_models
    from app.db.models import get_session_factory, init_db

    get_settings.cache_clear()
    db_models._engine = None
    db_models._SessionLocal = None
    init_db()
    return get_session_factory()


class _Parser:
    def parse_detail(self, html: str, raw):
        raw.price = 90_000
        raw.title = raw.title or "Офис"
        return raw


def _listing(db, *, external_id: str, url: str, raw_extra=None, price: float = 100_000):
    from app.db.models import Listing, Property
    from app.domain.enums import ListingStatus

    prop = Property(
        fingerprint=f"fp-{external_id}",
        deal_type="sale",
        property_type="office",
        title="Офис",
    )
    db.add(prop)
    db.flush()
    listing = Listing(
        source="lun",
        external_id=external_id,
        url=url,
        deal_type="sale",
        property_type="office",
        title="Офис",
        property_id=prop.id,
        status=ListingStatus.ACTIVE.value,
        price=price,
        currency="USD",
        area_sqm=120,
        first_seen_at=DAY1,
        last_seen_at=DAY1,
        raw_extra=raw_extra,
    )
    db.add(listing)
    db.commit()
    return listing


def test_probe_source_two_days_then_deal(tmp_path, monkeypatch):
    from sqlalchemy import func, select

    from app.db.models import DealHypothesis, Listing, PropertyEvent
    from app.domain.enums import EventType, ListingStatus
    from app.pipeline.probe_day import probe_source

    SessionLocal = _init_db(tmp_path, monkeypatch)
    with SessionLocal() as db:
        listing = _listing(db, external_id="l1", url="https://lun.ua/realty/101")
        lid = listing.id
        pid = listing.property_id

        def run(day):
            return probe_source(
                db,
                "lun",
                set(),
                deadline=10**9,
                fetch=lambda url: (404, ""),
                scraper=_Parser(),
                clock=lambda: 0,
                now_fn=lambda: day,
            )

        first = run(DAY1)
        assert first["miss"] == 1
        assert first["vanished"] == 0
        fresh = db.get(Listing, lid)
        assert fresh.status == ListingStatus.ACTIVE.value
        assert probe_miss(fresh.raw_extra) == (1, "2026-10-10")

        second = run(DAY2)
        assert second["vanished"] == 1
        fresh = db.get(Listing, lid)
        assert fresh.status == ListingStatus.VANISHED.value
        assert fresh.source_status_raw == "probe_removed"

        events = db.scalar(
            select(func.count())
            .select_from(PropertyEvent)
            .where(
                PropertyEvent.property_id == pid,
                PropertyEvent.event_type == EventType.VANISHED.value,
            )
        )
        assert events == 1
        hyp = db.scalar(select(DealHypothesis).where(DealHypothesis.property_id == pid))
        assert hyp is not None


def test_probe_source_same_day_miss_does_not_vanish(tmp_path, monkeypatch):
    from app.db.models import Listing
    from app.domain.enums import ListingStatus
    from app.pipeline.probe_day import probe_source

    SessionLocal = _init_db(tmp_path, monkeypatch)
    with SessionLocal() as db:
        listing = _listing(db, external_id="l2", url="https://lun.ua/realty/102")
        kwargs = dict(
            deadline=10**9,
            fetch=lambda url: (404, "no"),
            scraper=_Parser(),
            clock=lambda: 0,
        )
        probe_source(db, "lun", set(), now_fn=lambda: DAY1, **kwargs)
        probe_source(db, "lun", set(), now_fn=lambda: DAY1, **kwargs)
        fresh = db.get(Listing, listing.id)
        assert fresh.status == ListingStatus.ACTIVE.value
        assert probe_miss(fresh.raw_extra) == (1, "2026-10-10")


def test_probe_source_alive_records_price_drop(tmp_path, monkeypatch):
    from app.db.models import Listing
    from app.pipeline.probe_day import probe_source

    SessionLocal = _init_db(tmp_path, monkeypatch)
    with SessionLocal() as db:
        listing = _listing(db, external_id="l3", url="https://lun.ua/realty/103")
        stats = probe_source(
            db,
            "lun",
            set(),
            deadline=10**9,
            fetch=lambda url: (200, ALIVE_HTML),
            scraper=_Parser(),
            clock=lambda: 0,
            now_fn=lambda: DAY2,
        )
        assert stats["alive"] == 1
        fresh = db.get(Listing, listing.id)
        assert fresh.price == 90_000
        assert fresh.price_drop_count == 1
        assert probe_miss(fresh.raw_extra) == (0, None)


def test_probe_source_marks_explicit_sold(tmp_path, monkeypatch):
    from app.db.models import Listing
    from app.domain.enums import ListingStatus
    from app.pipeline.probe_day import probe_source

    SessionLocal = _init_db(tmp_path, monkeypatch)
    with SessionLocal() as db:
        listing = _listing(db, external_id="sold1", url="https://lun.ua/realty/501")
        stats = probe_source(
            db,
            "lun",
            set(),
            deadline=10**9,
            fetch=lambda url: (200, "<html>об'єкт продано</html>"),
            scraper=_Parser(),
            clock=lambda: 0,
            now_fn=lambda: DAY1,
        )
        assert stats["sold"] == 1
        fresh = db.get(Listing, listing.id)
        assert fresh.status == ListingStatus.SOLD_MARKED.value


def test_probe_source_orders_oldest_and_stops(tmp_path, monkeypatch):
    from app.pipeline.probe_day import probe_source

    SessionLocal = _init_db(tmp_path, monkeypatch)
    with SessionLocal() as db:
        _listing(
            db,
            external_id="old",
            url="https://lun.ua/realty/201",
            raw_extra={"probe": {"at": "2026-10-01T00:00:00+00:00", "miss": 0, "miss_on": None}},
        )
        _listing(db, external_id="new", url="https://lun.ua/realty/202")
        fetched: list[str] = []
        ticks = {"n": 0}

        def clock():
            ticks["n"] += 1
            return 0 if ticks["n"] == 1 else 100

        def fetch(url: str):
            fetched.append(url)
            return 404, ""

        stats = probe_source(
            db,
            "lun",
            set(),
            deadline=10,
            fetch=fetch,
            scraper=_Parser(),
            clock=clock,
            now_fn=lambda: DAY1,
        )
        assert fetched == ["https://lun.ua/realty/202"]
        assert stats["checked"] == 1
        assert stats["stopped"] == "deadline"


def test_probe_source_block_is_not_a_miss(tmp_path, monkeypatch):
    from app.db.models import Listing
    from app.domain.enums import ListingStatus
    from app.pipeline.probe_day import probe_source

    SessionLocal = _init_db(tmp_path, monkeypatch)
    with SessionLocal() as db:
        listing = _listing(db, external_id="b1", url="https://lun.ua/realty/301")
        _listing(db, external_id="b2", url="https://lun.ua/realty/302")
        fetched: list[str] = []

        def fetch(url: str):
            fetched.append(url)
            raise PortalBlockedError(403, url)

        stats = probe_source(
            db,
            "lun",
            set(),
            deadline=10**9,
            fetch=fetch,
            scraper=_Parser(),
            clock=lambda: 0,
            now_fn=lambda: DAY1,
        )
        assert stats["stopped"] == "blocked"
        assert stats["checked"] == 0
        assert len(fetched) == 1
        fresh = db.get(Listing, listing.id)
        assert fresh.status == ListingStatus.ACTIVE.value
        assert probe_miss(fresh.raw_extra) == (0, None)


def test_probe_source_skips_head_and_foreign_urls(tmp_path, monkeypatch):
    from app.pipeline.probe_day import probe_source

    SessionLocal = _init_db(tmp_path, monkeypatch)
    with SessionLocal() as db:
        _listing(db, external_id="seen", url="https://lun.ua/realty/401")
        _listing(db, external_id="bad", url="http://lun.ua/realty/402")
        fetched: list[str] = []
        stats = probe_source(
            db,
            "lun",
            {"seen"},
            deadline=10**9,
            fetch=lambda url: fetched.append(url) or (200, ALIVE_HTML),
            scraper=_Parser(),
            clock=lambda: 0,
            now_fn=lambda: DAY1,
        )
        assert fetched == []
        assert stats["in_head"] == 1
        assert stats["skipped"] == 1
        assert stats["checked"] == 0


def test_run_probe_day_strips_ids_and_respects_budget(monkeypatch):
    from app.pipeline import probe_day

    def fake_head(db, **kwargs):
        assert kwargs["include_ids"] is True
        assert kwargs["max_pages"] == 8
        assert kwargs["stale_page_limit"] == 2
        return {
            "ok": True,
            "mode": "light",
            "sources": {
                "lun": {"external_ids": ["a"], "cards": 1},
                "olx": {"error": "down"},
            },
        }

    calls: list[tuple] = []

    def fake_probe(source, seen, deadline):
        calls.append((source, set(seen), deadline))
        return source, {
            "checked": 0,
            "alive": 0,
            "miss": 0,
            "vanished": 0,
            "sold": 0,
            "rented": 0,
            "skipped": 0,
            "in_head": 0,
            "stopped": None,
        }

    monkeypatch.setattr(probe_day, "run_light_day", fake_head)
    monkeypatch.setattr(probe_day, "_probe_one_source", fake_probe)

    class Snap:
        day = "2026-10-12"

    monkeypatch.setattr(probe_day, "record_market_snapshot", lambda db, force=True: Snap())

    class DB:
        def expire_all(self) -> None:
            return None

    summary = probe_day.run_probe_day(DB(), sources=["lun", "olx"], minutes=40)
    assert summary["mode"] == "probe"
    assert summary["ok"] is True
    assert "external_ids" not in summary["head"]["sources"]["lun"]
    assert {row[0] for row in calls} == {"lun", "olx"}
    lun = next(row for row in calls if row[0] == "lun")
    assert lun[1] == {"a"}

    ticks = {"n": 0}

    def mono():
        ticks["n"] += 1
        if ticks["n"] <= 1:
            return 0.0
        return 10_000.0

    monkeypatch.setattr(probe_day.time, "monotonic", mono)
    calls.clear()
    summary = probe_day.run_probe_day(DB(), sources=["lun"], minutes=40)
    assert calls == []
    assert summary["probes"]["lun"]["stopped"] == "deadline"
    assert summary["elapsed_sec"] >= 0
