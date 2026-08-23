"""Text helpers for scrapers (encoding, cleanup)."""

from __future__ import annotations

import re


_MOJIBAKE_MARKERS = ("Ð", "Ñ", "Ã", "Â")


def looks_like_mojibake(text: str | None) -> bool:
    if not text:
        return False
    hits = sum(text.count(m) for m in _MOJIBAKE_MARKERS)
    return hits >= 2 and any("\u0400" <= ch <= "\u04ff" for ch in text) is False


def fix_mojibake(text: str | None) -> str | None:
    """Undo UTF-8 interpreted as Latin-1 / broken unicode_escape round-trip."""
    if not text:
        return text
    if not looks_like_mojibake(text) and "Ð" not in text and "Ñ" not in text:
        return text
    try:
        fixed = text.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text
    # Prefer fixed if it gained Cyrillic / lost mojibake markers
    if looks_like_mojibake(fixed):
        return text
    if any("\u0400" <= ch <= "\u04ff" for ch in fixed) or fixed.count("Ð") < text.count("Ð"):
        return fixed
    return text


def decode_js_escaped_json(raw: str) -> str:
    """Decode a JS-string-escaped JSON blob without destroying UTF-8 Cyrillic."""
    return raw.encode("utf-8").decode("unicode_escape").encode("latin-1").decode("utf-8")


_WS = re.compile(r"\s+")


def clean_text(text: str | None, *, limit: int | None = None) -> str | None:
    if not text:
        return None
    out = fix_mojibake(_WS.sub(" ", text).strip())
    if not out:
        return None
    if limit is not None:
        out = out[:limit]
    return out
