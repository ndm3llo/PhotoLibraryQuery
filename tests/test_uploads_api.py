"""Uploads and photos over HTTP: presigned PUT to a real S3 server, then the worker, then signed reads."""
import uuid
from datetime import datetime, timezone

import httpx
import pytest
from apiutil import User, build_ctx, join, make_library
from ingest_helpers import make_image_bytes
from sqlalchemy import text


class FakeEnrichers:
    clip_model, face_model = "fake-clip", "fake-faces"

    def clip(self, data):
        return [0.5] * 512

    def faces(self, data):
        from app.services.faces import DetectedFace
        return [DetectedFace(bbox=(10, 20, 110, 140), det_score=0.9, quality=0.8, px=100, embedding=[0.1] * 512)]


@pytest.fixture(scope="module")
def ctx(app_db, storage):
    mp = pytest.MonkeyPatch()
    from app.api import libraries, me, photos, uploads
    c = build_ctx(mp, [(me.router, "/api/me"), (libraries.router, "/api/libraries"),
                       (libraries.invites_router, "/api/invites"), (uploads.router, "/api/uploads"),
                       (photos.router, "/api/photos")])
    from app.services.pipeline import Context
    c.worker_ctx = Context(storage=storage, enrichers=lambda: FakeEnrichers())
    c.storage = storage
    yield c
    mp.undo()


@pytest.fixture(autouse=True)
def clean_queue(ctx):
    from app.db.rls import service_session
    with service_session() as s:
        s.execute(text("DELETE FROM job"))


def pump(ctx, limit=30):
    from app.worker import run_once
    n = 0
    while n < limit and run_once("t", ctx.worker_ctx, backoff_base=0):
        n += 1
    return n


def spec(name="IMG_1.jpg", data=None, ct="image/jpeg", mtime=None):
    data = data if data is not None else make_image_bytes(noise=hash(name) % 200 + 1)
    return {"filename": name, "size": len(data), "content_type": ct, "last_modified": mtime}, data


def upload(user, lib, files, *, put=True, complete=True):
    """files: [(spec, bytes)]. Returns (photo_ids, start_response_json, complete_response_json)."""
    r = user.call("POST", "/api/uploads", json={"library_id": lib, "files": [f[0] for f in files]})
    assert r.status_code == 200, r.text
    body = r.json()
    ids = [u.get("photo_id") for u in body["uploads"]]
    if put:
        for u, (_, data) in zip(body["uploads"], files):
            if "upload_url" in u:
                resp = httpx.put(u["upload_url"], content=data, headers=u["headers"])
                assert resp.status_code in (200, 201), resp.text
    done = None
    if complete:
        done = user.call("POST", "/api/uploads/complete", json={"photo_ids": [i for i in ids if i]})
        assert done.status_code == 200, done.text
    return ids, body, done.json() if done else None


def team(ctx):
    owner, editor, viewer, outsider = (User(ctx, n) for n in ("owner", "editor", "viewer", "out"))
    lib = make_library(owner)
    join(owner, lib, editor, "editor")
    join(owner, lib, viewer, "viewer")
    return owner, editor, viewer, outsider, lib


# --------------------------------------------------------------------------------- happy path

def test_full_flow_upload_process_and_read_back(ctx):
    owner, *_, lib = team(ctx)
    original = make_image_bytes((600, 400), date="2019:07:04 15:30:12", orientation=6)
    ids, start, done = upload(owner, lib, [spec("IMG_8457.JPG", original)])
    pid = ids[0]

    # the presigned request is scoped to this library and needs only Content-Type
    u = start["uploads"][0]
    assert f"{lib}/originals/{pid}" in u["upload_url"] and u["headers"] == {"Content-Type": "image/jpeg"}
    assert u["method"] == "PUT" and u["expires_in"] == 900
    assert done["results"] == [{"photo_id": pid, "status": "processing"}]

    assert pump(ctx) == 2  # process + enrich
    photo = owner.call("GET", f"/api/photos/{pid}").json()
    assert photo["status"] == "ready" and photo["enriched"] is True
    assert (photo["width"], photo["height"]) == (400, 600)
    assert photo["taken_at"].startswith("2019-07-04T15:30:12") and photo["taken_at_source"] == "exif_original"
    assert photo["taken_local"] == "2019-07-04T15:30:12" and photo["taken_tz"] is None, "no offset: local = stored"
    assert photo["filename"] == "IMG_8457.JPG"

    thumb = httpx.get(photo["thumbnail_url"])
    assert thumb.status_code == 200 and thumb.content[:4] == b"RIFF", "a WebP thumbnail is served via a signed link"
    original_link = owner.call("GET", f"/api/photos/{pid}/url", params={"size": "original"}).json()["url"]
    assert httpx.get(original_link).content == original, "the original comes back byte-for-byte"
    display = owner.call("GET", f"/api/photos/{pid}/url").json()
    assert httpx.get(display["url"]).status_code == 200 and display["expires_in"] == 600


def test_evening_photo_keeps_its_local_date_despite_utc(ctx):
    """8:37 pm on Nov 20 in UTC-5 is already Nov 21 in UTC. The photo was taken on the 20th, and searches
    for 'that day' must agree with the person who took it."""
    owner, *_, lib = team(ctx)
    data = make_image_bytes(date="2026:11:20 20:37:06", offset="-05:00", noise=5)
    ids, _, _ = upload(owner, lib, [spec("IMG_2114.jpeg", data)])
    pump(ctx)
    photo = owner.call("GET", f"/api/photos/{ids[0]}").json()
    assert photo["taken_at"].startswith("2026-11-21T01:37:06"), "the stored instant is UTC"
    assert photo["taken_local"] == "2026-11-20T20:37:06" and photo["taken_tz"] == "-05:00"
    listed = owner.call("GET", "/api/photos", params={"library_id": lib}).json()["items"][0]
    assert listed["taken_local"] == "2026-11-20T20:37:06"

    east = make_image_bytes(date="2026:11:20 01:15:00", offset="+09:00", noise=6)
    ids2, _, _ = upload(owner, lib, [spec("IMG_9.jpeg", east)])
    pump(ctx)
    photo = owner.call("GET", f"/api/photos/{ids2[0]}").json()
    assert photo["taken_at"].startswith("2026-11-19T16:15:00") and photo["taken_local"] == "2026-11-20T01:15:00"


def test_heic_without_a_browser_type_is_accepted_by_extension(ctx):
    owner, *_, lib = team(ctx)
    r = owner.call("POST", "/api/uploads", json={"library_id": lib, "files": [
        {"filename": "IMG_8481.HEIC", "size": 1000, "content_type": ""},
        {"filename": "pic.jpeg", "size": 1000, "content_type": "application/octet-stream"}]})
    cts = [u["headers"]["Content-Type"] for u in r.json()["uploads"]]
    assert cts == ["image/heic", "image/jpeg"]


def test_bad_files_are_rejected_individually(ctx):
    owner, *_, lib = team(ctx)
    r = owner.call("POST", "/api/uploads", json={"library_id": lib, "files": [
        {"filename": "ok.jpg", "size": 1000, "content_type": "image/jpeg"},
        {"filename": "doc.pdf", "size": 1000, "content_type": "application/pdf"},
        {"filename": "huge.jpg", "size": 60 * 1024 * 1024, "content_type": "image/jpeg"},
        {"filename": "../../etc/passwd.jpg", "size": 10, "content_type": "image/jpeg"}]})
    ups = r.json()["uploads"]
    assert "upload_url" in ups[0] and "Unsupported" in ups[1]["error"] and "larger than 50 MB" in ups[2]["error"]
    assert ups[3]["filename"] == "passwd.jpg", "path components are stripped"
    with ctx.engine.connect() as c:
        n = c.execute(text("SELECT count(*) FROM photo WHERE library_id = :l"), {"l": lib}).scalar()
    assert n == 2, "rejected files never create rows"


@pytest.mark.parametrize("payload", [{"files": []}, {"files": [{"filename": "", "size": 5}]},
                                     {"files": [{"filename": "a.jpg", "size": 0}]},
                                     {"files": [{"filename": "a.jpg", "size": -5}]}])
def test_invalid_upload_requests_are_422(ctx, payload):
    owner, *_, lib = team(ctx)
    assert owner.call("POST", "/api/uploads", json={"library_id": lib, **payload}).status_code == 422


# -------------------------------------------------------------------------------- permissions

def test_only_owners_and_editors_may_upload(ctx):
    owner, editor, viewer, outsider, lib = team(ctx)
    body = {"library_id": lib, "files": [spec()[0]]}
    assert owner.call("POST", "/api/uploads", json=body).status_code == 200
    assert editor.call("POST", "/api/uploads", json=body).status_code == 200
    assert viewer.call("POST", "/api/uploads", json=body).status_code == 403
    assert outsider.call("POST", "/api/uploads", json=body).status_code == 404


def test_library_must_be_named_when_ambiguous(ctx):
    owner = User(ctx, "multi")
    make_library(owner, "A")
    only = owner.call("POST", "/api/uploads", json={"files": [spec()[0]]})
    assert only.status_code == 200, "a single library is used automatically"
    make_library(owner, "B")
    assert owner.call("POST", "/api/uploads", json={"files": [spec()[0]]}).status_code == 400


def test_nobody_can_complete_someone_elses_upload(ctx):
    owner, editor, viewer, outsider, lib = team(ctx)
    ids, _, _ = upload(owner, lib, [spec("a.jpg")], complete=False)
    for intruder in (editor, outsider):
        r = intruder.call("POST", "/api/uploads/complete", json={"photo_ids": ids})
        assert r.json()["results"][0]["status"] == "not_found"
    with ctx.engine.connect() as c:
        assert c.execute(text("SELECT status FROM photo WHERE id=:i"), {"i": ids[0]}).scalar() == "pending"


def test_photos_are_invisible_and_unlinkable_across_libraries(ctx):
    owner, editor, viewer, outsider, lib = team(ctx)
    ids, _, _ = upload(owner, lib, [spec("a.jpg")])
    pump(ctx)
    pid = ids[0]
    assert viewer.call("GET", f"/api/photos/{pid}").status_code == 200, "members of the library can read"
    assert httpx.get(viewer.call("GET", f"/api/photos/{pid}/url").json()["url"]).status_code == 200
    for path in (f"/api/photos/{pid}", f"/api/photos/{pid}/url", f"/api/photos/{pid}/url?size=original"):
        assert outsider.call("GET", path).status_code == 404, path
    assert outsider.call("GET", "/api/photos").json()["items"] == []
    assert outsider.call("GET", "/api/photos", params={"library_id": lib}).status_code == 404


def test_viewers_cannot_modify_photos(ctx):
    owner, editor, viewer, outsider, lib = team(ctx)
    ids, _, _ = upload(owner, lib, [spec("a.jpg")])
    pump(ctx)
    pid = ids[0]
    assert viewer.call("PATCH", f"/api/photos/{pid}", json={"taken_at": "2019-01-01T00:00:00Z"}).status_code == 403
    assert viewer.call("DELETE", f"/api/photos/{pid}").status_code == 403
    assert viewer.call("POST", f"/api/photos/{pid}/retry").status_code == 403
    for u in (outsider,):
        assert u.call("PATCH", f"/api/photos/{pid}", json={"taken_at": "2019-01-01T00:00:00Z"}).status_code == 404
        assert u.call("DELETE", f"/api/photos/{pid}").status_code == 404


# ------------------------------------------------------------------------------ completion

def test_complete_reports_missing_and_mismatched_uploads(ctx):
    owner, *_, lib = team(ctx)
    ids, start, done = upload(owner, lib, [spec("never.jpg")], put=False)
    assert done["results"][0]["status"] == "not_uploaded"

    # a truncated upload: declared size differs from what reached the bucket
    s, data = spec("trunc.jpg")
    r = owner.call("POST", "/api/uploads", json={"library_id": lib, "files": [s]}).json()["uploads"][0]
    httpx.put(r["upload_url"], content=data[: len(data) // 2], headers=r["headers"])
    res = owner.call("POST", "/api/uploads/complete", json={"photo_ids": [r["photo_id"]]}).json()
    assert res["results"][0]["status"] == "size_mismatch"

    # the client re-uploads correctly and completes again
    httpx.put(r["upload_url"], content=data, headers=r["headers"])
    res = owner.call("POST", "/api/uploads/complete", json={"photo_ids": [r["photo_id"]]}).json()
    assert res["results"][0]["status"] == "processing"


def test_complete_is_idempotent_and_queues_one_job(ctx):
    owner, *_, lib = team(ctx)
    ids, _, _ = upload(owner, lib, [spec("a.jpg")])
    again = owner.call("POST", "/api/uploads/complete", json={"photo_ids": ids}).json()
    assert again["results"][0]["status"] == "processing"
    with ctx.engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM job WHERE kind='process_photo'")).scalar() == 1
    pump(ctx)
    assert owner.call("POST", "/api/uploads/complete", json={"photo_ids": ids}).json()["results"][0]["status"] == "ready"


def test_a_batch_of_many_files_all_arrive(ctx):
    owner, *_, lib = team(ctx)
    files = [spec(f"IMG_{i}.jpg", make_image_bytes(noise=i + 1, color=(i * 20 % 256, 50, 100))) for i in range(12)]
    ids, _, done = upload(owner, lib, files)
    assert all(r["status"] == "processing" for r in done["results"])
    assert pump(ctx) == 24
    items = owner.call("GET", "/api/photos", params={"library_id": lib, "limit": 100}).json()["items"]
    assert len(items) == 12 and {i["status"] for i in items} == {"ready"}


# --------------------------------------------------------------------------------- failures

def test_corrupt_upload_fails_and_can_be_retried_after_replacement(ctx):
    owner, editor, viewer, outsider, lib = team(ctx)
    ids, start, _ = upload(owner, lib, [spec("bad.jpg", b"definitely not a jpeg")])
    pid = ids[0]
    pump(ctx)
    photo = owner.call("GET", f"/api/photos/{pid}").json()
    assert photo["status"] == "failed" and "not a readable image" in photo["error"]
    # Retrying while the file is still corrupt fails again, cleanly, rather than looping forever.
    assert owner.call("POST", f"/api/photos/{pid}/retry").json()["status"] == "processing"
    pump(ctx)
    assert owner.call("GET", f"/api/photos/{pid}").json()["status"] == "failed"

    # Put a good file in place of the corrupt one and retry: now it succeeds.
    good = make_image_bytes((300, 300), noise=77)
    with ctx.engine.connect() as c:
        key = c.execute(text("SELECT original_key FROM photo WHERE id=:i"), {"i": pid}).scalar()
    ctx.storage.put_bytes(key, good, "image/jpeg")
    assert owner.call("POST", f"/api/photos/{pid}/retry").json()["status"] == "processing"
    pump(ctx)
    ready = owner.call("GET", f"/api/photos/{pid}").json()
    assert ready["status"] == "ready" and ready["error"] is None


def test_only_failed_photos_can_be_retried(ctx):
    owner, *_, lib = team(ctx)
    ids, _, _ = upload(owner, lib, [spec("a.jpg")])
    pump(ctx)
    assert owner.call("POST", f"/api/photos/{ids[0]}/retry").status_code == 409


def test_exact_duplicate_upload_is_hidden_but_traceable(ctx):
    owner, *_, lib = team(ctx)
    data = make_image_bytes(noise=150)
    first, _, _ = upload(owner, lib, [spec("a.jpg", data)])
    pump(ctx)
    second, _, _ = upload(owner, lib, [spec("again.jpg", data)])
    pump(ctx)
    listed = owner.call("GET", "/api/photos", params={"library_id": lib}).json()["items"]
    assert [i["id"] for i in listed] == [first[0]], "duplicates are hidden by default"
    dups = owner.call("GET", "/api/photos", params={"library_id": lib, "status": "duplicate"}).json()["items"]
    assert [i["id"] for i in dups] == [second[0]]
    assert owner.call("GET", f"/api/photos/{second[0]}").json()["duplicate_of"] == first[0]


# ------------------------------------------------------------------------------- editing

def test_manual_date_correction_survives_reprocessing(ctx):
    owner, editor, *_ , lib = team(ctx)
    ids, _, _ = upload(editor, lib, [spec("scan.jpg", make_image_bytes(noise=9))])  # no date of its own
    pump(ctx)
    pid = ids[0]
    r = editor.call("PATCH", f"/api/photos/{pid}", json={"taken_at": "2015-05-05T10:00:00Z"})
    assert r.status_code == 200 and r.json()["taken_at_source"] == "manual"

    from app.db.rls import service_session
    from app.services import jobs
    with service_session() as s:
        jobs.enqueue(s, "process_photo", {"photo_id": pid})
    pump(ctx)
    photo = owner.call("GET", f"/api/photos/{pid}").json()
    assert photo["taken_at"].startswith("2015-05-05") and photo["taken_at_source"] == "manual"


@pytest.mark.parametrize("bad", ["1849-12-31T00:00:00Z", "2999-01-01T00:00:00Z", "not a date"])
def test_manual_date_must_be_plausible(ctx, bad):
    owner, *_, lib = team(ctx)
    ids, _, _ = upload(owner, lib, [spec("a.jpg")])
    pump(ctx)
    assert owner.call("PATCH", f"/api/photos/{ids[0]}", json={"taken_at": bad}).status_code == 422


def test_delete_removes_the_row_faces_and_files(ctx):
    owner, editor, viewer, outsider, lib = team(ctx)
    ids, _, _ = upload(owner, lib, [spec("a.jpg")])
    pump(ctx)
    pid = ids[0]
    with ctx.engine.connect() as c:
        keys = c.execute(text("SELECT original_key, display_key, thumb_key FROM photo WHERE id=:i"), {"i": pid}).one()
        assert c.execute(text("SELECT count(*) FROM face WHERE photo_id=:i"), {"i": pid}).scalar() == 1
    assert all(ctx.storage.head(k) for k in keys)

    assert editor.call("DELETE", f"/api/photos/{pid}").status_code == 204
    assert owner.call("GET", f"/api/photos/{pid}").status_code == 404
    pump(ctx)  # delete_objects job
    assert not any(ctx.storage.head(k) for k in keys), "files are removed from the bucket"
    with ctx.engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM face WHERE photo_id=:i"), {"i": pid}).scalar() == 0


# ------------------------------------------------------------------------------- listing

def test_listing_is_newest_first_paginated_and_filtered(ctx):
    owner, *_, lib = team(ctx)
    base = datetime(2020, 1, 1, tzinfo=timezone.utc)
    pids = []
    with ctx.engine.begin() as c:
        for i in range(5):
            pid = uuid.uuid4()
            pids.append(pid)
            c.execute(text("INSERT INTO photo (id, library_id, uploader_id, status, taken_at) "
                           "VALUES (:i, :l, :u, 'ready', :t)"),
                      {"i": pid, "l": lib, "u": owner.id, "t": base.replace(day=i + 1)})
        c.execute(text("INSERT INTO photo (id, library_id, uploader_id, status) VALUES (gen_random_uuid(), :l, :u, 'pending')"),
                  {"l": lib, "u": owner.id})

    seen, cursor = [], None
    for _ in range(5):
        page = owner.call("GET", "/api/photos", params={"library_id": lib, "limit": 2, **({"cursor": cursor} if cursor else {})}).json()
        seen += [i["id"] for i in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == [str(p) for p in reversed(pids)], "newest capture date first, pending rows hidden, no gaps or repeats"

    pending = owner.call("GET", "/api/photos", params={"library_id": lib, "status": "pending"}).json()["items"]
    assert len(pending) == 1
    assert owner.call("GET", "/api/photos", params={"cursor": "garbage"}).status_code == 400
    assert owner.call("GET", "/api/photos", params={"limit": 0}).status_code == 422
    assert owner.call("GET", "/api/photos", params={"status": "weird"}).status_code == 422


def test_who_may_add_a_missing_date_and_who_may_change_one(ctx):
    owner, editor, viewer, _, lib = team(ctx)
    other = User(ctx, "other")
    join(owner, lib, other, "editor")
    ids, _, _ = upload(editor, lib, [spec("scan.jpg", make_image_bytes(noise=21)),
                                     spec("cam.jpg", make_image_bytes(date="2019:07:04 15:30:12", noise=22)),
                                     spec("scan2.jpg", make_image_bytes(noise=25))])
    pump(ctx)
    undated, camera, second = ids

    def can(user, pid):
        return user.call("GET", f"/api/photos/{pid}/details").json()["can_set_date"]

    def put(user, pid, when="1962-08-09T12:00:00"):
        return user.call("PATCH", f"/api/photos/{pid}", json={"taken_at": when}).status_code

    # Missing: owners, editors and the uploader may add it; viewers may not.
    assert [can(u, undated) for u in (editor, owner, other, viewer)] == [True, True, True, False]
    assert put(viewer, undated) == 403
    assert put(other, undated) == 200, "another editor adds a missing date"
    assert put(owner, second) == 200, "so does the owner"
    # Once there is a date, only the uploader may change it, wherever it came from.
    for pid in (camera, undated):
        assert [can(u, pid) for u in (editor, owner, other, viewer)] == [True, False, False, False]
        assert put(owner, pid) == put(other, pid) == put(viewer, pid) == 403
    assert put(editor, camera, "2018-01-02T12:00:00") == 200, "a wrong camera clock can be fixed"
    after = editor.call("GET", f"/api/photos/{camera}").json()
    assert after["taken_local"].startswith("2018-01-02") and after["taken_at_source"] == "manual"
    assert put(editor, undated, "1963-01-02T12:00:00") == 200
    listed = editor.call("GET", "/api/photos", params={"library_id": lib, "year": 1963}).json()["items"]
    assert [p["id"] for p in listed] == [undated]


def test_an_uploader_who_may_now_only_look_cannot_set_dates(ctx):
    owner, editor, _, _, lib = team(ctx)
    ids, _, _ = upload(editor, lib, [spec("scan.jpg", make_image_bytes(noise=23))])
    pump(ctx)
    assert owner.call("PATCH", f"/api/libraries/{lib}/members/{editor.id}", json={"role": "viewer"}).status_code == 200
    assert editor.call("GET", f"/api/photos/{ids[0]}/details").json()["can_set_date"] is False
    assert editor.call("PATCH", f"/api/photos/{ids[0]}", json={"taken_at": "2001-01-01T12:00:00"}).status_code == 403


def test_details_say_when_the_photo_was_added(ctx):
    owner, *_, lib = team(ctx)
    ids, _, _ = upload(owner, lib, [spec("a.jpg", make_image_bytes(noise=24))])
    pump(ctx)
    added = owner.call("GET", f"/api/photos/{ids[0]}/details").json()["added_at"]
    assert added[:2] == "20" and "T" in added
