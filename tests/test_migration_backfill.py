"""Migration 0001 -> 0002 must carry legacy prototype data into the new tenancy model."""
import uuid

from sqlalchemy import text

U1, U2 = uuid.uuid4(), uuid.uuid4()
PHOTO_READY, PHOTO_PENDING = uuid.uuid4(), uuid.uuid4()
ALBUM = uuid.uuid4()
FACE = uuid.uuid4()


def _seed_legacy(eng):
    with eng.begin() as c:
        c.execute(text("INSERT INTO profile (id, full_name) VALUES (:a, 'Ana María'), (:b, 'Bo')"),
                  {"a": U1, "b": U2})
        c.execute(text("INSERT INTO album (id, name, created_at, created_by) VALUES (:i, 'Trip', now(), :u)"),
                  {"i": ALBUM, "u": U1})
        c.execute(text("""
            INSERT INTO photo (id, user_id, album_id, storage_path, thumbnail_path, captured_at)
            VALUES (:r, :u, :al, 'https://pub-x.r2.dev/originals/a.jpg',
                    'https://pub-x.r2.dev/thumbnails/a.jpg', '2019-07-04 12:00:00'),
                   (:p, :u, NULL, 'pending', 'pending', NULL)"""),
                  {"r": PHOTO_READY, "p": PHOTO_PENDING, "u": U1, "al": ALBUM})
        c.execute(text(f"""
            INSERT INTO photoface (id, photo_id, album_id, face_embedding)
            VALUES (:f, :p, :al, '[{",".join(["0.1"] * 512)}]')"""),
                  {"f": FACE, "p": PHOTO_READY, "al": ALBUM})


def test_legacy_data_is_backfilled(make_db):
    db = make_db()
    db.upgrade("0001")
    eng = db.engine()
    _seed_legacy(eng)

    db.upgrade("0002")

    with eng.connect() as c:
        libs = c.execute(text("SELECT id, created_by FROM library")).all()
        assert len(libs) == 1, "one default library is created"
        lib, owner = libs[0]
        assert owner == U1, "the heaviest uploader becomes owner"

        roles = dict(c.execute(text("SELECT profile_id, role FROM library_member")).all())
        assert roles == {U1: "owner", U2: "viewer"}, "non-contributors default to viewer, not editor"

        handles = [r[0] for r in c.execute(text("SELECT handle FROM profile"))]
        assert len(set(handles)) == 2 and all(h for h in handles)
        assert c.execute(text("SELECT display_name FROM profile WHERE id=:i"), {"i": U1}).scalar() == "Ana María"

        ready = c.execute(text("SELECT * FROM photo WHERE id=:i"), {"i": PHOTO_READY}).mappings().one()
        assert ready["library_id"] == lib and ready["uploader_id"] == U1
        assert ready["status"] == "ready"
        assert ready["original_key"] == "originals/a.jpg", "full URL is reduced to an object key"
        assert ready["thumb_key"] == "thumbnails/a.jpg"
        assert ready["taken_at_source"] == "legacy" and ready["taken_at"] is not None

        pending = c.execute(text("SELECT status, original_key, taken_at_source FROM photo WHERE id=:i"),
                            {"i": PHOTO_PENDING}).one()
        assert tuple(pending) == ("pending", None, None)

        face = c.execute(text("SELECT library_id, person_id, model_version FROM face WHERE id=:i"),
                         {"i": FACE}).one()
        assert tuple(face) == (lib, None, "facenet512")

        assert c.execute(text("SELECT library_id FROM album WHERE id=:i"), {"i": ALBUM}).scalar() == lib
        links = c.execute(text("SELECT album_id, photo_id FROM album_photo")).all()
        assert [tuple(r) for r in links] == [(ALBUM, PHOTO_READY)]

        cols = {r[0] for r in c.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name='profile'"))}
        assert "face_embedding" not in cols, "unconsented biometric column is dropped"
    eng.dispose()


def test_migration_on_empty_database_and_downgrade(make_db):
    db = make_db()
    db.upgrade("head")
    eng = db.engine()
    with eng.connect() as c:
        assert c.execute(text("SELECT count(*) FROM library")).scalar() == 0
    eng.dispose()

    db.downgrade("0001")
    eng = db.engine()
    with eng.connect() as c:
        tables = {r[0] for r in c.execute(text(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public'"))}
        assert "library" not in tables and "photoface" in tables
    eng.dispose()
