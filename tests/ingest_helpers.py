"""Builders for ingestion tests (users, libraries, photo rows, image bytes)."""
import io
import uuid

from PIL import Image
from sqlalchemy import text

from app.services.storage import original_key


def make_image_bytes(size=(400, 300), color="red", fmt="JPEG", date=None, orientation=None, gps=None, noise=0):
    from app.services import extract_metadata as em

    img = Image.new("RGB", size, color)
    if noise:  # make pictures distinguishable by content
        for i in range(noise):
            img.putpixel((i % size[0], (i * 7) % size[1]), (i * 13 % 256, i * 29 % 256, i * 53 % 256))
    exif = Image.Exif()
    if orientation:
        exif[274] = orientation
    if date:
        exif.get_ifd(em.EXIF_IFD)[em.TAG_DATETIME_ORIGINAL] = date
    if gps:
        exif.get_ifd(em.GPS_IFD).update(gps)
    buf = io.BytesIO()
    kwargs = {"exif": exif} if (orientation or date or gps) else {}
    img.save(buf, fmt, **kwargs)
    return buf.getvalue()


def make_world(engine, role="owner"):
    """A user, a library they belong to, returns (user_id, library_id)."""
    user, lib = uuid.uuid4(), uuid.uuid4()
    with engine.begin() as c:
        c.execute(text("INSERT INTO auth.users (id, email) VALUES (:u, :e)"), {"u": user, "e": f"{user.hex[:8]}@test.dev"})
        c.execute(text("INSERT INTO profile (id, handle, display_name) VALUES (:u, :h, 'T')"),
                  {"u": user, "h": f"u-{user.hex[:10]}"})
        c.execute(text("INSERT INTO library (id, name, created_by) VALUES (:l, 'L', :u)"), {"l": lib, "u": user})
        c.execute(text("INSERT INTO library_member VALUES (:l, :u, :r)"), {"l": lib, "u": user, "r": role})
    return user, lib


def add_photo(engine, storage, user, lib, data, *, filename="IMG_1.jpg", status="processing",
              client_mtime=None, **cols):
    """Insert a photo row in the state right after /uploads/complete, with its original in storage."""
    pid = uuid.uuid4()
    key = original_key(lib, pid)
    if data is not None:
        storage.put_bytes(key, data, "image/jpeg")
    with engine.begin() as c:
        c.execute(
            text("INSERT INTO photo (id, library_id, uploader_id, status, original_key, original_filename, "
                 "client_last_modified) VALUES (:id, :l, :u, :s, :k, :f, :m)"),
            {"id": pid, "l": lib, "u": user, "s": status, "k": key, "f": filename, "m": client_mtime})
        for col, val in cols.items():
            c.execute(text(f"UPDATE photo SET {col} = :v WHERE id = :id"), {"v": val, "id": pid})
    return pid


def photo_row(engine, pid):
    with engine.connect() as c:
        return c.execute(text("SELECT * FROM photo WHERE id = :id"), {"id": pid}).mappings().one()
