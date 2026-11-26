"""Capture-date ladder. Pure logic; no database."""
import io
from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image

from app.services import extract_metadata as em

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc)


class FakeExif:
    """Stands in for Pillow's Exif: only get_ifd() is used."""

    def __init__(self, sub=None, gps=None):
        self.ifds = {em.EXIF_IFD: sub or {}, em.GPS_IFD: gps or {}}

    def get_ifd(self, tag):
        return self.ifds.get(tag, {})


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


# ------------------------------------------------------------------------------ EXIF rungs

def test_exif_original_is_preferred_over_everything():
    exif = FakeExif({em.TAG_DATETIME_ORIGINAL: "2019:07:04 15:30:12", em.TAG_DATETIME_DIGITIZED: "2020:01:01 00:00:00"})
    t = em.resolve_taken_at(exif, "IMG_20180101_000000.jpg", utc(2025, 1, 1), now=NOW)
    assert (t.value, t.source) == (utc(2019, 7, 4, 15, 30, 12), "exif_original")
    assert t.tz is None


def test_offset_is_recorded_and_changes_the_instant():
    exif = FakeExif({em.TAG_DATETIME_ORIGINAL: "2019:07:04 15:30:12", em.TAG_OFFSET_ORIGINAL: "+05:30"})
    t = em.resolve_taken_at(exif, now=NOW)
    assert t.tz == "+05:30"
    assert t.value == datetime(2019, 7, 4, 15, 30, 12, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    assert t.value.astimezone(timezone.utc) == utc(2019, 7, 4, 10, 0, 12)


def test_digitized_used_when_original_missing_or_junk():
    for junk in (None, "", "0000:00:00 00:00:00", "garbage", "2019:13:45 25:61:61", b"\x00\x00"):
        exif = FakeExif({em.TAG_DATETIME_ORIGINAL: junk, em.TAG_DATETIME_DIGITIZED: "2018:05:06 07:08:09"})
        t = em.resolve_taken_at(exif, now=NOW)
        assert (t.value, t.source) == (utc(2018, 5, 6, 7, 8, 9), "exif_digitized"), junk


def test_bytes_values_and_trailing_nul_are_accepted():
    exif = FakeExif({em.TAG_DATETIME_ORIGINAL: b"2019:07:04 15:30:12\x00"})
    assert em.resolve_taken_at(exif, now=NOW).value == utc(2019, 7, 4, 15, 30, 12)


@pytest.mark.parametrize("raw", ["1970:01:01 00:00:00", "1904:01:01 00:00:00", "1989:12:31 23:59:59", "2031:01:01 00:00:00"])
def test_implausible_exif_dates_are_skipped(raw):
    exif = FakeExif({em.TAG_DATETIME_ORIGINAL: raw})
    assert em.resolve_taken_at(exif, now=NOW) is None


def test_base_ifd_datetime_tag_306_is_never_used():
    """Tag 306 is the file-modified time. The prototype read it by mistake; it must not count."""
    class Base(FakeExif):
        def get(self, tag, default=None):
            return "2024:02:02 02:02:02" if tag == 306 else default

    assert em.resolve_taken_at(Base(), now=NOW) is None


# -------------------------------------------------------------------------- fallback rungs

@pytest.mark.parametrize("name,expected", [
    ("IMG_20190704_153012.jpg", utc(2019, 7, 4, 15, 30, 12)),
    ("PXL_20190704_153012345.jpg", utc(2019, 7, 4, 15, 30, 12)),
    ("2019-07-04 15.30.12.png", utc(2019, 7, 4, 15, 30, 12)),
    ("Screenshot 2019-07-04 at 15.30.12.png", utc(2019, 7, 4, 15, 30, 12)),
    ("20190704_153012.HEIC", utc(2019, 7, 4, 15, 30, 12)),
    ("IMG-20190704-WA0001.jpg", utc(2019, 7, 4)),
    ("2019-07-04.jpg", utc(2019, 7, 4)),
    ("photos/2019/IMG_20190704_153012.jpg", utc(2019, 7, 4, 15, 30, 12)),
])
def test_filename_patterns(name, expected):
    t = em.resolve_taken_at(None, name, now=NOW)
    assert (t.value, t.source) == (expected, "filename")


@pytest.mark.parametrize("name", [
    "IMG_8457.HEIC", "66636952694__332680D8-960A-42A1-9C56-1AB12EEC4075.heic",
    "DB5FEDD0-7C02-4B33-A886-CFB64F7E9B55.JPG", "FullSizeRender (2).heic", "IMG_20191345_000000.jpg",
    "photo.jpg", "", None, "IMG_19000101_000000.jpg", "x123456789012345.jpg",
])
def test_filenames_that_must_not_produce_a_date(name):
    assert em.resolve_taken_at(None, name, now=NOW) is None


def test_file_mtime_is_the_last_resort_and_low_confidence():
    t = em.resolve_taken_at(None, "IMG_8457.HEIC", utc(2023, 5, 5, 5, 5), now=NOW)
    assert (t.value, t.source) == (utc(2023, 5, 5, 5, 5), "file_mtime")
    assert t.confidence < 0.5


def test_naive_client_mtime_is_treated_as_utc_and_future_mtime_ignored():
    assert em.resolve_taken_at(None, None, datetime(2023, 5, 5), now=NOW).value == utc(2023, 5, 5)
    assert em.resolve_taken_at(None, None, NOW + timedelta(days=30), now=NOW) is None


def test_precedence_chain():
    exif = FakeExif({em.TAG_DATETIME_DIGITIZED: "2018:05:06 07:08:09"})
    assert em.resolve_taken_at(exif, "IMG_20190704_153012.jpg", utc(2025, 1, 1), now=NOW).source == "exif_digitized"
    assert em.resolve_taken_at(FakeExif(), "IMG_20190704_153012.jpg", utc(2025, 1, 1), now=NOW).source == "filename"
    assert em.resolve_taken_at(FakeExif(), "IMG_1.jpg", utc(2025, 1, 1), now=NOW).source == "file_mtime"
    assert em.resolve_taken_at(FakeExif(), "IMG_1.jpg", None, now=NOW) is None


# ---------------------------------------------------------- real files with embedded EXIF

def _jpeg_with_exif(date="2019:07:04 15:30:12", orientation=1, gps=None):
    img = Image.new("RGB", (64, 48), "blue")
    exif = Image.Exif()
    exif[274] = orientation
    exif.get_ifd(em.EXIF_IFD)[em.TAG_DATETIME_ORIGINAL] = date
    if gps:
        exif.get_ifd(em.GPS_IFD).update(gps)
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    return buf.getvalue()


def test_date_survives_a_real_jpeg_round_trip():
    img = Image.open(io.BytesIO(_jpeg_with_exif()))
    t = em.resolve_taken_at(em.read_exif(img), "x.jpg", now=NOW)
    assert (t.value, t.source) == (utc(2019, 7, 4, 15, 30, 12), "exif_original")


def test_gps_round_trip_and_bounds():
    gps = {1: "N", 2: (42.0, 30.0, 0.0), 3: "W", 4: (83.0, 15.0, 36.0)}
    img = Image.open(io.BytesIO(_jpeg_with_exif(gps=gps)))
    wkt = em.extract_gps_wkt(em.read_exif(img))
    assert wkt == "POINT(-83.26 42.5)"

    bad = {1: "N", 2: (95.0, 0.0, 0.0), 3: "E", 4: (10.0, 0.0, 0.0)}  # latitude out of range
    assert em.extract_gps_wkt(FakeExif(gps=bad)) is None
    null_island = {1: "N", 2: (0.0, 0.0, 0.0), 3: "E", 4: (0.0, 0.0, 0.0)}
    assert em.extract_gps_wkt(FakeExif(gps=null_island)) is None
    assert em.extract_gps_wkt(FakeExif()) is None
    assert em.extract_gps_wkt(None) is None


def test_each_date_uses_its_own_time_zone_tag():
    """Standard EXIF numbers: 36881 belongs to the original date, 36882 to the digitized one."""
    original = FakeExif({36867: "2019:07:04 15:30:12", 36881: "+05:30", 36880: "-08:00"})
    assert em.resolve_taken_at(original).tz == "+05:30"
    digitized = FakeExif({36868: "2019:07:04 15:30:12", 36882: "+02:00", 36881: "-08:00"})
    assert em.resolve_taken_at(digitized).tz == "+02:00"
    only_general = FakeExif({36867: "2019:07:04 15:30:12", 36880: "-03:00"})
    assert em.resolve_taken_at(only_general).tz == "-03:00"
