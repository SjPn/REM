"""Deeper analysis of saved LUN detail + discover other portal URLs."""
from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "uk-UA,uk;q=0.9",
}


def fetch(url: str) -> str:
    r = httpx.get(url, headers=HEADERS, timeout=30, follow_redirects=True, verify=False)
    print("GET", url, "->", r.status_code, r.url, len(r.text))
    r.raise_for_status()
    return r.text


def analyze_detail(path: Path) -> None:
    html = path.read_text(encoding="utf-8")
    print("\nFILE", path.name)
    # embedded JSON blobs
    for m in re.finditer(r"<script[^>]*>(\{.*?\})</script>", html, re.S):
        blob = m.group(1)
        if "phone" in blob.lower() or "address" in blob.lower() or "latitude" in blob:
            if len(blob) < 5000:
                print("small blob", blob[:800])
    # meta tags
    soup = BeautifulSoup(html, "lxml")
    for meta in soup.find_all("meta"):
        prop = meta.get("property") or meta.get("name") or ""
        if any(x in prop.lower() for x in ("og:", "description", "title")):
            print("meta", prop, (meta.get("content") or "")[:120])
    # dt/dd or label rows
    text = soup.get_text("\n", strip=True)
    for key in ("Адреса", "Площа", "Поверх", "Телефон", "Ціна", "Район", "Агентство", "Рієлтор"):
        idx = text.find(key)
        if idx >= 0:
            print("near", key, repr(text[idx : idx + 80]))
    # tel: links
    for a in soup.select('a[href^="tel:"]')[:5]:
        print("tel", a.get("href"), a.get_text(strip=True))
    # status badges
    for cls in ("archived", "sold", "inactive", "not-actual", "closed"):
        if cls in html.lower():
            print("class-hit", cls)


def try_urls() -> None:
    candidates = [
        "https://www.olx.ua/uk/nedvizhimost/kiev/kommercheskaya-nedvizhimost/",
        "https://www.olx.ua/nedvizhimost/kiev/kommercheskaya-nedvizhimost/",
        "https://www.olx.ua/uk/nedvizhimost/kommercheskaya-nedvizhimost/kiev/",
        "https://dom.ria.com/uk/search/?category=2",
        "https://dom.ria.com/uk/",
        "https://rieltor.ua/commerce/",
        "https://rieltor.ua/kyiv/",
        "https://rieltor.ua/",
    ]
    for u in candidates:
        try:
            html = fetch(u)
            Path("data/raw").mkdir(exist_ok=True)
            name = re.sub(r"\W+", "_", u)[-60:]
            Path(f"data/raw/{name}.html").write_text(html, encoding="utf-8")
            soup = BeautifulSoup(html, "lxml")
            hrefs = [a["href"] for a in soup.find_all("a", href=True)]
            interesting = [
                h
                for h in hrefs
                if any(
                    x in h
                    for x in (
                        "commerce",
                        "commercial",
                        "kommerchesk",
                        "office",
                        "/d/",
                        "realty-",
                        "arenda",
                        "prodazha",
                    )
                )
            ]
            print(" interesting", interesting[:15])
        except Exception as exc:  # noqa: BLE001
            print("fail", u, exc)


if __name__ == "__main__":
    details = list(Path("data/raw").glob("detail_https_lun_ua_realty_*.html"))
    if details:
        analyze_detail(details[0])
    try_urls()
