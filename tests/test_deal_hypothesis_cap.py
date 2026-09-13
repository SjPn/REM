"""Partial crawl must not demote an existing likely_deal hypothesis."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.db.models import Listing, Property
from app.domain.enums import DealBucket, ListingStatus
from app.pipeline.reconcile import create_or_update_deal_hypothesis


def _init_db(tmp_path, monkeypatch, db_name: str = "cap.db"):
    db_path = tmp_path / db_name
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    from app.config import get_settings
    from app.db import models as db_models
    from app.db.models import get_session_factory, init_db

    get_settings.cache_clear()
    db_models._engine = None
    db_models._SessionLocal = None
    init_db()
    return get_session_factory()


def _multi_vanished(db, *, fp: str, now: datetime):
    prop = Property(fingerprint=fp, deal_type="rent")
    db.add(prop)
    db.flush()
    lun = Listing(
        source="lun",
        external_id=f"{fp}-lun",
        url=f"https://e/{fp}-lun",
        deal_type="rent",
        property_id=prop.id,
        status=ListingStatus.VANISHED.value,
        first_seen_at=now - timedelta(days=40),
        last_seen_at=now - timedelta(days=14),
        vanished_at=now - timedelta(days=14),
        price=2200,
        currency="USD",
        price_drop_count=1,
    )
    dom = Listing(
        source="domria",
        external_id=f"{fp}-dom",
        url=f"https://e/{fp}-dom",
        deal_type="rent",
        property_id=prop.id,
        status=ListingStatus.VANISHED.value,
        first_seen_at=now - timedelta(days=40),
        last_seen_at=now - timedelta(days=14),
        vanished_at=now - timedelta(days=14),
        price=2200,
        currency="USD",
    )
    db.add_all([lun, dom])
    db.commit()
    db.refresh(lun)
    return prop, lun


def test_partial_crawl_preserves_existing_likely_deal(tmp_path, monkeypatch):
    SessionLocal = _init_db(tmp_path, monkeypatch)
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        _prop, listing = _multi_vanished(db, fp="keep-likely", now=now)

        hyp = create_or_update_deal_hypothesis(db, listing, allow_likely_deal=True)
        db.commit()
        assert hyp is not None
        assert hyp.bucket == DealBucket.LIKELY_DEAL.value
        hyp_id = hyp.id

        again = create_or_update_deal_hypothesis(
            db, listing, allow_likely_deal=False
        )
        db.commit()
        assert again is not None
        assert again.id == hyp_id
        assert again.bucket == DealBucket.LIKELY_DEAL.value
        assert not (again.features or {}).get("capped_partial_crawl")


def test_partial_crawl_caps_new_likely_without_explicit(tmp_path, monkeypatch):
    SessionLocal = _init_db(tmp_path, monkeypatch, "cap2.db")
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        _prop, lun = _multi_vanished(db, fp="new-cap", now=now)

        hyp = create_or_update_deal_hypothesis(db, lun, allow_likely_deal=False)
        db.commit()
        assert hyp is not None
        # Multi-source + aging would be likely, but cap blocks first promotion.
        assert hyp.bucket == DealBucket.AMBIGUOUS.value
        assert (hyp.features or {}).get("capped_partial_crawl") is True
