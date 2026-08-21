from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class RawListing:
    """Normalized listing payload produced by any scraper."""

    source: str
    external_id: str
    url: str
    deal_type: str
    title: str | None = None
    description: str | None = None
    property_type: str | None = None
    price: float | None = None
    currency: str | None = None
    price_per_sqm: float | None = None
    area_sqm: float | None = None
    floor: int | None = None
    rooms: int | None = None
    address_raw: str | None = None
    district: str | None = None
    city: str | None = None
    lat: float | None = None
    lon: float | None = None
    phone: str | None = None
    agency: str | None = None
    source_status_raw: str | None = None
    published_at: datetime | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        if self.published_at:
            data["published_at"] = self.published_at.isoformat()
        return data
