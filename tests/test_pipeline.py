"""Photo ingestion pipeline: process, enrich, duplicates, failures, crash recovery."""
import io
import uuid
from datetime import datetime, timezone

import pytest
from ingest_helpers import add_photo, make_image_bytes, make_world, photo_row
from PIL import Image
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError


class FakeEnrichers:
    def __init__(self, faces=2, fail=False):
        self.calls, self.n_faces, self.fail = 0, faces, fail
        self.clip_model, self.face_model = "fake-clip", "fake-faces"

    def clip(self, data):
        self.calls += 1
        if self.fail:
            raise RuntimeError("model exploded")
        return [0.5] * 512

    def faces(self, data):
        return [[0.1 * (i + 1)] * 512 for i in range(self.n_faces)]


@pytest.fixture
def env(app_db, storage):
    from app.db.rls import service_session
    from app.services.pipeline import Context

    eng = app_db.engine()
    with service_session() as s:
        s.execute(text("DELETE FROM job"))
    enr = FakeEnrichers()
    ctx = Context(storage=storage, enrichers=lambda: enr)

    class Env:
        pass

    e = Env()
    e.engine, e.storage, e.ctx, e.enrichers, e.svc = eng, storage, ctx, enr, service_session
    e.user, e.lib = make_world(eng)
    return e


def run_job(e, **kw):
    from app.worker import run_once
    return run_once("test-worker", e.ctx, backoff_base=0, **kw)


def drain(e, limit=20):
    n = 0
    while n < limit and run_job(e):
        n += 1
    return n


def jobs_for(e, kind=None):
    with e.svc() as s:
        q = "SELECT kind, status, attempts, last_error FROM job" + (" WHERE kind = :k" if kind else "") + " ORDER BY created_at"
        return s.execute(text(q), {"k": kind}).all()


def enqueue_process(e, pid):
    from app.services import jobs
    with e.svc() as s:
        jobs.enqueue(s, "process_photo", {"photo_id": str(pid)}, dedupe_key=f"process_photo:{pid}")


# ------------------------------------------------------------------------------ happy path

def test_process_photo_makes_a_usable_photo(env):
    data = make_image_bytes((400, 200), date="2019:07:04 15:30:12", orientation=6,
                            gps={1: "N", 2: (42.0, 30.0, 0.0), 3: "W", 4: (83.0, 15.0, 36.0)})
    pid = add_photo(env.engine, env.storage, env.user, env.lib, data, filename="IMG_8457.JPG")
    enqueue_process(env, pid)
    assert run_job(env) is True

    row = photo_row(env.engine, pid)
    assert row["status"] == "ready" and row["error"] is None
    assert (row["width"], row["height"]) == (200, 400), "EXIF orientation applied"
    assert row["taken_at"] == datetime(2019, 7, 4, 15, 30, 12, tzinfo=timezone.utc)
    assert row["taken_at_source"] == "exif_original" and row["taken_at_confidence"] > 0.85
    assert row["content_sha256"] and isinstance(row["phash"], int)
    assert row["display_key"] == f"{env.lib}/display/{pid}.webp" and row["thumb_key"] == f"{env.lib}/thumbs/{pid}.webp"
    assert Image.open(io.BytesIO(env.storage.get_bytes(row["display_key"]))).format == "WEBP"
    assert max(Image.open(io.BytesIO(env.storage.get_bytes(row["thumb_key"]))).size) <= 400
    assert env.storage.get_bytes(row["original_key"]) == data, "the original is kept byte-for-byte"
    with env.engine.connect() as c:
        assert c.execute(text("SELECT ST_AsText(location::geometry) FROM photo WHERE id = :i"), {"i": pid}).scalar() == "POINT(-83.26 42.5)"
    assert [(j.kind, j.status) for j in jobs_for(env)] == [("process_photo", "done"), ("enrich_photo", "queued")]


def test_enrichment_adds_vectors_and_is_safe_to_repeat(env):
    pid = add_photo(env.engine, env.storage, env.user, env.lib, make_image_bytes())
    enqueue_process(env, pid)
    assert drain(env) == 2  # process, then enrich

    with env.engine.connect() as c:
        assert c.execute(text("SELECT clip_embedding IS NOT NULL, enriched_at IS NOT NULL FROM photo WHERE id=:i"), {"i": pid}).one() == (True, True)
        faces = c.execute(text("SELECT library_id, model_version FROM face WHERE photo_id = :i"), {"i": pid}).all()
    assert len(faces) == 2 and {f[0] for f in faces} == {env.lib} and {f[1] for f in faces} == {"fake-faces"}

    from app.services import jobs
    with env.svc() as s:  # re-run (e.g. after a model upgrade or a crash): must not duplicate faces
        jobs.enqueue(s, "enrich_photo", {"photo_id": str(pid)})
    run_job(env)
    with env.engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM face WHERE photo_id = :i"), {"i": pid}).scalar() == 2


def test_enrichment_failure_leaves_the_photo_usable(env):
    env.enrichers.fail = True
    pid = add_photo(env.engine, env.storage, env.user, env.lib, make_image_bytes())
    enqueue_process(env, pid)
    run_job(env)  # process
    for _ in range(6):
        run_job(env)  # enrich attempts, backoff 0
    row = photo_row(env.engine, pid)
    assert row["status"] == "ready", "a model failure must not hide the photo"
    assert "model exploded" in row["enrich_error"] and row["clip_embedding"] is None
    assert jobs_for(env, "enrich_photo")[0].status == "dead"


# ------------------------------------------------------------------------------ dates

def test_date_falls_back_to_filename_then_mtime_and_manual_dates_survive(env):
    plain = make_image_bytes(color="blue")
    p1 = add_photo(env.engine, env.storage, env.user, env.lib, plain, filename="IMG_20180505_101112.jpg")
    p2 = add_photo(env.engine, env.storage, env.user, env.lib, make_image_bytes(color="green", noise=50),
                   filename="IMG_8457.JPG", client_mtime=datetime(2022, 2, 2, tzinfo=timezone.utc))
    p3 = add_photo(env.engine, env.storage, env.user, env.lib, make_image_bytes(color="white", noise=90),
                   filename="IMG_1.JPG", taken_at=datetime(2001, 1, 1, tzinfo=timezone.utc),
                   taken_at_source="manual", taken_at_confidence=1.0)
    for p in (p1, p2, p3):
        enqueue_process(env, p)
    drain(env, 3)
    assert photo_row(env.engine, p1)["taken_at_source"] == "filename"
    assert photo_row(env.engine, p2)["taken_at_source"] == "file_mtime"
    r3 = photo_row(env.engine, p3)
    assert (r3["taken_at"].year, r3["taken_at_source"]) == (2001, "manual"), "a person's correction is never overwritten"


# ------------------------------------------------------------------------------ duplicates

def test_exact_duplicate_is_marked_and_its_original_freed(env):
    data = make_image_bytes(noise=200)
    first = add_photo(env.engine, env.storage, env.user, env.lib, data)
    enqueue_process(env, first)
    run_job(env)
    second = add_photo(env.engine, env.storage, env.user, env.lib, data)
    enqueue_process(env, second)
    run_job(env, )  # may pick up enrich first; drain to be sure
    drain(env)

    r = photo_row(env.engine, second)
    assert r["status"] == "duplicate" and r["duplicate_of"] == first
    assert r["display_key"] is None and r["content_sha256"] is None and r["original_key"] is None
    assert env.storage.head(f"{env.lib}/originals/{second}") is None, "storage freed"
    assert photo_row(env.engine, first)["status"] == "ready"
    assert [j for j in jobs_for(env, "enrich_photo")].__len__() == 1, "duplicates are not enriched"


def test_the_same_picture_in_another_library_is_not_a_duplicate(env):
    data = make_image_bytes(noise=120)
    other_user, other_lib = make_world(env.engine)
    a = add_photo(env.engine, env.storage, env.user, env.lib, data)
    b = add_photo(env.engine, env.storage, other_user, other_lib, data)
    for p in (a, b):
        enqueue_process(env, p)
    drain(env)
    assert photo_row(env.engine, a)["status"] == "ready" and photo_row(env.engine, b)["status"] == "ready"


def test_resized_copy_is_flagged_as_near_duplicate_not_dropped(env):
    base = Image.new("RGB", (600, 400), "white")
    for i in range(0, 600, 40):
        base.paste((i % 255, 40, 200 - i % 200), (i, 0, i + 20, 400))
    big = io.BytesIO()
    base.save(big, "JPEG", quality=95)
    small = io.BytesIO()
    base.resize((300, 200)).save(small, "JPEG", quality=60)

    a = add_photo(env.engine, env.storage, env.user, env.lib, big.getvalue())
    enqueue_process(env, a)
    run_job(env)
    b = add_photo(env.engine, env.storage, env.user, env.lib, small.getvalue())
    enqueue_process(env, b)
    drain(env)
    rb = photo_row(env.engine, b)
    assert rb["status"] == "ready" and rb["near_duplicate_of"] == a


def test_database_allows_only_one_ready_photo_per_content_hash(env):
    a = add_photo(env.engine, env.storage, env.user, env.lib, make_image_bytes(noise=11), content_sha256="abc")
    b = add_photo(env.engine, env.storage, env.user, env.lib, make_image_bytes(noise=12))
    with pytest.raises(IntegrityError):
        with env.engine.begin() as c:
            c.execute(text("UPDATE photo SET content_sha256='abc' WHERE id=:i"), {"i": b})
    with env.engine.begin() as c:  # but a row marked duplicate may share it
        c.execute(text("UPDATE photo SET content_sha256='abc', status='duplicate' WHERE id=:i"), {"i": b})
    assert a != b


# ------------------------------------------------------------------------------ failures

def test_corrupt_file_fails_once_and_is_reported_on_the_photo(env):
    pid = add_photo(env.engine, env.storage, env.user, env.lib, b"this is not an image at all")
    enqueue_process(env, pid)
    run_job(env)
    row = photo_row(env.engine, pid)
    assert row["status"] == "failed" and "not a readable image" in row["error"]
    j = jobs_for(env, "process_photo")[0]
    assert (j.status, j.attempts) == ("dead", 1), "no pointless retries for a permanent problem"


def test_missing_original_is_a_permanent_failure(env):
    pid = add_photo(env.engine, env.storage, env.user, env.lib, None)  # row exists, no object
    enqueue_process(env, pid)
    run_job(env)
    assert photo_row(env.engine, pid)["status"] == "failed"
    assert "missing" in photo_row(env.engine, pid)["error"]


def test_decompression_bomb_is_a_permanent_failure(env, monkeypatch):
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)
    pid = add_photo(env.engine, env.storage, env.user, env.lib, make_image_bytes((400, 400)))
    enqueue_process(env, pid)
    run_job(env)
    assert photo_row(env.engine, pid)["status"] == "failed" and "too large" in photo_row(env.engine, pid)["error"]


def test_transient_storage_error_is_retried_and_then_succeeds(env, monkeypatch):
    pid = add_photo(env.engine, env.storage, env.user, env.lib, make_image_bytes())
    enqueue_process(env, pid)
    real, calls = env.storage.get_bytes, {"n": 0}

    def flaky(key, max_bytes=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("storage hiccup")
        return real(key, max_bytes)

    monkeypatch.setattr(env.storage, "get_bytes", flaky)
    run_job(env)
    assert photo_row(env.engine, pid)["status"] == "processing"
    j = jobs_for(env, "process_photo")[0]
    assert (j.status, j.attempts) == ("queued", 1) and "storage hiccup" in j.last_error
    run_job(env)
    assert photo_row(env.engine, pid)["status"] == "ready"


def test_photo_deleted_while_queued_is_a_clean_noop(env):
    pid = add_photo(env.engine, env.storage, env.user, env.lib, make_image_bytes())
    enqueue_process(env, pid)
    with env.engine.begin() as c:
        c.execute(text("DELETE FROM photo WHERE id = :i"), {"i": pid})
    run_job(env)
    assert jobs_for(env, "process_photo")[0].status == "done"


# ---------------------------------------------------------------------------- crash recovery

def test_crashed_worker_is_recovered_without_duplicates(env):
    """A worker claims the job and dies. After the lease expires another worker finishes it, and the
    result is exactly one ready photo with one set of files and one enrichment job."""
    from app.services import jobs

    pid = add_photo(env.engine, env.storage, env.user, env.lib, make_image_bytes(noise=33))
    enqueue_process(env, pid)
    with env.svc() as s:
        crashed = jobs.claim(s, "worker-that-dies")  # ...and never completes
        s.execute(text("UPDATE job SET lease_expires_at = now() - interval '1 second' WHERE id = :i"), {"i": crashed.id})
    assert photo_row(env.engine, pid)["status"] == "processing"

    drain(env)
    row = photo_row(env.engine, pid)
    assert row["status"] == "ready"
    assert len(jobs_for(env, "enrich_photo")) == 1
    objects = [o["Key"] for o in env.storage.client.list_objects_v2(Bucket="test-bucket", Prefix=f"{env.lib}/")["Contents"]
               if str(pid) in o["Key"]]
    assert sorted(objects) == sorted([f"{env.lib}/originals/{pid}", f"{env.lib}/display/{pid}.webp", f"{env.lib}/thumbs/{pid}.webp"])
    with env.engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM photo WHERE library_id = :l"), {"l": env.lib}).scalar() == 1


def test_reprocessing_a_ready_photo_is_idempotent(env):
    pid = add_photo(env.engine, env.storage, env.user, env.lib, make_image_bytes(noise=44))
    enqueue_process(env, pid)
    drain(env)
    before = photo_row(env.engine, pid)
    enqueue_process(env, pid)
    drain(env)
    after = photo_row(env.engine, pid)
    assert after["status"] == "ready" and after["content_sha256"] == before["content_sha256"]
    assert after["duplicate_of"] is None


def test_delete_objects_job_removes_files(env):
    key = f"{env.lib}/originals/{uuid.uuid4()}"
    env.storage.put_bytes(key, b"x", "image/jpeg")
    from app.services import jobs
    with env.svc() as s:
        jobs.enqueue(s, "delete_objects", {"keys": [key, key, "never/existed"]})
    run_job(env)
    assert env.storage.head(key) is None
