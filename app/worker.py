"""Background worker: `python -m app.worker`

Claims jobs from the Postgres queue and runs them. Safe to run several copies; each job is delivered to
one worker at a time, and re-delivered if a worker dies (its lease expires).
"""
import logging
import os
import signal
import socket
import time

from app.db.rls import service_session
from app.services import jobs
from app.services.pipeline import HANDLERS, Context, PermanentJobError, on_dead
from app.services.storage import get_storage

logger = logging.getLogger("worker")


def run_once(worker_id: str, ctx: Context, *, lease_seconds: int = jobs.DEFAULT_LEASE_SECONDS,
             backoff_base: int = jobs.BACKOFF_BASE_SECONDS) -> bool:
    """Claim and run at most one job. Returns True if a job was handled (successfully or not)."""
    with service_session() as s:
        for dead in jobs.reap_dead(s):
            on_dead(dead, "worker lost: lease expired")
        job = jobs.claim(s, worker_id, lease_seconds=lease_seconds)
    if job is None:
        return False

    handler = HANDLERS.get(job.kind)
    try:
        if handler is None:
            raise PermanentJobError(f"no handler for job kind {job.kind!r}")
        result = handler(job.payload, ctx)
    except Exception as e:
        permanent = isinstance(e, PermanentJobError)
        logger.warning("job %s (%s) failed on attempt %s: %s", job.id, job.kind, job.attempts, e)
        with service_session() as s:
            outcome = jobs.fail(s, job, f"{type(e).__name__}: {e}", permanent=permanent, backoff_base=backoff_base)
        if outcome == "dead":
            on_dead(job, f"{type(e).__name__}: {e}")
    else:
        with service_session() as s:
            jobs.complete(s, job.id)
        logger.info("job %s (%s) done: %s", job.id, job.kind, result)
    return True


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    poll = float(os.getenv("WORKER_POLL_SECONDS", "2"))
    ctx = Context(storage=get_storage())
    stop = {"now": False}

    def _stop(signum, _frame):
        logger.info("signal %s received, finishing the current job then exiting", signum)
        stop["now"] = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    logger.info("worker %s started (poll every %.1fs)", worker_id, poll)
    while not stop["now"]:
        try:
            did_work = run_once(worker_id, ctx)
        except Exception:
            logger.exception("worker loop error")
            did_work = False
        if not did_work:
            time.sleep(poll)
    logger.info("worker stopped")


if __name__ == "__main__":
    main()
