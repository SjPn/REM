from __future__ import annotations

import re
from dataclasses import dataclass

from app.domain.enums import PropertyType

# Target product segments for RealEstateMonitor (REM).
TARGET_SEGMENTS = {
    "office",
    "retail",
    "showroom",
    "business_center",
    "street_retail",  # first-floor / facade retail
    "building",  # standalone commercial building
    "free_purpose",  # often first-floor commercial fit-out
}

EXCLUDE_SEGMENTS = {
    "warehouse",
    "industrial",
    "land_commercial",
    "logistics",
    "garage",
}

EXCLUDE_KEYWORDS = (
    "склад",
    "warehouse",
    "логіст",
    "логист",
    "виробниц",
    "производств",
    "промислов",
    "промышлен",
    "industrial",
    "цех",
    "ангар",
    "завод",
    "ділянка",
    "участок",
    "земл",
    "гараж",
    "паркоміс",
    "паркинг",
    "parking",
    "сто",
    "автосервіс",
    "автосервис",
    "с/г",
    "сільськогосподар",
)

TARGET_KEYWORDS = (
    "офіс",
    "офис",
    "office",
    "шоурум",
    "showroom",
    "show-room",
    "торгов",
    "магазин",
    "рітейл",
    "ритейл",
    "retail",
    "бізнес-центр",
    "бизнес-центр",
    "бізнес центр",
    "бизнес центр",
    "бц ",
    " бц",
    "фасад",
    "1 поверх",
    "1-й поверх",
    "перший поверх",
    "первый этаж",
    "street retail",
    "окрема будівля",
    "отдельно стоящ",
    "окремо стояч",
    "вільного призначення",
    "свободного назначения",
    "комерційн",
    "коммерческ",
    "приміщення",
    "помещение",
)


@dataclass(frozen=True)
class SegmentDecision:
    relevant: bool
    segment: str
    reason: str


def _blob(*parts: str | None) -> str:
    return " ".join(p for p in parts if p).lower()


def classify_segment(
    *,
    title: str | None = None,
    description: str | None = None,
    property_type: str | None = None,
    floor: int | None = None,
    address: str | None = None,
    url: str | None = None,
) -> SegmentDecision:
    text = _blob(title, description, address)
    url_l = (url or "").lower()

    # URL signals from portals (more reliable than noisy list-card HTML)
    if any(x in url_l for x in ("ofis", "office", "офис")):
        return SegmentDecision(True, "office", "url_office")
    if any(x in url_l for x in ("showroom", "шоурум")):
        return SegmentDecision(True, "showroom", "url_showroom")
    if any(x in url_l for x in ("torgov", "retail", "magazin", "kommerchesk")):
        # commercial premises — keep, refine below if title is more specific
        pass

    if any(k in text for k in EXCLUDE_KEYWORDS):
        if not any(k in text for k in ("офіс", "офис", "office", "шоурум", "бц")):
            return SegmentDecision(False, "excluded", "exclude_keyword")

    # Strong positives from title/address/description only (not page chrome)
    if any(k in text for k in ("шоурум", "showroom", "show-room")):
        return SegmentDecision(True, "showroom", "keyword_showroom")
    if any(k in text for k in ("бізнес-центр", "бизнес-центр", "бізнес центр", "бизнес центр")) or re.search(
        r"\bбц\b", text
    ):
        return SegmentDecision(True, "business_center", "keyword_bc")
    if any(k in text for k in ("окрема будівля", "отдельно стоящ", "окремо стояч")):
        return SegmentDecision(True, "building", "keyword_building")
    if any(k in text for k in ("офіс", "офис", "office")):
        return SegmentDecision(True, "office", "keyword_office")
    if any(k in text for k in ("торгов", "магазин", "рітейл", "ритейл", "retail")):
        segment = "street_retail" if floor == 1 or any(
            k in text for k in ("фасад", "1 поверх", "перший поверх", "первый этаж")
        ) else "retail"
        return SegmentDecision(True, segment, "keyword_retail")

    if floor == 1 and any(
        k in text for k in ("комерційн", "коммерческ", "приміщення", "помещение", "вільного", "свободного")
    ):
        return SegmentDecision(True, "street_retail", "first_floor_commercial")

    if property_type in {
        PropertyType.OFFICE.value,
        PropertyType.RETAIL.value,
        PropertyType.FREE_PURPOSE.value,
        PropertyType.SHOWROOM.value,
        PropertyType.BUSINESS_CENTER.value,
        PropertyType.STREET_RETAIL.value,
        PropertyType.BUILDING.value,
    }:
        # Do not trust previously mis-tagged showroom without evidence
        if property_type == PropertyType.SHOWROOM.value and "шоурум" not in text and "showroom" not in text:
            if any(x in url_l for x in ("ofis", "office")):
                return SegmentDecision(True, "office", "fix_showroom_via_url")
            return SegmentDecision(True, "free_purpose", "retag_bare_showroom")
        return SegmentDecision(True, property_type, "property_type")

    if property_type in EXCLUDE_SEGMENTS or property_type in {
        PropertyType.WAREHOUSE.value,
        PropertyType.INDUSTRIAL.value,
        PropertyType.LAND_COMMERCIAL.value,
    }:
        return SegmentDecision(False, property_type or "excluded", "excluded_type")

    if any(k in text for k in TARGET_KEYWORDS):
        return SegmentDecision(True, property_type or "other", "target_keyword")

    # Kyiv commercial listing with only address title — keep as free_purpose
    if title and len(title.strip()) >= 5:
        return SegmentDecision(True, "free_purpose", "address_like_title")

    return SegmentDecision(False, "excluded", "no_target_signal")


def is_relevant_listing(
    *,
    title: str | None = None,
    description: str | None = None,
    property_type: str | None = None,
    floor: int | None = None,
    address: str | None = None,
    url: str | None = None,
) -> bool:
    return classify_segment(
        title=title,
        description=description,
        property_type=property_type,
        floor=floor,
        address=address,
        url=url,
    ).relevant
