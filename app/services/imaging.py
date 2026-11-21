"""Decode, orient, resize and fingerprint images. Pure functions; no I/O.

Originals are never modified (they are stored byte-for-byte). Derived copies are WebP.
"""
import hashlib
import io
import warnings
from dataclasses import dataclass

import imagehash
from PIL import Image, ImageOps
from pillow_heif import register_heif_opener

register_heif_opener()

# Reject decompression bombs instead of merely warning: a small file that decodes to a gigantic bitmap.
MAX_PIXELS = 120_000_000
Image.MAX_IMAGE_PIXELS = MAX_PIXELS

DISPLAY_LONG_EDGE = 2048
THUMB_LONG_EDGE = 400
DISPLAY_QUALITY = 85
THUMB_QUALITY = 80


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


def compute_phash(img: Image.Image) -> int:
    """64-bit perceptual hash as a SIGNED int (fits Postgres bigint)."""
    value = int(str(imagehash.phash(_to_rgb(ImageOps.exif_transpose(img)))), 16)
    return value - (1 << 64) if value >= (1 << 63) else value
