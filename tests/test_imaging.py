"""Decoding, orientation, derivatives and hashing. Pure logic; no database."""
import io

import pytest
from PIL import Image

from app.services import imaging


def make_jpeg(size=(400, 200), orientation=None, color="red", mode="RGB"):
    img = Image.new(mode, size, color)
    kwargs = {}
    if orientation:
        exif = Image.Exif()
        exif[274] = orientation
        kwargs["exif"] = exif
    buf = io.BytesIO()
    img.save(buf, "JPEG", **kwargs)
    return buf.getvalue()


def webp_size(data):
    return Image.open(io.BytesIO(data)).size


@pytest.mark.parametrize("orientation,expected", [(1, (400, 200)), (6, (200, 400)), (8, (200, 400)), (3, (400, 200))])
def test_exif_orientation_is_applied(orientation, expected):
    d = imaging.make_derivatives(imaging.open_image(make_jpeg(orientation=orientation)))
    assert (d.width, d.height) == expected
    assert webp_size(d.display) == expected  # small image: not upscaled


def test_derivatives_are_webp_and_sized_by_long_edge():
    d = imaging.make_derivatives(imaging.open_image(make_jpeg(size=(4000, 3000))))
    assert Image.open(io.BytesIO(d.display)).format == "WEBP"
    assert webp_size(d.display) == (2048, 1536)
    assert webp_size(d.thumb) == (400, 300)
    assert (d.width, d.height) == (4000, 3000), "dimensions describe the original, not the copy"


def test_small_images_are_never_upscaled():
    d = imaging.make_derivatives(imaging.open_image(make_jpeg(size=(100, 80))))
    assert webp_size(d.display) == (100, 80) and webp_size(d.thumb) == (100, 80)


@pytest.mark.parametrize("mode", ["RGBA", "P", "L", "CMYK"])
def test_unusual_color_modes_are_converted(mode):
    img = Image.new(mode, (50, 50))
    buf = io.BytesIO()
    img.save(buf, "PNG" if mode in ("RGBA", "P", "L") else "TIFF")
    d = imaging.make_derivatives(imaging.open_image(buf.getvalue()))
    assert Image.open(io.BytesIO(d.display)).mode == "RGB"


def test_transparency_is_flattened_onto_white():
    img = Image.new("RGBA", (10, 10), (0, 0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    out = Image.open(io.BytesIO(imaging.make_derivatives(imaging.open_image(buf.getvalue())).display))
    assert out.getpixel((5, 5))[0] > 240


def test_icc_profile_is_preserved():
    from PIL import ImageCms
    icc = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
    img = Image.new("RGB", (30, 30), "green")
    buf = io.BytesIO()
    img.save(buf, "JPEG", icc_profile=icc)
    d = imaging.make_derivatives(imaging.open_image(buf.getvalue()))
    assert Image.open(io.BytesIO(d.display)).info.get("icc_profile")


@pytest.mark.parametrize("junk", [b"", b"not an image", b"\xff\xd8\xff garbage", b"%PDF-1.4 hello", b"GIF89a"])
def test_non_images_are_rejected_by_content(junk):
    with pytest.raises(imaging.InvalidImage):
        imaging.open_image(junk)


def test_truncated_image_is_rejected():
    with pytest.raises(imaging.InvalidImage):
        imaging.open_image(make_jpeg(size=(800, 600))[:200])


def test_decompression_bomb_is_rejected(monkeypatch):
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)
    with pytest.raises(imaging.InvalidImage, match="too large"):
        imaging.open_image(make_jpeg(size=(400, 200)))


def test_phash_is_stable_signed_and_orientation_invariant():
    a = imaging.open_image(make_jpeg(size=(400, 200), color="red"))
    h1, h2 = imaging.compute_phash(a), imaging.compute_phash(a)
    assert h1 == h2 and -(1 << 63) <= h1 < (1 << 63)
    rotated_with_tag = imaging.open_image(make_jpeg(size=(200, 400), orientation=6, color="red"))
    upright = imaging.open_image(make_jpeg(size=(400, 200), color="red"))
    assert imaging.compute_phash(rotated_with_tag) == imaging.compute_phash(upright)


def test_phash_differs_for_different_pictures():
    left = Image.new("RGB", (64, 64), "white")
    left.paste("black", (0, 0, 32, 64))
    right = Image.new("RGB", (64, 64), "white")
    right.paste("black", (32, 0, 64, 64))
    assert imaging.compute_phash(left) != imaging.compute_phash(right)


def test_sha256_is_hex_of_the_bytes():
    assert imaging.sha256_hex(b"abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_heic_round_trip_when_the_encoder_is_available():
    pillow_heif = pytest.importorskip("pillow_heif")
    img = Image.new("RGB", (320, 240), "orange")
    buf = io.BytesIO()
    try:
        pillow_heif.from_pillow(img).save(buf, format="HEIF", quality=80)
    except Exception as e:  # encoder not compiled into this wheel
        pytest.skip(f"no HEIC encoder: {e}")
    opened = imaging.open_image(buf.getvalue())
    assert opened.size == (320, 240)
    assert imaging.make_derivatives(opened).width == 320
