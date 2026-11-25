"""Export every table in the live database to a JSON file, before a migration.

    python scripts/backup_db.py --label pre-0008           # writes backups/pre-0008-<timestamp>.json
    python scripts/backup_db.py --restore-to $URL FILE     # load a backup into a LOCAL throwaway database

Reads the direct (non-pooled) connection MIGRATION_DATABASE_URL from .env. Backups hold real family data:
the backups/ folder is gitignored and files are written owner-readable only. The JSON keeps each row exactly as
Postgres reports it (to_jsonb), so a rehearsal can load it into a local copy of the same schema version with
--restore-to and run the migration against live-shaped data before it ever touches the real database.

Not a replacement for Supabase's own backups (enable those too); this is the quick "undo" snapshot taken
immediately before a schema change. Photos themselves live in R2 and are not touched by migrations.
"""
import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parent.parent
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "db"}
# (spatial_ref_sys is PostGIS's own reference data, restored by the extension, never backed up here.)
# Parents before children, so a restore satisfies foreign keys. Anything not listed is appended (sorted).
ORDER = ["profile", "library", "library_member", "library_invite", "person", "album", "photo", "album_photo",
         "face_cluster", "face", "person_alias", "person_reference", "face_rejection", "biometric_consent",
         "audit_log", "job"]


def tables(conn) -> list[str]:
    names = [r[0] for r in conn.execute(text(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' "
        "AND table_type = 'BASE TABLE' AND table_name NOT IN ('alembic_version', 'spatial_ref_sys')"))]
    return sorted(names, key=lambda n: (ORDER.index(n) if n in ORDER else len(ORDER), n))


def backup(url: str, label: str) -> Path:
    eng = create_engine(url)
    data: dict = {}
    with eng.connect() as c:
        data["_alembic_version"] = c.execute(text("SELECT version_num FROM alembic_version")).scalar()
        data["_auth_users"] = [dict(r) for r in c.execute(text("SELECT id::text AS id, email FROM auth.users")).mappings()]
        for t in tables(c):
            data[t] = [r[0] for r in c.execute(text(f'SELECT to_jsonb(x) FROM "{t}" x'))]  # noqa: S608 (names from catalog)
    out = ROOT / "backups" / f"{label}-{datetime.now():%Y%m%d-%H%M%S}.json"
    out.parent.mkdir(exist_ok=True)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    counts = {k: len(v) for k, v in data.items() if isinstance(v, list)}
    print(f"schema version {data['_alembic_version']}; wrote {out}\nrows: {counts}")
    return out


def restore(url: str, file: Path) -> None:
    host = make_url(url).host
    if host not in LOCAL_HOSTS:
        sys.exit(f"Refusing to restore into non-local host {host!r}")
    data = json.loads(file.read_text())
    eng = create_engine(url)
    with eng.begin() as c:
        for u in data.get("_auth_users", []):
            c.execute(text("INSERT INTO auth.users (id, email) VALUES (CAST(:id AS uuid), :email) ON CONFLICT DO NOTHING"), u)
        for t, rows in data.items():
            if t.startswith("_") or not rows:
                continue
            c.execute(text(f'INSERT INTO "{t}" SELECT * FROM json_populate_recordset(NULL::"{t}", CAST(:rows AS json))'),  # noqa: S608
                      {"rows": json.dumps(rows)})
            print(f"restored {t}: {len(rows)}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--label", default="backup")
    ap.add_argument("--restore-to", metavar="URL")
    ap.add_argument("file", nargs="?", type=Path)
    args = ap.parse_args()
    if args.restore_to:
        if not args.file:
            sys.exit("give the backup file to restore")
        restore(args.restore_to, args.file)
        return
    load_dotenv(ROOT / ".env")
    url = os.getenv("MIGRATION_DATABASE_URL")
    if not url:
        sys.exit("MIGRATION_DATABASE_URL is not set in .env")
    backup(url, args.label)


if __name__ == "__main__":
    main()
