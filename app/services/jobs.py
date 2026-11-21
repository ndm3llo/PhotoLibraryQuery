"""A small durable work queue on Postgres.

Why a table instead of a queue library: it works through the Supabase transaction pooler (libraries
that rely on LISTEN/NOTIFY need a session connection), adds no dependency, and is plenty for a
family's photo volume.

Semantics:
- enqueue(): at most one ACTIVE job per dedupe_key (so a photo is never queued twice at once).
- claim(): atomically takes the oldest runnable job with FOR UPDATE SKIP LOCKED, so concurrent
  workers never get the same job. The claim carries a LEASE. If a worker dies mid-job the lease
  expires and another worker picks the job up again (at-least-once delivery), so handlers must be
  idempotent.
- fail(): retries with exponential backoff, then marks the job 'dead' after max_attempts.
All functions take a Session and do NOT commit; callers decide transaction boundaries.
"""
import json
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlmodel import Session

DEFAULT_LEASE_SECONDS = 300
BACKOFF_BASE_SECONDS = 30
BACKOFF_MAX_SECONDS = 3600


@dataclass(frozen=True)
class Job:
    id: UUID
    kind: str
    payload: dict
    attempts: int
    max_attempts: int


def enqueue(session: Session, kind: str, payload: dict, *, dedupe_key: str | None = None,
            max_attempts: int = 5, delay_seconds: int = 0) -> UUID | None:
    """Returns the new job id, or None if an active job with the same dedupe_key already exists."""
    row = session.execute(
        text("""
            INSERT INTO job (kind, payload, dedupe_key, max_attempts, run_at)
            VALUES (:kind, CAST(:payload AS jsonb), :dedupe, :max_attempts,
                    now() + make_interval(secs => :delay))
            ON CONFLICT (dedupe_key) WHERE dedupe_key IS NOT NULL AND status IN ('queued', 'running')
            DO NOTHING
            RETURNING id"""),
        {"kind": kind, "payload": json.dumps(payload), "dedupe": dedupe_key,
         "max_attempts": max_attempts, "delay": delay_seconds},
    ).first()
    return row[0] if row else None


def claim(session: Session, worker_id: str, *, lease_seconds: int = DEFAULT_LEASE_SECONDS,
          kinds: list[str] | None = None) -> Job | None:
    """Take the oldest runnable job (queued and due, or running with an expired lease)."""
    row = session.execute(
        text("""
            UPDATE job SET status = 'running', attempts = attempts + 1, locked_by = :worker,
                           lease_expires_at = now() + make_interval(secs => :lease)
            WHERE id = (
                SELECT id FROM job
                WHERE ((status = 'queued' AND run_at <= now())
                       OR (status = 'running' AND lease_expires_at < now() AND attempts < max_attempts))
                  AND (CAST(:kinds AS text[]) IS NULL OR kind = ANY (CAST(:kinds AS text[])))
                ORDER BY run_at, created_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1)
            RETURNING id, kind, payload, attempts, max_attempts"""),
        {"worker": worker_id, "lease": lease_seconds, "kinds": kinds},
    ).first()
    if row is None:
        return None
    return Job(id=row.id, kind=row.kind, payload=row.payload, attempts=row.attempts,
               max_attempts=row.max_attempts)


def complete(session: Session, job_id: UUID) -> None:
    session.execute(
        text("UPDATE job SET status = 'done', finished_at = now(), lease_expires_at = NULL, "
             "locked_by = NULL, last_error = NULL WHERE id = :id"),
        {"id": job_id},
    )


def fail(session: Session, job: Job, error: str, *, backoff_base: int = BACKOFF_BASE_SECONDS,
         permanent: bool = False) -> str:
    """Record a failure. Returns 'retry' (requeued with backoff) or 'dead' (attempts exhausted, or the
    failure is permanent, e.g. a corrupt file that no retry can fix)."""
    error = error[:2000]
    if permanent or job.attempts >= job.max_attempts:
        session.execute(
            text("UPDATE job SET status = 'dead', finished_at = now(), lease_expires_at = NULL, "
                 "locked_by = NULL, last_error = :e WHERE id = :id"),
            {"id": job.id, "e": error},
        )
        return "dead"
    delay = min(backoff_base * (2 ** (job.attempts - 1)), BACKOFF_MAX_SECONDS)
    session.execute(
        text("UPDATE job SET status = 'queued', run_at = now() + make_interval(secs => :d), "
             "lease_expires_at = NULL, locked_by = NULL, last_error = :e WHERE id = :id"),
        {"id": job.id, "d": delay, "e": error},
    )
    return "retry"


def reap_dead(session: Session) -> list[Job]:
    """Jobs whose lease expired AFTER their last allowed attempt (the worker died every time).
    Marks them dead and returns them so the caller can run failure hooks."""
    rows = session.execute(
        text("""
            UPDATE job SET status = 'dead', finished_at = now(), lease_expires_at = NULL, locked_by = NULL,
                           last_error = coalesce(last_error, 'worker lost: lease expired')
            WHERE status = 'running' AND lease_expires_at < now() AND attempts >= max_attempts
            RETURNING id, kind, payload, attempts, max_attempts""")
    ).all()
    return [Job(id=r.id, kind=r.kind, payload=r.payload, attempts=r.attempts, max_attempts=r.max_attempts)
            for r in rows]
