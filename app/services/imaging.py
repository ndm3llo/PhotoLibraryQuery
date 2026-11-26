"""Decode, orient, resize and fingerprint images. Pure functions; no I/O.

Originals are never modified (they are stored byte-for-byte). Derived copies are WebP.
"""
import hashlib
import io
import struct
import warnings
from dataclasses import dataclass
from datetime import datetime

import imagehash
from PIL import Image, ImageOps
from pillow_heif import register_heif_opener

from app.services.extract_metadata import EXIF_IFD, TAG_DATETIME_ORIGINAL, TAG_OFFSET_ORIGINAL

register_heif_opener()

# Reject decompression bombs instead of merely warning: a small file that decodes to a gigantic bitmap.
MAX_PIXELS = 120_000_000
Image.MAX_IMAGE_PIXELS = MAX_PIXELS

DISPLAY_LONG_EDGE = 2048
THUMB_LONG_EDGE = 400
DISPLAY_QUALITY = 85
THUMB_QUALITY = 80
SAVE_QUALITY = 95


class InvalidImage(ValueError):
    """The bytes are not a usable image (corrupt, unsupported, or too large to decode safely)."""


@dataclass(frozen=True)
class Derivatives:
    display: bytes
    thumb: bytes
    width: int  # of the oriented original
    height: int


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def open_image(data: bytes) -> Image.Image:
    """Open from bytes, trusting the CONTENT not the filename or declared type."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            img = Image.open(io.BytesIO(data))
            img.load()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as e:
        raise InvalidImage(f"image too large to decode safely: {e}") from e
    except Exception as e:
        raise InvalidImage(f"not a readable image: {type(e).__name__}") from e
    return img


def _to_rgb(img: Image.Image) -> Image.Image:
    if img.mode in ("RGB",):
        return img
    if img.mode in ("RGBA", "LA", "P", "PA"):
        rgba = img.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return img.convert("RGB")  # CMYK, I;16, L, ...


def _resized_webp(img: Image.Image, long_edge: int, quality: int, icc: bytes | None) -> bytes:
    w, h = img.size
    scale = min(1.0, long_edge / max(w, h))  # never upscale
    out = img if scale == 1.0 else img.resize((max(1, round(w * scale)), max(1, round(h * scale))),
                                              Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    kwargs = {"format": "WEBP", "quality": quality, "method": 4}
    if icc:
        kwargs["icc_profile"] = icc
    out.save(buf, **kwargs)
    return buf.getvalue()


def make_derivatives(img: Image.Image) -> Derivatives:
    """Apply EXIF orientation (so faces are upright for detection), then build display + thumbnail."""
    icc = img.info.get("icc_profile")
    oriented = ImageOps.exif_transpose(img)  # also drops the now-wrong orientation tag
    rgb = _to_rgb(oriented)
    return Derivatives(
        display=_resized_webp(rgb, DISPLAY_LONG_EDGE, DISPLAY_QUALITY, icc),
        thumb=_resized_webp(rgb, THUMB_LONG_EDGE, THUMB_QUALITY, icc),
        width=rgb.width,
        height=rgb.height,
    )


def exif_block(orientation: int = 1, taken_local: datetime | None = None, utc_offset: str | None = None) -> bytes:
    """A tiny EXIF block (TIFF structure) holding at most which way up the picture goes and when it was taken.

    Written by hand so that it can hold nothing else. With neither, it is a valid empty block.
    """
    texts = []
    if taken_local is not None:
        texts.append((TAG_DATETIME_ORIGINAL, taken_local.strftime("%Y:%m:%d %H:%M:%S").encode() + b"\0"))
        if utc_offset:
            texts.append((TAG_OFFSET_ORIGINAL, utc_offset.encode("ascii") + b"\0"))
    first = [(274, 3, orientation)] if orientation != 1 else []
    first_size = 2 + 12 * (len(first) + bool(texts)) + 4
    dates_at = 8 + first_size
    if texts:
        first.append((EXIF_IFD, 4, dates_at))
    out = b"II*\0" + struct.pack("<IH", 8, len(first))
    out += b"".join(struct.pack("<HHII", tag, kind, 1, value) for tag, kind, value in first) + bytes(4)
    if texts:
        data_at = dates_at + 2 + 12 * len(texts) + 4
        entries, data = b"", b""
        for tag, value in texts:  # every value here is longer than 4 bytes, so it lives after the table
            entries += struct.pack("<HHII", tag, 2, len(value), data_at + len(data))
            data += value
        out += struct.pack("<H", len(texts)) + entries + bytes(4) + data
    return out


def make_save_copy(img: Image.Image, taken_local: datetime | None = None, utc_offset: str | None = None) -> bytes:
    """A photo rebuilt from its pixels: full size, upright, JPEG, with the date taken and NOTHING else of the
    original's hidden details.

    Used when the original file cannot be cleaned in place (app/services/clean_copy.py). Built up from nothing: a new
    picture receives only the pixels, the color profile and the date, so nothing unexpected can come along.
    """
    # A color profile describes one color model; after converting from another (such as CMYK) it would be wrong.
    icc = img.info.get("icc_profile") if img.mode in ("RGB", "RGBA", "P") else None
    rgb = _to_rgb(ImageOps.exif_transpose(img))
    clean = Image.new("RGB", rgb.size)
    clean.paste(rgb)
    options: dict = {"format": "JPEG", "quality": SAVE_QUALITY, "subsampling": 0}
    if icc:
        options["icc_profile"] = icc
    if taken_local is not None:
        options["exif"] = b"Exif\0\0" + exif_block(1, taken_local, utc_offset)
    buf = io.BytesIO()
    clean.save(buf, **options)
    return buf.getvalue()


def compute_phash(img: Image.Image) -> int:
    """64-bit perceptual hash as a SIGNED int (fits Postgres bigint)."""
    value = int(str(imagehash.phash(_to_rgb(ImageOps.exif_transpose(img)))), 16)
    return value - (1 << 64) if value >= (1 << 63) else value
