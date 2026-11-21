"""The Postgres job queue: delivery, leases, retries, concurrency."""
import threading
import uuid
from collections import Counter

import pytest
from sqlalchemy import text

from app.services import jobs


@pytest.fixture
def svc(app_db):
    """service_session plus a clean queue for every test."""
    from app.db.rls import service_session
    with service_session() as s:
        s.execute(text("DELETE FROM job"))
    return service_session


def job_row(svc, job_id):
    with svc() as s:
        return s.execute(text("SELECT * FROM job WHERE id = :i"), {"i": job_id}).mappings().one()


def test_enqueue_and_claim_round_trip(svc):
    with svc() as s:
        jid = jobs.enqueue(s, "process_photo", {"photo_id": "x"})
    with svc() as s:
        job = jobs.claim(s, "w1")
    assert job.id == jid and job.kind == "process_photo" and job.payload == {"photo_id": "x"} and job.attempts == 1
    row = job_row(svc, jid)
    assert row["status"] == "running" and row["locked_by"] == "w1" and row["lease_expires_at"] is not None
    with svc() as s:
        assert jobs.claim(s, "w2") is None, "a running job with a live lease is not handed out again"


def test_dedupe_key_allows_one_active_job_but_not_after_completion(svc):
    with svc() as s:
        first = jobs.enqueue(s, "k", {}, dedupe_key="photo:1")
        assert jobs.enqueue(s, "k", {}, dedupe_key="photo:1") is None
        assert jobs.enqueue(s, "k", {}, dedupe_key="photo:2") is not None
        assert jobs.enqueue(s, "k", {}) is not None and jobs.enqueue(s, "k", {}) is not None, "no key = no dedupe"
    with svc() as s:
        job = jobs.claim(s, "w")
        assert job.id == first
        assert jobs.enqueue(s, "k", {}, dedupe_key="photo:1") is None, "still active while running"
        jobs.complete(s, job.id)
        assert jobs.enqueue(s, "k", {}, dedupe_key="photo:1") is not None, "finished jobs do not block a retry"


def test_jobs_run_oldest_first_and_future_jobs_wait(svc):
    with svc() as s:
        later = jobs.enqueue(s, "k", {"n": "later"}, delay_seconds=3600)
        a = jobs.enqueue(s, "k", {"n": "a"})
        b = jobs.enqueue(s, "k", {"n": "b"})
    with svc() as s:
        assert jobs.claim(s, "w").id == a
        assert jobs.claim(s, "w").id == b
        assert jobs.claim(s, "w") is None, "the delayed job is not due yet"
    assert job_row(svc, later)["status"] == "queued"


def test_claim_can_be_limited_to_kinds(svc):
    with svc() as s:
        jobs.enqueue(s, "a", {})
        b = jobs.enqueue(s, "b", {})
    with svc() as s:
        assert jobs.claim(s, "w", kinds=["b"]).id == b
        assert jobs.claim(s, "w", kinds=["b"]) is None


def test_concurrent_workers_never_get_the_same_job(svc):
    total = 40
    with svc() as s:
        for i in range(total):
            jobs.enqueue(s, "k", {"i": i})

    claimed, lock = [], threading.Lock()

    def worker(name):
        while True:
            with svc() as s:
                job = jobs.claim(s, name)
            if job is None:
                return
            with lock:
                claimed.append(job.id)
            with svc() as s:
                jobs.complete(s, job.id)

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(8)]
    [t.start() for t in threads]
    [t.join(timeout=60) for t in threads]
    counts = Counter(claimed)
    assert len(claimed) == total and all(n == 1 for n in counts.values()), "each job delivered exactly once"


def test_expired_lease_is_redelivered_to_another_worker(svc):
    with svc() as s:
        jid = jobs.enqueue(s, "k", {})
    with svc() as s:
        first = jobs.claim(s, "dies")
    assert first.attempts == 1
    with svc() as s:  # the worker "crashes": it never completes, and its lease runs out
        s.execute(text("UPDATE job SET lease_expires_at = now() - interval '1 second' WHERE id = :i"), {"i": jid})
    with svc() as s:
        second = jobs.claim(s, "rescuer")
    assert second.id == jid and second.attempts == 2
    assert job_row(svc, jid)["locked_by"] == "rescuer"


def test_failure_backs_off_then_goes_dead(svc):
    with svc() as s:
        jid = jobs.enqueue(s, "k", {}, max_attempts=3)
    for attempt in (1, 2):
        with svc() as s:
            job = jobs.claim(s, "w")
            assert job.attempts == attempt
            assert jobs.fail(s, job, "boom", backoff_base=100) == "retry"
        row = job_row(svc, jid)
        assert row["status"] == "queued" and row["last_error"] == "boom"
        with svc() as s:
            assert jobs.claim(s, "w") is None, "backoff keeps it out of the queue for now"
            s.execute(text("UPDATE job SET run_at = now() WHERE id = :i"), {"i": jid})
    with svc() as s:
        job = jobs.claim(s, "w")
        assert job.attempts == 3
        assert jobs.fail(s, job, "boom 3") == "dead"
    row = job_row(svc, jid)
    assert row["status"] == "dead" and row["finished_at"] is not None
    with svc() as s:
        assert jobs.claim(s, "w") is None


def test_backoff_grows_exponentially(svc):
    with svc() as s:
        jid = jobs.enqueue(s, "k", {}, max_attempts=5)
    delays = []
    for _ in range(3):
        with svc() as s:
            job = jobs.claim(s, "w")
            jobs.fail(s, job, "x", backoff_base=10)
            delays.append(s.execute(text("SELECT extract(epoch FROM run_at - now()) FROM job WHERE id = :i"),
                                    {"i": jid}).scalar())
            s.execute(text("UPDATE job SET run_at = now() WHERE id = :i"), {"i": jid})
    assert [round(d / 10) for d in delays] == [1, 2, 4]


def test_permanent_failures_are_dead_immediately(svc):
    with svc() as s:
        jid = jobs.enqueue(s, "k", {}, max_attempts=5)
    with svc() as s:
        job = jobs.claim(s, "w")
        assert jobs.fail(s, job, "corrupt", permanent=True) == "dead"
    assert job_row(svc, jid)["attempts"] == 1


def test_worker_that_keeps_dying_is_eventually_reaped(svc):
    with svc() as s:
        jid = jobs.enqueue(s, "k", {"photo_id": str(uuid.uuid4())}, max_attempts=2)
    for _ in range(2):
        with svc() as s:
            assert jobs.claim(s, "dies") is not None
            s.execute(text("UPDATE job SET lease_expires_at = now() - interval '1 second' WHERE id = :i"), {"i": jid})
    with svc() as s:
        assert jobs.claim(s, "w") is None, "attempts exhausted: not handed out again"
        dead = jobs.reap_dead(s)
    assert [j.id for j in dead] == [jid]
    assert job_row(svc, jid)["status"] == "dead"
    with svc() as s:
        assert jobs.reap_dead(s) == []


def test_database_rejects_users_reading_the_queue(app_db, svc):
    from sqlalchemy.exc import DBAPIError
    with svc() as s:
        jobs.enqueue(s, "k", {})
    import app.db.rls as rls
    with pytest.raises(DBAPIError, match="permission denied"):
        with rls.rls_session(uuid.uuid4()) as s:
            s.execute(text("SELECT * FROM job"))
