from app.domain.segments import classify_segment, is_relevant_listing


def test_exclude_warehouse():
    d = classify_segment(title="Оренда складу 600 м² логістика", property_type="warehouse")
    assert not d.relevant


def test_exclude_industrial():
    assert not is_relevant_listing(title="Виробничий цех / промисловий комплекс")


def test_keep_office():
    d = classify_segment(title="Офіс 120 м² Шевченківський", floor=4)
    assert d.relevant
    assert d.segment == "office"


def test_keep_showroom():
    d = classify_segment(title="Шоурум на Печерську 80 м²")
    assert d.relevant
    assert d.segment == "showroom"


def test_keep_bc():
    d = classify_segment(title="Оренда в БЦ Gulliver")
    assert d.relevant
    assert d.segment == "business_center"


def test_keep_first_floor_retail():
    d = classify_segment(
        title="Торгове приміщення вільного призначення",
        floor=1,
    )
    assert d.relevant
    assert d.segment in {"street_retail", "free_purpose", "retail"}


def test_keep_standalone_building():
    d = classify_segment(title="Окрема будівля під бізнес 450 м²")
    assert d.relevant
    assert d.segment == "building"
