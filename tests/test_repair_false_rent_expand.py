from app.domain.repair_false_rent_expand import restored_monthly_rent


def test_restores_m2bomber_hryvnia_total_times_area():
    restored = restored_monthly_rent(
        price=81000,
        currency="UAH",
        area_sqm=27,
        deal_type="rent",
        source="m2bomber",
        title="Оренда офісу 27м2 ціна 3000грн",
        description="Київ 3 000 ₴ 27 м² Офіс біля метро.",
    )
    assert restored == (3000, "UAH")


def test_restores_olx_title_price():
    restored = restored_monthly_rent(
        price=81000,
        currency="UAH",
        area_sqm=27,
        deal_type="rent",
        source="olx",
        title="Оренда офісу 27м2 ціна 3000грн",
        description="Офіс біля метро Лівобережна.",
    )
    assert restored == (3000, "UAH")


def test_restores_price_written_in_the_ad_under_two_dollars():
    restored = restored_monthly_rent(
        price=135000,
        currency="UAH",
        area_sqm=45,
        deal_type="rent",
        source="m2bomber",
        title="Оренда офісу 45 м²",
        description="3 000 ₴ 45 м² офіс",
    )
    assert restored == (3000, "UAH")


def test_keeps_tiny_hryvnia_that_is_a_rate():
    restored = restored_monthly_rent(
        price=120000,
        currency="UAH",
        area_sqm=240,
        deal_type="rent",
        source="m2bomber",
        title="Оренда офісу 240 м²",
        description="500 ₴ 240 м² офіс",
    )
    assert restored is None


def test_keeps_explicit_hryvnia_per_sqm():
    restored = restored_monthly_rent(
        price=78000,
        currency="UAH",
        area_sqm=78,
        deal_type="rent",
        source="olx",
        title="Оренда магазину",
        description="Ціна – 1000 грн./кв.м. + комунальні.",
    )
    assert restored is None


def test_skips_hourly_fee():
    restored = restored_monthly_rent(
        price=36000,
        currency="UAH",
        area_sqm=30,
        deal_type="rent",
        source="olx",
        title="Оренда кабінету психолога",
        description="Оренда: погодинно. день 1200 грн",
    )
    assert restored is None
