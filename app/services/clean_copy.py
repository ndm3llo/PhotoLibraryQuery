"""The copy of a photo that people save: the original picture itself, with its hidden details removed.

"Lossless": the compressed picture is carried over byte for byte, in its own format. Only the parts of the file
that describe it (location, date, camera, serial numbers, captions, embedded extra pictures, motion clips) are
dropped or blanked. What stays: what is needed to show the picture correctly (orientation, color profile) and the
date it was taken, as the app knows it (so a corrected date is the one that travels).

Nothing is trusted. Every result is checked, and if anything about the file is unfamiliar or the check fails, the
photo is re-saved from its pixels alone instead (`imaging.make_save_copy`), which cannot carry anything over.
Pure functions; no I/O.
"""
import logging
import struct
import zlib
from dataclasses import dataclass
from datetime import datetime

from PIL import Image, ImageOps

from app.services.extract_metadata import (
    EXIF_IFD,
    GPS_IFD,
    TAG_DATETIME_ORIGINAL,
    TAG_OFFSET_ORIGINAL,
)
from app.services.imaging import exif_block, make_save_copy, open_image

logger = logging.getLogger(__name__)

ORIENTATION = 274
EXTENSIONS = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/heic": "heic"}


class Unfamiliar(ValueError):
    """The file has something this module does not understand well enough to clean it."""


@dataclass(frozen=True)
class Keep:
    """The only details the cleaned file may carry."""
    orientation: int = 1
    taken_local: datetime | None = None
    utc_offset: str | None = None
    size: tuple[int, int] = (0, 0)  # of the picture as stored

    @property
    def anything(self) -> bool:
        return self.orientation != 1 or self.taken_local is not None

    def block(self) -> bytes:
        return exif_block(self.orientation, self.taken_local, self.utc_offset)


# ------------------------------------------------------------------------------------ JPEG

def _jpeg(data: bytes, keep: Keep) -> bytes:
    if data[:2] != b"\xff\xd8":
        raise Unfamiliar("not a JPEG")
    kept, i = [], 2
    while True:
        if i + 2 > len(data) or data[i] != 0xFF:
            raise Unfamiliar("broken JPEG")
        marker = data[i + 1]
        if marker == 0xFF:  # padding
            i += 1
            continue
        if marker == 0xD9:  # end of the main picture; anything after it (extra pictures, motion clips) is left behind
            kept.append(b"\xff\xd9")
            break
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            kept.append(data[i:i + 2])
            i += 2
            continue
        if i + 4 > len(data):
            raise Unfamiliar("broken JPEG")
        end = i + 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
        if end > len(data) or end < i + 4:
            raise Unfamiliar("broken JPEG")
        segment, body = data[i:end], data[i + 4:end]
        i = end
        if marker == 0xE0:
            if body.startswith(b"JFIF\x00") and len(body) >= 14:  # keep the header, never its little preview picture
                kept.append(b"\xff\xe0\x00\x10" + body[:12] + b"\x00\x00")
        elif marker == 0xE2:
            if body.startswith(b"ICC_PROFILE\x00"):
                kept.append(segment)
        elif marker == 0xEE:
            if body.startswith(b"Adobe"):  # says how the colors are stored
                kept.append(segment)
        elif 0xE0 <= marker <= 0xEF or marker == 0xFE:
            continue  # every other application block and comments: EXIF, XMP, captions, maker data
        else:
            kept.append(segment)
        if marker == 0xDA:  # picture data follows, up to the next real marker
            j = i
            while True:
                j = data.find(b"\xff", j)
                if j < 0 or j + 1 >= len(data):
                    raise Unfamiliar("JPEG ends early")
                following = data[j + 1]
                if following == 0x00 or 0xD0 <= following <= 0xD7:
                    j += 2
                elif following == 0xFF:
                    j += 1
                else:
                    break
            kept.append(data[i:j])
            i = j
    if keep.anything:
        exif = b"Exif\x00\x00" + keep.block()
        at = 1 if kept and kept[0].startswith(b"\xff\xe0") else 0
        kept.insert(at, b"\xff\xe1" + struct.pack(">H", len(exif) + 2) + exif)
    return b"\xff\xd8" + b"".join(kept)


# ------------------------------------------------------------------------------------- PNG

_PNG_KEEP = {b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS", b"cHRM", b"gAMA", b"iCCP", b"sBIT", b"sRGB", b"bKGD",
             b"pHYs", b"cICP", b"acTL", b"fcTL", b"fdAT"}


def _png_chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def _png(data: bytes, keep: Keep) -> bytes:
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise Unfamiliar("not a PNG")
    out, i = [data[:8]], 8
    while i + 12 <= len(data):
        size = struct.unpack(">I", data[i:i + 4])[0]
        kind, end = data[i + 4:i + 8], i + 12 + size
        if end > len(data):
            break
        if kind in _PNG_KEEP:
            out.append(data[i:end])
        if kind == b"IHDR" and keep.anything:
            out.append(_png_chunk(b"eXIf", keep.block()))
        if kind == b"IEND":
            return b"".join(out)
        i = end
    raise Unfamiliar("PNG ends early")


# ------------------------------------------------------------------------------------ WebP

_WEBP_KEEP = {b"VP8 ", b"VP8L", b"VP8X", b"ALPH", b"ICCP", b"ANIM", b"ANMF"}


def _webp(data: bytes, keep: Keep) -> bytes:
    if data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        raise Unfamiliar("not a WebP")
    chunks, i, limit = [], 12, min(len(data), 8 + struct.unpack("<I", data[4:8])[0])
    while i + 8 <= limit:
        kind, size = data[i:i + 4], struct.unpack("<I", data[i + 4:i + 8])[0]
        if i + 8 + size > limit:
            raise Unfamiliar("WebP ends early")
        if kind in _WEBP_KEEP:
            chunks.append([kind, data[i + 8:i + 8 + size]])
        i += 8 + size + (size & 1)
    header = next((c for c in chunks if c[0] == b"VP8X"), None)
    if header is None and keep.anything:  # a plain WebP needs the extended header before it can carry anything
        picture = next((b for k, b in chunks if k == b"VP8L"), None)
        see_through = 0x10 if picture is not None and len(picture) > 4 and picture[4] & 0x10 else 0
        width, height = keep.size
        header = [b"VP8X", bytes([see_through, 0, 0, 0]) + (width - 1).to_bytes(3, "little")
                  + (height - 1).to_bytes(3, "little")]
        chunks.insert(0, header)
    if header is not None:
        flags = header[1][0] & ~0x0C  # no EXIF, no XMP
        if keep.anything:
            flags |= 0x08
            chunks.append([b"EXIF", keep.block()])
        header[1] = bytes([flags]) + header[1][1:]
    body = b"".join(k + struct.pack("<I", len(b)) + b + (b"\x00" if len(b) & 1 else b"") for k, b in chunks)
    return b"RIFF" + struct.pack("<I", 4 + len(body)) + b"WEBP" + body


# ------------------------------------------------------------------------------------ HEIC
# A HEIC file is a tree of boxes. Details live as separate "items" whose bytes are found through an index. Moving
# anything would break that index, so the bytes are blanked where they are and the file keeps its size.

def _boxes(data: bytes, start: int, end: int):
    """Yields (type, box start, content start, box end)."""
    i = start
    while i < end:
        if i + 8 > end:
            raise Unfamiliar("broken box")
        size, kind, head = struct.unpack(">I", data[i:i + 4])[0], data[i + 4:i + 8], 8
        if size == 1:
            size, head = struct.unpack(">Q", data[i + 8:i + 16])[0], 16
        elif size == 0:
            size = end - i
        if size < head or i + size > end:
            raise Unfamiliar("broken box")
        yield kind, i, i + head, i + size
        i += size


def _number(data: bytes, at: int, width: int) -> int:
    return int.from_bytes(data[at:at + width], "big")


def _detail_items(data: bytes, start: int, end: int) -> dict:
    """Item id -> type, for the items that describe the picture rather than being it."""
    version = data[start]
    first = start + 4 + (2 if version == 0 else 4)
    found = {}
    for kind, _, content, _ in _boxes(data, first, end):
        if kind != b"infe" or data[content] < 2:
            raise Unfamiliar("old item list")
        width = 2 if data[content] == 2 else 4
        item = _number(data, content + 4, width)
        item_type = data[content + 4 + width + 2:content + 4 + width + 6]
        if item_type in (b"Exif", b"mime", b"uri "):
            found[item] = item_type
    return found


def _item_places(data: bytes, start: int, idat: int | None) -> dict:
    """Item id -> [(position, length)] in the file."""
    version = data[start]
    at = start + 4
    offset_width, length_width = data[at] >> 4, data[at] & 15
    base_width, index_width = data[at + 1] >> 4, (data[at + 1] & 15 if version in (1, 2) else 0)
    at += 2
    count_width = 2 if version < 2 else 4
    count = _number(data, at, count_width)
    at += count_width
    places = {}
    for _ in range(count):
        item = _number(data, at, count_width)
        at += count_width
        method = 0
        if version in (1, 2):
            method = _number(data, at, 2) & 15
            at += 2
        at += 2  # data reference
        base = _number(data, at, base_width)
        at += base_width
        extents = _number(data, at, 2)
        at += 2
        spans = []
        for _ in range(extents):
            at += index_width
            offset = _number(data, at, offset_width)
            at += offset_width
            length = _number(data, at, length_width)
            at += length_width
            if method == 0:
                spans.append((base + offset, length))
            elif method == 1 and idat is not None:
                spans.append((idat + base + offset, length))
            else:
                spans.append((None, length))
        places[item] = spans
    return places


def _heic(data: bytes, keep: Keep) -> bytes:
    out = bytearray(data)

    def wipe(box_start: int, content: int, box_end: int) -> None:
        out[box_start + 4:box_start + 8] = b"free"
        out[content:box_end] = bytes(box_end - content)

    top = list(_boxes(data, 0, len(data)))
    if not top or top[0][0] != b"ftyp":
        raise Unfamiliar("not a HEIC")
    meta = None
    for kind, box_start, content, box_end in top:
        if kind == b"meta":
            meta = (content + 4, box_end)  # the box starts with version and flags
        elif kind in (b"uuid", b"udta"):
            wipe(box_start, content, box_end)
        elif kind not in (b"ftyp", b"mdat", b"free", b"skip", b"wide"):
            raise Unfamiliar(f"unexpected part {kind!r}")  # for example the video track of a moving photo
    if meta is None:
        raise Unfamiliar("no index")
    inside = {}
    for kind, box_start, content, box_end in _boxes(data, *meta):
        if kind in (b"uuid", b"udta"):
            wipe(box_start, content, box_end)
        inside[kind] = (content, box_end)
    if b"iinf" not in inside or b"iloc" not in inside:
        raise Unfamiliar("no item index")
    details = _detail_items(data, *inside[b"iinf"])
    places = _item_places(data, inside[b"iloc"][0], inside[b"idat"][0] if b"idat" in inside else None)
    dated = keep.taken_local is None  # nothing to write, or not yet written
    # A HEIC keeps its orientation outside these items, so only the date goes back in.
    kept = bytes(4) + exif_block(1, keep.taken_local, keep.utc_offset)
    for item, item_type in details.items():
        for position, (start, length) in enumerate(places.get(item, [])):
            if start is None or not length or start + length > len(data):
                raise Unfamiliar("an item that cannot be located")
            blank = (b" " if item_type == b"mime" else b"\x00") * length
            if item_type == b"Exif" and position == 0:  # leave a valid EXIF block behind, holding only the date
                if not dated and len(kept) <= length:
                    blank, dated = kept + bytes(length - len(kept)), True
                else:
                    blank = (bytes(4) + exif_block() + bytes(length))[:length]
            out[start:start + length] = blank
    if not dated:
        raise Unfamiliar("no room for the date")
    return bytes(out)


# ------------------------------------------------------------------------- clean and check

_FORMATS = {"JPEG": (_jpeg, "image/jpeg"), "MPO": (_jpeg, "image/jpeg"), "PNG": (_png, "image/png"),
            "WEBP": (_webp, "image/webp"), "HEIF": (_heic, "image/heic")}
_ADDRESSES = (b"ns.adobe.com/xap", b"<x:xmpmeta", b"<?xpacket")


def _written_values(exif) -> set[bytes]:
    """Pieces of text and the location numbers from the original, to look for in the cleaned file."""
    found = set()
    gps = exif.get_ifd(GPS_IFD) if exif else {}
    for block in (dict(exif or {}), dict(exif.get_ifd(EXIF_IFD)) if exif else {}, dict(gps)):
        for value in block.values():
            if isinstance(value, str) and len(value.strip("\x00 ")) >= 6:
                found.add(value.strip("\x00 ").encode())
    for tag in (2, 4):  # latitude and longitude: three fractions each, as they are stored
        if tag in gps:
            for order in (">", "<"):
                try:
                    found.add(b"".join(struct.pack(order + "II", v.numerator, v.denominator) for v in gps[tag]))
                except (AttributeError, struct.error):
                    pass
    return found


def _check(original: Image.Image, cleaned: bytes, keep: Keep) -> None:
    result = open_image(cleaned)
    exif = result.getexif()
    wanted = {}
    if keep.taken_local is not None:
        wanted[TAG_DATETIME_ORIGINAL] = keep.taken_local.strftime("%Y:%m:%d %H:%M:%S")
        if keep.utc_offset:
            wanted[TAG_OFFSET_ORIGINAL] = keep.utc_offset
    if exif.get_ifd(GPS_IFD) or set(exif) - {ORIENTATION, EXIF_IFD}:
        raise Unfamiliar("details are still there")
    if dict(exif.get_ifd(EXIF_IFD)) != wanted:
        raise Unfamiliar("the date is wrong, or something came with it")
    for needle in _ADDRESSES:
        if needle in cleaned:
            raise Unfamiliar("a description block is still there")
    allowed = {value.encode() for value in wanted.values()}
    for value in _written_values(original.getexif()) - allowed:
        if value in cleaned:
            raise Unfamiliar("a value from the original is still there")
    before, after = ImageOps.exif_transpose(original), ImageOps.exif_transpose(result)
    if before.size != after.size or before.mode != after.mode or before.tobytes() != after.tobytes():
        raise Unfamiliar("the picture changed")


def make_clean_copy(data: bytes, taken_local: datetime | None = None,
                    utc_offset: str | None = None) -> tuple[bytes, str]:
    """(bytes, content type) of the copy to hand out. `taken_local` is the date taken as the app knows it (None if
    unknown). Raises InvalidImage if the original cannot be read."""
    original = open_image(data)
    handler = _FORMATS.get(original.format)
    if handler is not None:
        clean, content_type = handler
        try:
            keep = Keep(int(original.getexif().get(ORIENTATION, 1) or 1), taken_local, utc_offset, original.size)
            cleaned = clean(data, keep)
            _check(original, cleaned, keep)
            return cleaned, content_type
        except Exception as e:  # anything unexpected: fall back to the copy that is safe by construction
            logger.warning("re-saving a %s photo from its pixels: %s", original.format, e)
    return make_save_copy(open_image(data), taken_local, utc_offset), "image/jpeg"
