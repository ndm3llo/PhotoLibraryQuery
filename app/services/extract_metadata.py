import logging
from io import BytesIO

from PIL import Image
from PIL.ExifTags import GPSTAGS
from pillow_heif import register_heif_opener

register_heif_opener()

logger = logging.getLogger(__name__)


def get_decimal_from_dms(dms, ref):
    # dms is usually a tuple like (degrees, minutes, seconds)
    degrees = float(dms[0])
    minutes = float(dms[1]) / 60.0
    seconds = float(dms[2]) / 3600.0

    decimal = degrees + minutes + seconds
    if ref in ["S", "W"]:
        decimal = -decimal
    return decimal


def get_photo_metadata(image_bytes):
    img = Image.open(BytesIO(image_bytes))

    exif_data = img.getexif()
    output = {"date": None, "location_wkt": None}
    if not exif_data:
        return output

    gps_info_raw = exif_data.get_ifd(0x8825)

    # Tag 306 is DateTime, 36867 is DateTimeOriginal
    # NOTE: DateTimeOriginal actually lives in the Exif sub-IFD, so this usually
    # resolves to tag 306 (file-modified time). Fixed properly in Phase 3.
    raw_date = exif_data.get(306) or exif_data.get(36867) or gps_info_raw.get(29)

    if raw_date:
        try:
            output["date"] = raw_date.replace(":", "-", 2)
        except Exception as e:
            logger.warning("Date formatting error: %s", e)

    if gps_info_raw:
        # Normalize the GPS dictionary (maps ID 2 to 'GPSLatitude', etc.)
        gps_data = {GPSTAGS.get(k, k): v for k, v in gps_info_raw.items()}

        required = ["GPSLatitude", "GPSLatitudeRef", "GPSLongitude", "GPSLongitudeRef"]
        if all(k in gps_data for k in required):
            lat = get_decimal_from_dms(gps_data["GPSLatitude"], gps_data["GPSLatitudeRef"])
            lon = get_decimal_from_dms(gps_data["GPSLongitude"], gps_data["GPSLongitudeRef"])

            # WKT string for PostGIS
            output["location_wkt"] = f"POINT({lon} {lat})"

    return output
