"""Capture time and GPS from an image, with provenance.

Date resolution ladder (first usable rung wins; the rung is recorded in `taken_at_source`):

  1. exif_original    EXIF DateTimeOriginal (+ OffsetTimeOriginal when present)
  2. exif_digitized   EXIF DateTimeDigitized
  3. filename         e.g. IMG_20190704_153012.jpg, 2019-07-04 15.30.12.png, PXL_20190704_..., IMG-20190704-WA0001
  4. file_mtime       the file's last-modified time as reported by the uploading browser (weak)

The EXIF base-IFD `DateTime` (tag 306) is deliberately NOT used: it is the file-modified time, not the
moment the photo was taken. (The prototype read tag 306 by accident because DateTimeOriginal lives in the
Exif sub-IFD, not the base IFD.)

Naive EXIF times (no UTC offset) are stored as if they were UTC with `taken_tz` left NULL: the wall-clock
reading is what matters for "summer 2019", and no timezone is invented.
"""
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from PIL import Image
from PIL.ExifTags import GPSTAGS
from pillow_heif import register_heif_opener

register_heif_opener()

EXIF_IFD = 0x8769
GPS_IFD = 0x8825
TAG_DATETIME_ORIGINAL = 36867
TAG_DATETIME_DIGITIZED = 36868
TAG_OFFSET_ORIGINAL = 36881
TAG_OFFSET_DIGITIZED = 36882
# "OffsetTime", which belongs to the file's modification date. Used only when the date's own offset is missing
# (earlier versions read it for the original date, and some cameras write only this one).
TAG_OFFSET_GENERAL = 36880

MIN_YEAR = 1990  # earlier than any digital photo a family will import; catches 1970/1904 epoch junk


@dataclass(frozen=True)
class TakenAt:
    value: datetime  # timezone-aware
    source: str
    confidence: float
    tz: str | None = None  # "+05:30" when the camera recorded an offset


def get_decimal_from_dms(dms, ref):
    # dms is usually a tuple like (degrees, minutes, seconds)
    degrees = float(dms[0])
    minutes = float(dms[1]) / 60.0
    seconds = float(dms[2]) / 3600.0

    decimal = degrees + minutes + seconds
    if ref in ["S", "W"]:
        decimal = -decimal
    return decimal


def _plausible(dt: datetime, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    return MIN_YEAR <= dt.year and dt <= now + timedelta(days=1)


def _parse_exif_datetime(raw, offset_raw=None) -> tuple[datetime, str | None] | None:
    """'2019:07:04 15:30:12' (+ '+05:30') -> (aware datetime, offset string). None if unusable."""
    if isinstance(raw, bytes):
        raw = raw.decode("ascii", "ignore")
    if not isinstance(raw, str):
        return None
    raw = raw.strip().strip("\x00")
    m = re.fullmatch(r"(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})", raw)
    if not m:
        return None
    try:
        naive = datetime(*[int(g) for g in m.groups()])
    except ValueError:  # e.g. the common "0000:00:00 00:00:00"
        return None

    tz, tzinfo = None, timezone.utc
    if isinstance(offset_raw, bytes):
        offset_raw = offset_raw.decode("ascii", "ignore")
    if isinstance(offset_raw, str):
        om = re.fullmatch(r"([+-])(\d{2}):(\d{2})", offset_raw.strip().strip("\x00"))
        if om:
            sign = 1 if om.group(1) == "+" else -1
            delta = timedelta(hours=int(om.group(2)), minutes=int(om.group(3)))
            if delta <= timedelta(hours=14):
                tzinfo, tz = timezone(sign * delta), offset_raw.strip().strip("\x00")
    return naive.replace(tzinfo=tzinfo), tz


# Filename patterns, most specific first. Digits must not touch other digits, so a long number such as
# a phone-number-like name never produces a fake date.
_FILENAME_PATTERNS = [
    re.compile(r"(?<!\d)((?:19|20)\d{2})[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])"
               r"[-_ T.at]*([01]\d|2[0-3])[.:_-]?([0-5]\d)[.:_-]?([0-5]\d)(?:\d{1,3})?(?!\d)"),  # optional ms (Pixel)
    re.compile(r"(?<!\d)((?:19|20)\d{2})[-_.]?(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])(?!\d)"),
]


def parse_filename_date(filename: str | None) -> datetime | None:
    if not filename:
        return None
    stem = filename.rsplit("/", 1)[-1]
    for pattern in _FILENAME_PATTERNS:
        for m in pattern.finditer(stem):
            parts = [int(g) for g in m.groups()]
            try:
                return datetime(*parts, tzinfo=timezone.utc)
            except ValueError:
                continue
    return None


def resolve_taken_at(exif, filename: str | None = None, client_last_modified: datetime | None = None,
                     now: datetime | None = None) -> TakenAt | None:
    """Apply the ladder. `exif` is a Pillow Exif mapping (or None)."""
    if exif:
        try:
            sub = exif.get_ifd(EXIF_IFD)
        except Exception:
            sub = {}
        for tag, offset_tag, source, confidence in (
            (TAG_DATETIME_ORIGINAL, TAG_OFFSET_ORIGINAL, "exif_original", 0.95),
            (TAG_DATETIME_DIGITIZED, TAG_OFFSET_DIGITIZED, "exif_digitized", 0.85),
        ):
            parsed = _parse_exif_datetime(sub.get(tag), sub.get(offset_tag) or sub.get(TAG_OFFSET_GENERAL))
            if parsed and _plausible(parsed[0], now):
                return TakenAt(parsed[0], source, confidence if parsed[1] else confidence - 0.05, parsed[1])

    from_name = parse_filename_date(filename)
    if from_name and _plausible(from_name, now):
        return TakenAt(from_name, "filename", 0.5)

    if client_last_modified is not None:
        lm = client_last_modified if client_last_modified.tzinfo else client_last_modified.replace(tzinfo=timezone.utc)
        if _plausible(lm, now):
            return TakenAt(lm, "file_mtime", 0.3)
    return None


def extract_gps_wkt(exif) -> str | None:
    """'POINT(lon lat)' for PostGIS, or None."""
    if not exif:
        return None
    try:
        gps_raw = exif.get_ifd(GPS_IFD)
    except Exception:
        return None
    if not gps_raw:
        return None
    gps = {GPSTAGS.get(k, k): v for k, v in gps_raw.items()}
    if not all(k in gps for k in ("GPSLatitude", "GPSLatitudeRef", "GPSLongitude", "GPSLongitudeRef")):
        return None
    try:
        lat = get_decimal_from_dms(gps["GPSLatitude"], gps["GPSLatitudeRef"])
        lon = get_decimal_from_dms(gps["GPSLongitude"], gps["GPSLongitudeRef"])
    except (TypeError, ValueError, ZeroDivisionError, IndexError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None
    return f"POINT({lon} {lat})"


def read_exif(img: Image.Image):
    try:
        return img.getexif()
    except Exception:
        return None
