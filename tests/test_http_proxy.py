from __future__ import annotations

from app.scrapers.http_utils import HttpClient, _normalize_proxy


def test_normalize_proxy():
    assert _normalize_proxy(None) is None
    assert _normalize_proxy("") is None
    assert _normalize_proxy("  ") is None
    assert _normalize_proxy(" http://u:p@h:1 ") == "http://u:p@h:1"


def test_http_client_passes_proxy(monkeypatch):
    monkeypatch.setattr(
        "app.scrapers.http_utils.get_settings",
        lambda: type(
            "S",
            (),
            {
                "http_timeout_sec": 5.0,
                "http_verify_ssl": False,
                "http_proxy": "http://user:pass@127.0.0.1:8888",
                "user_agent": "test-agent",
            },
        )(),
    )
    client = HttpClient()
    assert client.proxy == "http://user:pass@127.0.0.1:8888"
    with client._client() as hx:
        assert hx._mounts  # proxy configures transport mounts


def test_http_client_no_proxy(monkeypatch):
    monkeypatch.setattr(
        "app.scrapers.http_utils.get_settings",
        lambda: type(
            "S",
            (),
            {
                "http_timeout_sec": 5.0,
                "http_verify_ssl": False,
                "http_proxy": None,
                "user_agent": "test-agent",
            },
        )(),
    )
    client = HttpClient()
    assert client.proxy is None
    with client._client() as hx:
        assert hx._mounts == {}
