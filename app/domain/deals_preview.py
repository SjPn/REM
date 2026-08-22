"""Recent vanished listings scored as possible deals (F5-style, separate from active medians)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import DealHypothesis, Listing


def _since(hours: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=hours)


def recent_deal_hypotheses(
    db: Session,
    *,
    deal_type: str,
    bucket: str | None = "likely_deal",
    hours: int = 168,
    limit: int = 5,
) -> list[DealHypothesis]:
    """Top scored hypotheses for a deal mode (default: last 7 days)."""
    q = (
        select(DealHypothesis)
        .join(Listing, DealHypothesis.listing_id == Listing.id)
        .where(Listing.deal_type == deal_type)
        .order_by(DealHypothesis.score.desc(), DealHypothesis.created_at.desc())
        .limit(limit)
    )
    if hours > 0:
        q = q.where(DealHypothesis.created_at >= _since(hours))
    if bucket:
        q = q.where(DealHypothesis.bucket == bucket)
    return list(db.scalars(q).all())


def deal_bucket_counts(
    db: Session,
    *,
    deal_type: str,
    hours: int | None = 168,
) -> dict[str, int]:
    """Counts per bucket for the deals UI tabs."""
    q = (
        select(DealHypothesis.bucket, func.count())
        .join(Listing, DealHypothesis.listing_id == Listing.id)
        .where(Listing.deal_type == deal_type)
        .group_by(DealHypothesis.bucket)
    )
    if hours is not None and hours > 0:
        q = q.where(DealHypothesis.created_at >= _since(hours))
    rows = db.execute(q).all()
    by_bucket = {str(b): int(n) for b, n in rows}
    total = sum(by_bucket.values())
    return {
        "likely_deal": by_bucket.get("likely_deal", 0),
        "ambiguous": by_bucket.get("ambiguous", 0),
        "likely_withdrawn": by_bucket.get("likely_withdrawn", 0),
        "all": total,
    }
