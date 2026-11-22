"""Queue face/search enrichment again for photos that were processed by an older face model.

    python scripts/reenrich.py             # photos whose faces_model is not the current model
    python scripts/reenrich.py --library <id> --all   # every ready photo (e.g. after changing CLIP)

The worker (python -m app.worker) then does the work. Faces that people have already labeled are kept.
"""
import argparse
import sys
from pathlib import Path

from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.rls import service_session  # noqa: E402
from app.services import jobs  # noqa: E402
from app.services.faces import MODEL_VERSION  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--library", help="only this library id")
    ap.add_argument("--all", action="store_true", help="every ready photo, not just ones on an older face model")
    args = ap.parse_args()

    with service_session() as s:
        rows = s.execute(
            text("SELECT id FROM photo WHERE status = 'ready' AND display_key IS NOT NULL "
                 "AND (CAST(:lib AS uuid) IS NULL OR library_id = CAST(:lib AS uuid)) "
                 "AND (:all OR faces_model IS DISTINCT FROM :m)"),
            {"lib": args.library, "all": args.all, "m": MODEL_VERSION}).all()
        queued = sum(1 for (pid,) in rows
                     if jobs.enqueue(s, "enrich_photo", {"photo_id": str(pid)}, dedupe_key=f"enrich_photo:{pid}"))
    print(f"{len(rows)} photo(s) need enrichment with {MODEL_VERSION}; queued {queued} job(s).")
    print("Run the worker to process them: python -m app.worker")
    return 0


if __name__ == "__main__":
    sys.exit(main())
