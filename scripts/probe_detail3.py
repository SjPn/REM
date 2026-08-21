"""Fetch DOM.RIA / RIELTOR samples and mine LUN detail for JSON."""
from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "uk-UA,uk;q=0.9",
    "Accept": "text/html,application/xhtml+xml",
}


def fetch(url: str) -> httpx.Response:
    return httpx.get(url, headers=HEADERS, timeout=30, follow_redirects=True, verify=False)


def mine_lun_detail() -> None:
    path = next(Path("data/raw").glob("detail_https_lun_ua_realty_4717797520.html"))
    html = path.read_text(encoding="utf-8")
    # Find large JSON assignments
    patterns = [
        r"window\.__[A-Z_]+__\s*=\s*(\{.*?\});?\s*</script>",
        r"application/json[^>]*>(\{.*?\})</script>",
        r'"phoneNumber"\s*:\s*"([^"]+)"',
        r'"streetAddress"\s*:\s*"([^"]+)"',
        r'"addressLocality"\s*:\s*"([^"]+)"',
        r'"floor"\s*:\s*(\d+)',
        r'"area"\s*:\s*([0-9.]+)',
        r'"latitude"\s*:\s*([0-9.]+)',
        r'"longitude"\s*:\s*([0-9.]+)',
        r'"agencyName"\s*:\s*"([^"]+)"',
        r'"sellerName"\s*:\s*"([^"]+)"',
        r'"status"\s*:\s*"([^"]+)"',
        r'"isActive"\s*:\s*(true|false)',
        r'"archived"\s*:\s*(true|false)',
    ]
    for pat in patterns:
        found = re.findall(pat, html, flags=re.I | re.S)
        if found:
            print(pat[:50], "->", found[:5])

    # script type module / json
    soup = BeautifulSoup(html, "lxml")
    for script in soup.find_all("script"):
        txt = script.string or ""
        if not txt:
            continue
        if any(k in txt for k in ("phoneNumber", "streetAddress", "realtyId", "offerId")):
            print("SCRIPT HIT len", len(txt))
            # extract a compact slice around phone
            m = re.search(r".{0,80}phoneNumber.{0,120}", txt)
            if m:
                print(m.group(0))
            m = re.search(r".{0,80}streetAddress.{0,120}", txt)
            if m:
                print(m.group(0))


def probe_portal(name: str, list_urls: list[str], detail_hint: str) -> None:
    print("\n====", name)
    for u in list_urls:
        r = fetch(u)
        print(u, r.status_code, len(r.text))
        if r.status_code != 200:
            continue
        Path(f"data/{name}_list.html").write_text(r.text, encoding="utf-8")
        soup = BeautifulSoup(r.text, "lxml")
        # JSON-LD
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(script.string or "")
            except json.JSONDecodeError:
                continue
            print("LD type", data.get("@type") if isinstance(data, dict) else type(data))
            if isinstance(data, dict) and "itemListElement" in data:
                item = (data["itemListElement"] or [{}])[0]
                print("first", json.dumps(item, ensure_ascii=False)[:600])
        hrefs = []
        for a in soup.find_all("a", href=True):
            h = a["href"]
            if re.search(detail_hint, h):
                hrefs.append(urljoin(u, h))
        hrefs = list(dict.fromkeys(hrefs))
        print("detail hrefs", len(hrefs), hrefs[:8])
        if hrefs:
            dr = fetch(hrefs[0])
            print("detail", dr.status_code, dr.url, len(dr.text))
            if dr.status_code == 200:
                out = Path(f"data/raw/{name}_detail.html")
                out.write_text(dr.text, encoding="utf-8")
                ds = BeautifulSoup(dr.text, "lxml")
                print("title", ds.title.string if ds.title else None)
                phones = re.findall(r"\+?38\s?\(?0\d{2}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}", dr.text)
                print("phones", phones[:5])
                for word in ("продано", "здано", "архів", "неактуальн", "знято з публікації"):
                    if word in dr.text.lower():
                        print("status-hit", word)
                for script in ds.find_all("script", type="application/ld+json"):
                    print("detail-ld", (script.string or "")[:700])
            break


if __name__ == "__main__":
    mine_lun_detail()
    probe_portal(
        "rieltor",
        [
            "https://rieltor.ua/commercials-rent/",
            "https://rieltor.ua/commercials-sale/",
        ],
        r"-\d{5,}",
    )
    probe_portal(
        "domria",
        [
            "https://dom.ria.com/uk/arenda-kom-nedvizhimosti/",
            "https://dom.ria.com/uk/prodazha-kom-nedvizhimosti/",
            "https://dom.ria.com/uk/search/?category=2&realty_type=2&operation_type=3",
        ],
        r"realty-|/\d{6,}",
    )
