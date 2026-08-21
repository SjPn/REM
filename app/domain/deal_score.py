from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.config import get_settings
from app.domain.enums import DealBucket, DealType


@dataclass
class ScoreFeature:
    code: str
    points: int
    detail: str


@dataclass
class DealScoreResult:
    score: int
    bucket: DealBucket
    features: list[ScoreFeature] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "bucket": self.bucket.value,
            "features": [
                {"code": f.code, "points": f.points, "detail": f.detail}
                for f in self.features
            ],
        }


@dataclass
class DealScoreInput:
    deal_type: DealType
    vanished_at: datetime
    first_seen_at: datetime
    last_price: float | None = None
    previous_price: float | None = None
    price_drop_count: int = 0
    active_on_other_sources: int = 0
    vanished_on_sources: int = 1
    tracked_sources_for_property: int = 1
    explicit_sold_or_rented: bool = False
    agency_bulk_delist: bool = False
    relisted_soon: bool = False


def _days_between(a: datetime, b: datetime) -> float:
    if a.tzinfo is None:
        a = a.replace(tzinfo=timezone.utc)
    if b.tzinfo is None:
        b = b.replace(tzinfo=timezone.utc)
    return abs((b - a).total_seconds()) / 86400.0


def score_deal(inp: DealScoreInput) -> DealScoreResult:
    settings = get_settings()
    features: list[ScoreFeature] = []
    score = 0

    if inp.explicit_sold_or_rented:
        features.append(
            ScoreFeature("explicit_status", 25, "Площадка явно пометила продано/сдано")
        )
        score += 25

    if (
        inp.tracked_sources_for_property >= 2
        and inp.vanished_on_sources >= inp.tracked_sources_for_property
    ):
        features.append(
            ScoreFeature(
                "vanished_all_sources",
                45,
                f"Исчезло со всех отслеживаемых источников ({inp.vanished_on_sources})",
            )
        )
        score += 45
    elif inp.vanished_on_sources >= 2:
        features.append(
            ScoreFeature(
                "vanished_multi_source",
                25,
                f"Исчезло с нескольких источников ({inp.vanished_on_sources})",
            )
        )
        score += 25
    else:
        features.append(
            ScoreFeature("vanished_single_source", 10, "Исчезло с одного источника")
        )
        score += 10

    if inp.price_drop_count > 0 or (
        inp.last_price is not None
        and inp.previous_price is not None
        and inp.last_price < inp.previous_price
    ):
        pts = min(15, 10 + 5 * max(inp.price_drop_count, 1))
        features.append(
            ScoreFeature(
                "price_drop",
                pts,
                f"Снижение цены перед исчезновением (drops={inp.price_drop_count})",
            )
        )
        score += pts

    days_on_market = _days_between(inp.first_seen_at, inp.vanished_at)
    # Typical windows differ for rent vs sale
    if inp.deal_type == DealType.RENT:
        typical = 7 <= days_on_market <= 90
    else:
        typical = 14 <= days_on_market <= 180

    if typical:
        features.append(
            ScoreFeature(
                "typical_dom",
                10,
                f"Время в рынке в типичном окне сделки ({days_on_market:.0f} дн.)",
            )
        )
        score += 10

    if days_on_market < settings.vanish_too_fast_days:
        features.append(
            ScoreFeature(
                "too_fast",
                -30,
                f"Снято слишком быстро ({days_on_market:.1f} дн.)",
            )
        )
        score -= 30

    if inp.active_on_other_sources > 0:
        features.append(
            ScoreFeature(
                "alive_elsewhere",
                -25,
                f"Ещё активно на других источниках ({inp.active_on_other_sources})",
            )
        )
        score -= 25

    if inp.agency_bulk_delist:
        features.append(
            ScoreFeature("agency_bulk", -15, "Похоже на массовую чистку агентства")
        )
        score -= 15

    if inp.relisted_soon:
        features.append(
            ScoreFeature("relisted", -35, "Объект быстро переопубликован")
        )
        score -= 35

    score = max(0, min(100, score))
    if score >= settings.deal_likely_min:
        bucket = DealBucket.LIKELY_DEAL
    elif score >= settings.deal_ambiguous_min:
        bucket = DealBucket.AMBIGUOUS
    else:
        bucket = DealBucket.LIKELY_WITHDRAWN

    return DealScoreResult(score=score, bucket=bucket, features=features)
