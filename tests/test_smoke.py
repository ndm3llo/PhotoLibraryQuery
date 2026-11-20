from app.services.extract_metadata import get_decimal_from_dms


def test_dms_to_decimal_north_east():
    assert round(get_decimal_from_dms((42, 30, 0), "N"), 4) == 42.5


def test_dms_to_decimal_south_west_is_negative():
    assert round(get_decimal_from_dms((10, 0, 36), "W"), 4) == -10.01
