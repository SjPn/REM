from app.domain.deal_score import DealScoreInput, score_deal
from app.domain.enums import DealBucket, DealType
from app.domain.fingerprint import (
    FingerprintInput,
    build_fingerprint,
    normalize_address,
    round_price_band,
)
from datetime import datetime, timedelta, timezone


def test_fingerprint_stable_for_same_object():
    a = FingerprintInput(
        address="вул. Богдана Хмельницького, 16",
        area_sqm=120,
        floor=4,
        price=2200,
        property_type="office",
        deal_type="rent",
    )
    b = FingerprintInput(
        address="вул Богдана Хмельницького 16",
        area_sqm=120.4,
        floor=4,
        price=2280,
        property_type="office",
        deal_type="rent",
    )
    assert build_fingerprint(a) == build_fingerprint(b)


def test_price_band_groups_small_changes():
    assert round_price_band(100_000, "sale") == round_price_band(102_000, "sale")
    assert round_price_band(2200, "rent") == round_price_band(2280, "rent")
    assert round_price_band(100_000, "sale") != round_price_band(120_000, "sale")


def test_phone_used_only_when_weak_location():
    base = FingerprintInput(
        address="вул. Хрещатик, 1",
        area_sqm=100,
        floor=2,
        price=100_000,
        property_type="office",
        deal_type="sale",
        lat=50.45,
        lon=30.52,
    )
    with_phone = FingerprintInput(**{**base.__dict__, "phone": "380501112233"})
    assert build_fingerprint(base) == build_fingerprint(with_phone)

    weak = FingerprintInput(
        address=None,
        area_sqm=None,
        price=1000,
        deal_type="rent",
        phone="380501112233",
    )
    weak_other = FingerprintInput(**{**weak.__dict__, "phone": "380509998877"})
    assert build_fingerprint(weak) != build_fingerprint(weak_other)


def test_normalize_address_strips_noise():
    assert "київ" not in normalize_address("Київ, вул. Хрещатик, 1")


def test_deal_score_likely_multi_source():
    now = datetime.now(timezone.utc)
    result = score_deal(
        DealScoreInput(
            deal_type=DealType.RENT,
            vanished_at=now,
            first_seen_at=now - timedelta(days=40),
            last_price=2200,
            previous_price=2500,
            price_drop_count=1,
            active_on_other_sources=0,
            vanished_on_sources=2,
            tracked_sources_for_property=2,
        )
    )
    assert result.score >= 70
    assert result.bucket == DealBucket.LIKELY_DEAL


def test_deal_score_fast_single_source_withdrawn():
    now = datetime.now(timezone.utc)
    result = score_deal(
        DealScoreInput(
            deal_type=DealType.RENT,
            vanished_at=now,
            first_seen_at=now - timedelta(days=1),
            vanished_on_sources=1,
            tracked_sources_for_property=1,
            active_on_other_sources=0,
        )
    )
    assert result.bucket == DealBucket.LIKELY_WITHDRAWN
