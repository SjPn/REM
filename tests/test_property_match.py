from __future__ import annotations

from app.domain.fingerprint import FingerprintInput
from app.domain.property_match import is_weak_location, soft_identity_key
from app.domain.signals import classify_seller


def test_is_weak_location():
    assert is_weak_location("Печерський", 100) is True
    assert is_weak_location("вул. Хрещатик, 1", None) is True
    assert is_weak_location("вул. Хрещатик, 1", 120) is False


def test_soft_identity_key_stable():
    a = soft_identity_key(
        address="вул. Богдана Хмельницького, 16",
        area_sqm=120.4,
        floor=4,
        deal_type="sale",
    )
    b = soft_identity_key(
        address="вул Богдана Хмельницького 16",
        area_sqm=119.0,
        floor=4,
        deal_type="sale",
    )
    assert a is not None and b is not None
    assert a[0] == b[0]
    assert a[2] == b[2]
    # areas round to nearby bands; soft match uses ±2 later
    assert abs((a[1] or 0) - (b[1] or 0)) <= 2


def test_classify_owner_by_unique_phone_and_text():
    assert (
        classify_seller(
            agency=None,
            phone="380501112233",
            title="Офіс від власника",
            phone_listing_count=1,
        )
        == "owner"
    )
    assert (
        classify_seller(
            agency="АН Київ",
            phone="380501112233",
            title="Офіс",
            phone_listing_count=1,
        )
        == "agency"
    )
    assert (
        classify_seller(
            agency=None,
            phone="380501112233",
            title="Офіс",
            phone_listing_count=5,
        )
        == "agency"
    )
