from app.db.models import Listing
from app.domain.list_card import list_card_changed, needs_detail_fetch
from app.scrapers.base import RawListing


def _listing(**kwargs) -> Listing:
    base = dict(
        source="lun",
        external_id="1",
        title="Офис 100 м²",
        price=1000.0,
        currency="USD",
        area_sqm=100.0,
        source_status_raw=None,
        phone="+380501234567",
        description="desc",
        raw_extra={},
    )
    base.update(kwargs)
    return Listing(**base)


def _raw(**kwargs) -> RawListing:
    base = dict(
        source="lun",
        external_id="1",
        url="https://lun.ua/x",
        title="Офис 100 м²",
        deal_type="rent",
        price=1000.0,
        currency="USD",
        area_sqm=100.0,
    )
    base.update(kwargs)
    return RawListing(**base)


def test_list_card_unchanged():
    lst = _listing()
    raw = _raw()
    assert list_card_changed(lst, raw) is False
    assert needs_detail_fetch(lst, raw) is False


def test_list_card_price_change_needs_detail():
    lst = _listing(price=1000.0)
    raw = _raw(price=900.0)
    assert list_card_changed(lst, raw) is True
    assert needs_detail_fetch(lst, raw) is True


def test_new_listing_needs_detail():
    raw = _raw()
    assert needs_detail_fetch(None, raw) is True


def test_missing_phone_still_needs_detail():
    lst = _listing(phone=None, description="")
    raw = _raw()
    assert needs_detail_fetch(lst, raw) is True
