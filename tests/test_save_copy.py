"""Saving a photo: the original picture, in its own format, with none of its hidden details."""
import io
import uuid
from datetime import datetime

import pytest
from apiutil import User, build_ctx, join, make_library
from ingest_helpers import make_image_bytes
from PIL import Image, ImageCms, ImageOps
from pillow_heif import register_heif_opener
from sqlalchemy import text

register_heif_opener()

GPS_IFD, EXIF_IFD = 0x8825, 0x8769
GPS = {1: "N", 2: (42.0, 16.0, 49.0), 3: "W", 4: (83.0, 44.0, 52.0)}
SECRETS = (b"Secret Camera", b"SERIAL-12345", b"2019:07:04", b"ns.adobe.com/xap", b"x:xmpmeta", b"GPSLatitude",
           b"Grandma's house", b"MOTION-CLIP-LOCATION")
XMP = (b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
       b'<rdf:Description xmlns:exif="http://ns.adobe.com/exif/1.0/" exif:GPSLatitude="42.2808"/></rdf:RDF>'
       b'</x:xmpmeta>')
PROFILE = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
FORMATS = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp", "HEIF": "image/heic"}
TAKEN = datetime(2001, 2, 3, 4, 5, 6)  # the date the app knows, deliberately unlike the one in the file


def loaded_original(fmt, orientation=None, **extra):
    """A picture carrying a location every way we know, plus a date, a camera, a serial number and a caption."""
    img = Image.new("RGB", (400, 200), "red")
    img.paste("blue", (0, 0, 40, 200))  # a blue stripe on the left, to see which way up it ends
    for n in range(0, 400, 7):
        img.putpixel((n, n % 200), (n % 256, 10, 200))
    exif = Image.Exif()
    exif[271] = "Secret Camera"
    exif[270] = "Grandma's house"
    if orientation:
        exif[274] = orientation
    exif.get_ifd(EXIF_IFD)[36867] = "2019:07:04 15:30:12"
    exif.get_ifd(EXIF_IFD)[42033] = "SERIAL-12345"
    exif.get_ifd(GPS_IFD).update(GPS)
    buf = io.BytesIO()
    options = {"exif": exif, "icc_profile": PROFILE, **extra}
    if fmt in ("JPEG", "WEBP", "HEIF"):
        options["xmp"] = XMP
    if fmt == "JPEG":
        options["comment"] = b"Grandma's house"
    img.save(buf, fmt, **options)
    return buf.getvalue()


def opened(data):
    img = Image.open(io.BytesIO(data))
    img.load()
    return img


def assert_clean(data, date=None, offset=None):
    """No details at all, except (when given) exactly this date taken."""
    out = opened(data)
    exif = out.getexif()
    assert not exif.get_ifd(GPS_IFD), "no location"
    want = {} if date is None else {36867: date, **({36881: offset} if offset else {})}
    assert dict(exif.get_ifd(EXIF_IFD)) == want, "only the date taken; no serial number or anything else"
    assert set(exif) <= {274, EXIF_IFD}, f"only orientation and date may remain, found {sorted(exif)}"
    for secret in SECRETS:
        if not (date and secret.decode(errors="ignore") in date):
            assert secret not in data, secret
    return out


@pytest.mark.parametrize("fmt", list(FORMATS))
def test_the_date_taken_travels_and_it_is_the_one_the_app_knows(fmt):
    from app.services.clean_copy import make_clean_copy
    source = loaded_original(fmt)
    data, content_type = make_clean_copy(source, TAKEN, "-05:00")
    out = assert_clean(data, "2001:02:03 04:05:06", "-05:00")
    assert content_type == FORMATS[fmt] and out.tobytes() == opened(source).tobytes(), "still the original picture"
    assert_clean(make_clean_copy(source, datetime(2019, 7, 4, 15, 30, 12))[0], "2019:07:04 15:30:12")


@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "WEBP", "HEIF"])
def test_date_and_orientation_fit_together(fmt):
    from app.services.clean_copy import make_clean_copy
    source = loaded_original(fmt, orientation=6)
    data, content_type = make_clean_copy(source, TAKEN)
    assert content_type == FORMATS[fmt]
    assert_clean(data, "2001:02:03 04:05:06")
    want, got = ImageOps.exif_transpose(opened(source)), ImageOps.exif_transpose(opened(data))
    assert got.size == want.size and got.tobytes() == want.tobytes()


@pytest.mark.parametrize("options", [{"lossless": True}, {"lossless": False}])
def test_a_plain_webp_can_be_given_a_date(options):
    from app.services.clean_copy import make_clean_copy
    buf = io.BytesIO()
    Image.new("RGB", (301, 157), "green").save(buf, "WEBP", **options)
    assert b"VP8X" not in buf.getvalue()
    data, content_type = make_clean_copy(buf.getvalue(), TAKEN)
    assert content_type == "image/webp"
    assert assert_clean(data, "2001:02:03 04:05:06").size == (301, 157)


def test_a_heic_with_nowhere_to_put_the_date_is_rebuilt_with_it():
    from app.services.clean_copy import make_clean_copy
    buf = io.BytesIO()
    Image.new("RGB", (100, 80), "red").save(buf, "HEIF")  # no EXIF at all
    data, content_type = make_clean_copy(buf.getvalue(), TAKEN)
    assert content_type == "image/jpeg"
    assert_clean(data, "2001:02:03 04:05:06")
    assert make_clean_copy(buf.getvalue())[1] == "image/heic", "with no date to add it stays as it is"


@pytest.mark.parametrize("fmt", list(FORMATS))
def test_if_cleaning_in_place_fails_the_rebuilt_photo_keeps_the_date(fmt, monkeypatch):
    from app.services import clean_copy
    source = loaded_original(fmt)
    monkeypatch.setattr(clean_copy, "_FORMATS", {opened(source).format: (lambda d, k: d, FORMATS[fmt])})
    data, content_type = clean_copy.make_clean_copy(source, TAKEN, "+01:00")
    assert content_type == "image/jpeg"
    assert_clean(data, "2001:02:03 04:05:06", "+01:00")


@pytest.mark.parametrize("fmt", list(FORMATS))
def test_the_picture_is_the_original_one_and_the_details_are_gone(fmt):
    from app.services.clean_copy import make_clean_copy
    source = loaded_original(fmt)
    before = opened(source)
    assert before.getexif().get_ifd(GPS_IFD), "the test picture really has a location"
    data, content_type = make_clean_copy(source)
    out = assert_clean(data)
    assert content_type == FORMATS[fmt] and out.format == before.format, "same kind of file"
    assert out.tobytes() == before.tobytes(), "not one pixel changed"
    assert out.info.get("icc_profile") == before.info.get("icc_profile"), "colors stay right"


def test_jpeg_and_png_picture_data_is_copied_byte_for_byte():
    from app.services.clean_copy import make_clean_copy
    jpeg = loaded_original("JPEG")
    scan = jpeg[jpeg.index(b"\xff\xda"):]
    assert make_clean_copy(jpeg)[0].endswith(scan), "no re-compression"
    png = loaded_original("PNG")
    assert png[png.index(b"IDAT"):png.index(b"IEND") - 4] in make_clean_copy(png)[0]


def test_anything_hidden_after_a_jpeg_is_left_behind():
    """Phones append extra pictures and motion clips after the picture; those can carry the location too."""
    from app.services.clean_copy import make_clean_copy
    source = loaded_original("JPEG") + loaded_original("JPEG") + b"MOTION-CLIP-LOCATION +42.28-083.74/"
    data, content_type = make_clean_copy(source)
    assert content_type == "image/jpeg" and data.endswith(b"\xff\xd9") and len(data) < len(source) / 2
    assert_clean(data)


@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "WEBP", "HEIF"])
def test_a_sideways_photo_still_shows_the_right_way_up(fmt):
    from app.services.clean_copy import make_clean_copy
    source = loaded_original(fmt, orientation=6)
    data, _ = make_clean_copy(source)
    assert_clean(data)
    want, got = ImageOps.exif_transpose(opened(source)), ImageOps.exif_transpose(opened(data))
    assert got.size == want.size and got.tobytes() == want.tobytes()


def test_progressive_jpegs_work():
    from app.services.clean_copy import make_clean_copy
    source = loaded_original("JPEG", progressive=True)
    data, content_type = make_clean_copy(source)
    assert content_type == "image/jpeg" and opened(data).tobytes() == opened(source).tobytes()
    assert_clean(data)


@pytest.mark.parametrize("fmt", list(FORMATS))
def test_if_cleaning_in_place_fails_the_photo_is_rebuilt_from_pixels(fmt, monkeypatch):
    from app.services import clean_copy
    source = loaded_original(fmt)
    for broken in (lambda data, keep: data,  # "cleaned" but still full of details: the check must catch it
                   lambda data, keep: data[: len(data) // 2],
                   lambda data, keep: (_ for _ in ()).throw(ValueError("surprise"))):
        monkeypatch.setattr(clean_copy, "_FORMATS", {opened(source).format: (broken, FORMATS[fmt])})
        data, content_type = clean_copy.make_clean_copy(source)
        out = assert_clean(data)
        assert content_type == "image/jpeg" and out.format == "JPEG" and out.size == (400, 200)


def test_see_through_pictures_get_a_white_background_when_rebuilt():
    from app.services.imaging import make_save_copy, open_image
    buf = io.BytesIO()
    Image.new("RGBA", (10, 10), (0, 0, 0, 0)).save(buf, "PNG")
    out = opened(make_save_copy(open_image(buf.getvalue())))
    assert min(out.getpixel((5, 5))) > 250


def test_browsing_copies_have_no_location_either():
    """A long press on the viewer's picture saves the browsing copy."""
    from app.services.imaging import make_derivatives, open_image
    for fmt in ("JPEG", "HEIF", "WEBP"):
        copies = make_derivatives(open_image(loaded_original(fmt)))
        for data in (copies.display, copies.thumb):
            assert_clean(data)


# ------------------------------------------------------------------------------ the API

@pytest.fixture(scope="module")
def ctx(app_db, storage):
    mp = pytest.MonkeyPatch()
    from app.api import libraries, photos
    c = build_ctx(mp, [(libraries.router, "/api/libraries"), (libraries.invites_router, "/api/invites"),
                       (photos.router, "/api/photos")])
    c.storage = storage
    yield c
    mp.undo()


def stored_photo(ctx, lib, user, status="ready", filename="IMG 1 (a).JPG"):
    from app.services.storage import original_key
    pid = uuid.uuid4()
    key = original_key(lib, pid)
    data = make_image_bytes(gps=GPS, date="2019:07:04 12:00:00")  # the app learns the same date below
    ctx.storage.put_bytes(key, data, "image/jpeg")
    with ctx.engine.begin() as c:
        c.execute(text("INSERT INTO photo (id, library_id, uploader_id, status, original_key, original_filename, "
                       "location, taken_at, taken_at_source, taken_tz) VALUES (:i, :l, :u, :s, :k, :f, "
                       "ST_GeogFromText('POINT(-83.74 42.28)'), '2019-07-04 17:00+00', 'exif_original', '-05:00')"),
                  {"i": pid, "l": lib, "u": user.id, "s": status, "k": key, "f": filename})
    return str(pid), key, data


def save(user, photo):
    return user.call("POST", f"/api/photos/{photo}/save-copy")


def team(ctx):
    owner, editor, viewer = User(ctx, "owner"), User(ctx, "editor"), User(ctx, "viewer")
    lib = make_library(owner)
    join(owner, lib, editor, "editor")
    join(owner, lib, viewer, "viewer")
    return owner, editor, viewer, lib


def test_even_viewers_can_save_and_the_original_is_untouched(ctx):
    from app.services.storage import download_key
    owner, _, viewer, lib = team(ctx)
    pid, original, data = stored_photo(ctx, lib, owner)
    r = save(viewer, pid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["filename"], body["content_type"]) == ("IMG 1 (a).jpg", "image/jpeg")
    assert download_key(lib, pid) in body["url"] and "attachment" in body["url"] and "Signature" in body["url"]
    assert_clean(ctx.storage.get_bytes(download_key(lib, pid)), "2019:07:04 12:00:00", "-05:00")
    assert ctx.storage.get_bytes(original) == data, "the original keeps everything"


def test_only_owners_and_the_uploader_get_the_untouched_original_and_the_location(ctx):
    import httpx
    owner, editor, viewer, lib = team(ctx)
    pid, _, original = stored_photo(ctx, lib, editor)
    other_editor = User(ctx, "other")
    join(owner, lib, other_editor, "editor")

    def fetched(user):
        r = user.call("GET", f"/api/photos/{pid}/url", params={"size": "original"})
        assert r.status_code == 200, r.text
        return httpx.get(r.json()["url"]).content

    for person in (owner, editor):
        assert fetched(person) == original, "byte for byte"
        assert person.call("GET", f"/api/photos/{pid}").json()["location"] is not None
    for person in (viewer, other_editor):
        data = fetched(person)
        assert data != original
        assert_clean(data, "2019:07:04 12:00:00", "-05:00")
        assert person.call("GET", f"/api/photos/{pid}").json()["location"] is None
    # Save and Download clean the photo for everyone, owners included.
    assert_clean(httpx.get(save(owner, pid).json()["url"]).content, "2019:07:04 12:00:00", "-05:00")


def test_the_copy_is_made_once(ctx, monkeypatch):
    from app.api import photos as api
    owner, _, viewer, lib = team(ctx)
    pid = stored_photo(ctx, lib, owner)[0]
    first = save(owner, pid).json()
    monkeypatch.setattr(api, "make_clean_copy", lambda *a: pytest.fail("made again"))
    again = save(viewer, pid).json()
    assert (again["filename"], again["content_type"]) == (first["filename"], first["content_type"])


def test_who_cannot_save(ctx):
    owner, _, viewer, lib = team(ctx)
    pid = stored_photo(ctx, lib, owner)[0]
    outsider = User(ctx, "outsider")
    make_library(outsider)
    assert save(outsider, pid).status_code == 404
    assert outsider.call("GET", f"/api/photos/{pid}/url", params={"size": "original"}).status_code == 404
    assert ctx.client.post(f"/api/photos/{pid}/save-copy").status_code == 401
    assert save(viewer, str(uuid.uuid4())).status_code == 404
    unfinished = stored_photo(ctx, lib, owner, status="processing")[0]
    assert save(owner, unfinished).status_code == 404
    assert viewer.call("GET", f"/api/photos/{unfinished}/url", params={"size": "original"}).status_code == 404


@pytest.mark.parametrize("name,saved", [("../../etc/passwd", "etc_passwd.heic"), ('a"b\\c.png', "a_b_c.heic"),
                                        ("", "photo.heic"), ("Cumpleaños.jpg", "Cumplea_os.heic")])
def test_file_names_are_made_safe(name, saved, app_db):
    from app.api.photos import _save_name
    assert _save_name(name, "heic") == saved


def test_deleting_a_photo_removes_its_saved_copies(ctx):
    from app.services.storage import download_keys
    owner, _, _, lib = team(ctx)
    pid = stored_photo(ctx, lib, owner)[0]
    assert owner.call("DELETE", f"/api/photos/{pid}").status_code == 204
    with ctx.engine.connect() as c:
        queued = str(c.execute(text("SELECT json_agg(payload) FROM job WHERE kind = 'delete_objects'")).scalar())
    for key in download_keys(lib, pid):
        assert key in queued


def test_changing_the_date_replaces_the_saved_copy(ctx):
    from app.services.storage import download_key
    owner, _, viewer, lib = team(ctx)
    pid = stored_photo(ctx, lib, owner)[0]
    assert save(viewer, pid).status_code == 200
    assert owner.call("PATCH", f"/api/photos/{pid}", json={"taken_at": "1975-03-02T12:00:00"}).status_code == 200
    assert ctx.storage.head(download_key(lib, pid)) is None, "gone at once, without waiting for the worker"
    assert save(viewer, pid).status_code == 200
    assert_clean(ctx.storage.get_bytes(download_key(lib, pid)), "1975:03:02 12:00:00")


@pytest.mark.parametrize("extra", [{"utc_offset": "+09:00"}, {"taken_local": datetime(1999, 9, 9)}])
def test_the_check_rejects_any_detail_that_was_not_asked_for(extra, monkeypatch):
    """Even a harmless-looking addition, with the picture intact, means the in-place result is not trusted."""
    from app.services import clean_copy
    real = clean_copy._jpeg

    def adds_something(data, keep):
        return real(data, clean_copy.Keep(**{"orientation": keep.orientation, "size": keep.size, **extra}))

    monkeypatch.setattr(clean_copy, "_FORMATS", {"JPEG": (adds_something, "image/jpeg")})
    source = loaded_original("JPEG")
    out, _ = clean_copy.make_clean_copy(source)  # asked for no date at all
    assert_clean(out)
