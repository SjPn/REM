import json
import re
from pathlib import Path

from bs4 import BeautifulSoup

html = Path("data/lun_list.html").read_text(encoding="utf-8")
soup = BeautifulSoup(html, "lxml")
for script in soup.find_all("script", type="application/ld+json"):
    data = json.loads(script.string)
    if isinstance(data, dict) and "itemListElement" in data:
        for i, el in enumerate(data["itemListElement"][:5]):
            item = el["item"]
            img = item.get("image")
            if isinstance(img, list):
                img = img[0] if img else None
            print(i, item.get("name"), img)
            print("  addr", item.get("address"))
            print("  geo", item.get("geo"))
            print("  offer", item.get("offers"))
        print("count", len(data["itemListElement"]))
        break

print("path realty", re.findall(r"/realty/\d+", html)[:30])
print("unique realty", sorted(set(re.findall(r"/realty/(\d+)", html)))[:30])

# active detail that is not 404
html2 = Path("data/raw/detail_https_lun_ua_realty_4716406883.html").read_text(encoding="utf-8")
print("robots", re.findall(r'name\":\"robots\",\"content\":\"([^\"]+)\"', html2))
print("status404", "statusCode\":404" in html2)
print("phones", re.findall(r"380\d{9}", html2)[:3])
# extract street from title/og
m = re.search(r'og:title\",\"content\":\"([^\"]+)\"', html2)
print("ogtitle", m.group(1) if m else None)
# floor in text
for key in ("поверх", "Площа", "м²", "агент", "рієлтор"):
    if key.lower() in html2.lower():
        print("has", key)
