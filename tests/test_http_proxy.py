from __future__ import annotations

from app.scrapers.http_utils import (
    HttpClient,
    _normalize_proxy,
    browser_headers,
    human_delay_seconds,
    pick_user_agent,
)


def test_normalize_proxy():
    assert _normalize_proxy(None) is None
    assert _normalize_proxy("") is None
    assert _normalize_proxy("  ") is None
    assert _normalize_proxy(" http://u:p@h:1 ") == "http://u:p@h:1"


def _settings(**kwargs):
    base = {
        "http_timeout_sec": 5.0,
        "http_verify_ssl": False,
        "http_proxy": None,
        "user_agent": "test-agent",
        "crawl_human_mode": False,
        "crawl_delay_sec": 1.0,
        "crawl_delay_jitter_sec": 0.0,
        "crawl_block_backoff_sec": 8.0,
        "crawl_break_every_min": 10,
        "crawl_break_every_max": 18,
        "crawl_break_sec_min": 25.0,
        "crawl_break_sec_max": 70.0,
    }
    base.update(kwargs)
    return type("S", (), base)()


def test_http_client_passes_proxy(monkeypatch):
    monkeypatch.setattr(
        "app.scrapers.http_utils.get_settings",
        lambda: _settings(http_proxy="http://user:pass@127.0.0.1:8888"),
    )
    client = HttpClient()
    assert client.proxy == "http://user:pass@127.0.0.1:8888"
    assert client._client._mounts  # proxy configures transport mounts
    client.close()


def test_http_client_no_proxy(monkeypatch):
    monkeypatch.setattr(
        "app.scrapers.http_utils.get_settings",
        lambda: _settings(http_proxy=None),
    )
    client = HttpClient()
    assert client.proxy is None
    assert client._client._mounts == {}
    client.close()


def test_pick_user_agent_fixed_and_pool():
    assert pick_user_agent("MyBot/1.0") == "MyBot/1.0"
    ua = pick_user_agent("")
    assert "Mozilla" in ua


def test_browser_headers_have_sec_fetch():
    h = browser_headers("Mozilla/5.0 Chrome/131.0.0.0", referer="https://lun.ua/x", same_site=True)
    assert h["Referer"] == "https://lun.ua/x"
    assert h["Sec-Fetch-Site"] == "same-origin"
    assert "sec-ch-ua" in h


def test_human_delay_blocked_longer(monkeypatch):
    monkeypatch.setattr(
        "app.scrapers.http_utils.get_settings",
        lambda: _settings(crawl_human_mode=False, crawl_delay_sec=1.0, crawl_block_backoff_sec=20.0),
    )
    monkeypatch.setattr("app.scrapers.http_utils.random.uniform", lambda a, b: 0.0)
    assert human_delay_seconds() == 1.0
    assert human_delay_seconds(blocked=True) == 20.0
