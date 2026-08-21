"""Temporary probe script for portal HTML structures."""
from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

RAW = Path("data/raw")
RAW.mkdir(parents=True, exist_ok=True)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8",
}


def fetch(url: str) -> str:
    r = httpx.get(url, headers=HEADERS, timeout=30, follow_redirects=True, verify=False)
    r.raise_for_status()
    return r.text


def analyze_lun_list() -> list[str]:
    html = Path("data/lun_list.html").read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "lxml")
    urls: list[str] = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        types = data.get("@type")
        if "ItemList" not in str(types):
            continue
        for el in data.get("itemListElement") or []:
            item = el.get("item") or {}
            print("ITEM keys", sorted(item.keys()))
            print(json.dumps(item, ensure_ascii=False)[:1200])
            url = item.get("url")
            if url:
                urls.append(url)
            # maybe id in offers
            offer = item.get("offers") or {}
            print("offer", offer)
            break
        break

    # fallback ids
    ids = sorted(set(re.findall(r"realty[/\-](\d{6,})", html)))
    print("ids", ids[:20])
    for i in ids[:5]:
        urls.append(f"https://lun.ua/realty/{i}")
        urls.append(f"https://lun.ua/a/{i}")
        urls.append(f"https://lun.ua/uk/realty/{i}")
    return urls


def probe_detail(url: str) -> None:
    print("\n=== DETAIL", url)
    try:
        html = fetch(url)
    except Exception as exc:  # noqa: BLE001
        print("fail", exc)
        return
    path = RAW / ("detail_" + re.sub(r"\W+", "_", url)[-80:] + ".html")
    path.write_text(html, encoding="utf-8")
    print("saved", path, "len", len(html), "final?")
    soup = BeautifulSoup(html, "lxml")
    print("title", (soup.title.string if soup.title else None))
    for script in soup.find_all("script", type="application/ld+json"):
        txt = (script.string or "")[:1500]
        print("LD", txt)
    # phones
    phones = set(re.findall(r"\+?38\s?\(?0\d{2}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}", html))
    print("phones", list(phones)[:10])
    # status words
    for word in ("продано", "здано", "знято", "архів", "неактуальн", "sold", "rented"):
        if word in html.lower():
            print("status-hit", word)
    # coords
    for pat in (r'"lat(?:itude)?"\s*:\s*([0-9.]+)', r'"lon(?:gitude)?"\s*:\s*([0-9.]+)'):
        m = re.search(pat, html)
        if m:
            print("coord", pat, m.group(1))


def main() -> None:
    urls = analyze_lun_list()
    # also grab other portals list + one detail
    for name, url in [
        ("olx", "https://www.olx.ua/uk/nedvizhimost/kommercheskaya-nedvizhimost/arenda-pomescheniy/kiev/"),
        ("domria", "https://dom.ria.com/uk/realty-arenda-kommercheskaya-nedvizhimost-kiev/"),
        ("rieltor", "https://rieltor.ua/kyiv/commerce-rent/"),
    ]:
        try:
            html = fetch(url)
            Path(f"data/{name}_list.html").write_text(html, encoding="utf-8")
            print(name, "list ok", len(html))
            soup = BeautifulSoup(html, "lxml")
            hrefs = []
            for a in soup.find_all("a", href=True):
                h = a["href"]
                if name == "olx" and ("/d/" in h or "ID" in h):
                    hrefs.append(h)
                if name == "domria" and re.search(r"\d{6,}", h) and "realty" in h:
                    hrefs.append(h)
                if name == "rieltor" and re.search(r"-\d{5,}", h):
                    hrefs.append(h)
            print(name, "hrefs", len(hrefs), hrefs[:5])
            if hrefs:
                from urllib.parse import urljoin

                base = {
                    "olx": "https://www.olx.ua",
                    "domria": "https://dom.ria.com",
                    "rieltor": "https://rieltor.ua",
                }[name]
                urls.append(urljoin(base, hrefs[0]))
        except Exception as exc:  # noqa: BLE001
            print(name, "fail", exc)

    seen = set()
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        probe_detail(u)
        if len(seen) > 12:
            break


if __name__ == "__main__":
    main()
