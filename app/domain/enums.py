from __future__ import annotations

from enum import StrEnum


class DealType(StrEnum):
    SALE = "sale"
    RENT = "rent"


class PropertyType(StrEnum):
    OFFICE = "office"
    RETAIL = "retail"
    SHOWROOM = "showroom"
    BUSINESS_CENTER = "business_center"
    STREET_RETAIL = "street_retail"
    BUILDING = "building"
    WAREHOUSE = "warehouse"
    INDUSTRIAL = "industrial"
    FREE_PURPOSE = "free_purpose"
    LAND_COMMERCIAL = "land_commercial"
    OTHER = "other"
    EXCLUDED = "excluded"


class ListingStatus(StrEnum):
    ACTIVE = "active"
    VANISHED = "vanished"
    SOLD_MARKED = "sold_marked"
    RENTED_MARKED = "rented_marked"
    RELISTED = "relisted"
    UNKNOWN = "unknown"


class EventType(StrEnum):
    APPEARED = "appeared"
    PRICE_CHANGED = "price_changed"
    STATUS_CHANGED = "status_changed"
    VANISHED = "vanished"
    RELISTED = "relisted"
    CONTENT_CHANGED = "content_changed"


class DealBucket(StrEnum):
    LIKELY_DEAL = "likely_deal"
    AMBIGUOUS = "ambiguous"
    LIKELY_WITHDRAWN = "likely_withdrawn"


class SourceName(StrEnum):
    LUN = "lun"
    DOMRIA = "domria"
    OLX = "olx"
    RIELTOR = "rieltor"
    M2BOMBER = "m2bomber"


class Currency(StrEnum):
    USD = "USD"
    UAH = "UAH"
    EUR = "EUR"
