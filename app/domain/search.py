"""Text search helpers (SQLite ILIKE is ASCII-only for case-folding)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import or_

from app.db.models import Listing
from app.domain.fingerprint import normalize_text

# Russian keyboard / spelling ↔ Ukrainian in street names.
_UA_RU_SWAPS = (
    ("и", "і"),
    ("е", "є"),
    ("г", "ґ"),
    ("И", "І"),
    ("Е", "Є"),
    ("Г", "Ґ"),
)

# Trailing house number: 70, 70а, 40/85, 70-А
_HOUSE_IN_QUERY_RE = re.compile(
    r"^(?P<street>.+?)[\s,]+(?P<house>\d{1,4}(?:/\d{1,4})?[-\s]?[а-яіїєґa-zA-Z]?)\s*$",
    re.IGNORECASE | re.UNICODE,
)
_HOUSE_PARSE_RE = re.compile(
    r"^(?P<main>\d{1,4})(?:/(?P<sub>\d{1,4}))?[-\s]?(?P<letter>[а-яіїєґa-zA-Z])?$",
    re.IGNORECASE | re.UNICODE,
)


def _unify_cyrillic(text: str) -> str:
    out = text
    for a, b in _UA_RU_SWAPS:
        out = out.replace(a, b)
    return out


@dataclass(frozen=True)
class AddressSearchQuery:
    raw: str
    street: str
    house: str | None = None


def parse_address_search_query(q: str | None) -> AddressSearchQuery | None:
    """Split «Саксаганського 70» into street + optional house number."""
    raw = (q or "").strip()
    if not raw:
        return None
    m = _HOUSE_IN_QUERY_RE.match(raw)
    if m:
        street = m.group("street").strip(" ,")
        house = re.sub(r"\s+", "", m.group("house") or "")
        if street and house:
            return AddressSearchQuery(raw=raw, street=street, house=house)
    return AddressSearchQuery(raw=raw, street=raw, house=None)


def text_has_house_number(text: str | None, house: str) -> bool:
    """True if text contains this building number (70 matches 70А / 70/1, not 700)."""
    if not text or not house:
        return False
    parsed = _HOUSE_PARSE_RE.match(house.strip())
    if not parsed:
        return house.lower() in text.lower()
    main = parsed.group("main")
    sub = parsed.group("sub")
    letter = (parsed.group("letter") or "").lower()
    if sub:
        pat = rf"(?<!\d){re.escape(main)}/{re.escape(sub)}[а-яіїєґa-z]?(?!\d)"
    elif letter:
        pat = rf"(?<!\d){re.escape(main)}[-\s]?{re.escape(letter)}(?!\d)"
    else:
        # Exact number with optional flat suffix letter or /wing.
        pat = rf"(?<!\d){re.escape(main)}(?:/\d{{1,4}})?[-\s]?[а-яіїєґa-z]?(?!\d)"
    return bool(re.search(pat, text, re.IGNORECASE | re.UNICODE))


def _listing_search_blob(listing: Listing) -> str:
    return " ".join(
        p
        for p in (
            listing.title,
            listing.address_raw,
            listing.district,
            listing.city,
        )
        if p
    )


def _street_matches_blob(street: str, blob: str) -> bool:
    street_n = _unify_cyrillic(normalize_text(street))
    blob_n = _unify_cyrillic(normalize_text(blob))
    if not street_n or not blob_n:
        return False
    if street_n in blob_n:
        return True
    # Stem: first significant token / prefix (handles UA/RU endings).
    token = street_n.split()[0] if street_n.split() else street_n
    stem_len = min(max(4, len(token) - 2), 10, len(token))
    stem = token[:stem_len]
    return len(stem) >= 4 and stem in blob_n


def search_relevance_tier(listing: Listing, q: str | None) -> int:
    """Higher = better. 2 = street+house, 1 = street (or plain text hit), 0 = weak."""
    parsed = parse_address_search_query(q)
    if parsed is None:
        return 0
    blob = _listing_search_blob(listing)
    street_ok = _street_matches_blob(parsed.street, blob)
    if parsed.house:
        house_ok = text_has_house_number(blob, parsed.house)
        if street_ok and house_ok:
            return 2
        if street_ok:
            return 1
        return 0
    return 1 if street_ok else 0


def search_query_variants(q: str, *, limit: int = 64) -> list[str]:
    """Expand query for Cyrillic case + UA/RU + street-name stems."""
    raw = (q or "").strip()
    if not raw:
        return []

    parsed = parse_address_search_query(raw)
    # For «street N» expand the street part so recall keeps other house numbers.
    seed_text = parsed.street if parsed and parsed.house else raw

    seeds: set[str] = {raw, seed_text}
    norm = _unify_cyrillic(normalize_text(seed_text))
    if norm:
        seeds.add(norm)
        if len(norm) >= 4:
            for n in range(4, min(len(norm), 12) + 1):
                seeds.add(norm[:n])

    expanded: set[str] = set()
    stack = list(seeds)
    while stack:
        cur = stack.pop()
        if cur in expanded:
            continue
        expanded.add(cur)
        for a, b in _UA_RU_SWAPS:
            for x, y in ((a, b), (b, a)):
                if x not in cur:
                    continue
                nv = cur.replace(x, y)
                if nv not in expanded:
                    stack.append(nv)

    with_case: set[str] = set()
    for v in expanded:
        with_case.add(v)
        with_case.add(v.lower())
        if v:
            with_case.add(v[0].upper() + v[1:])

    out = [v for v in with_case if len(v) >= 3]
    out.sort(key=len, reverse=True)
    return out[:limit]


def listing_text_search_filter(q: str | None):
    """SQLAlchemy OR filter for listing text fields."""
    variants = search_query_variants(q or "")
    if not variants:
        return None
    parts = []
    for v in variants:
        like = f"%{v}%"
        parts.extend(
            [
                Listing.title.ilike(like),
                Listing.address_raw.ilike(like),
                Listing.district.ilike(like),
                Listing.city.ilike(like),
            ]
        )
    return or_(*parts)
