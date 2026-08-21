from __future__ import annotations

import json
import re
from pathlib import Path

from bs4 import BeautifulSoup

# LUN detail scripts
path = next(Path("data/raw").glob("detail_https_lun_ua_realty_4717797520.html"))
html = path.read_text(encoding="utf-8")
soup = BeautifulSoup(html, "lxml")
out = Path("data/raw/lun_scripts.txt")
chunks = []
for i, script in enumerate(soup.find_all("script")):
    txt = script.string or ""
    if any(k in txt for k in ("phone", "address", "realty", "latitude", "floor", "agency")):
        chunks.append(f"\n===== SCRIPT {i} len={len(txt)} =====\n{txt[:4000]}")
out.write_text("\n".join(chunks), encoding="utf-8")
print("wrote", out, "chunks", len(chunks))

# phones / address patterns in utf8
print("phones", re.findall(r"380\d{9}", html)[:5])
print("tel links", re.findall(r"tel:[^\s\"']+", html)[:5])

# RIELTOR list
rhtml = Path("data/rieltor_list.html")
if not rhtml.exists():
    # reload from commercials
    import httpx

    r = httpx.get(
        "https://rieltor.ua/commercials-rent/",
        headers={"User-Agent": "Mozilla/5.0"},
        verify=False,
        timeout=30,
        follow_redirects=True,
    )
    rhtml.write_text(r.text, encoding="utf-8")

html = rhtml.read_text(encoding="utf-8")
soup = BeautifulSoup(html, "lxml")
hrefs = sorted({a["href"] for a in soup.find_all("a", href=True)})
for h in hrefs:
    if re.search(r"\d{5,}", h) and "commercial" in h.lower():
        print("R", h)
print("total hrefs", len(hrefs))
# sample of numeric
nums = [h for h in hrefs if re.search(r"/\d{6,}(/|$)", h) or re.search(r"-\d{6,}", h)]
print("numeric", nums[:30])

# look for data-href / api
for m in re.finditer(r"https://rieltor\.ua/[a-z0-9\-_/]{10,80}", html):
    u = m.group(0)
    if "commercial" in u and re.search(r"\d", u):
        print("abs", u)
        break

# DOM detail LD parse
dpath = Path("data/raw/domria_detail.html")
if dpath.exists():
    dhtml = dpath.read_text(encoding="utf-8")
    dsoup = BeautifulSoup(dhtml, "lxml")
    for script in dsoup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except json.JSONDecodeError:
            continue
        Path("data/raw/domria_ld.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("dom ld dumped")
        break
    phones = re.findall(r"380\d{9}", dhtml)
    print("dom phones", phones[:5])
    # common domria phone hide
    for pat in (r"data-phone=\"([^\"]+)\"", r"phone_hash", r"showPhone", r"'phone'\s*:\s*'([^']+)'"):
        print(pat, re.findall(pat, dhtml)[:3])
