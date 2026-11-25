"""Import a folder of photos through the real API (same path the browser uses).

    export ALBUM_TOKEN=<access token from /login > Copy access token>
    python scripts/bulk_import.py ~/Pictures/family --library <library-id>
    python scripts/bulk_import.py ~/Pictures/family --dry-run
    python scripts/bulk_import.py app_imgs --label-folders   # keep each photo's folder in its filename

Files go straight to storage with presigned URLs; this script then waits for the worker to finish them
(so `python -m app.worker` must be running). Tokens last about an hour: for very large imports, run it
again; already-imported files are detected as duplicates and skipped.
"""
import argparse
import os
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp"}
TERMINAL = {"ready", "failed", "duplicate"}


def find_files(folder: Path) -> list[Path]:
    return sorted(p for p in folder.rglob("*")
                  if p.is_file() and p.suffix.lower() in EXTENSIONS and not any(part.startswith(".") for part in p.parts))


def upload_name(path: Path, root: Path, label_folders: bool) -> str:
    """The filename to store. With --label-folders, folders are kept as a prefix ("ananya__IMG1.jpg",
    "others__looks_like_ari__IMG2.jpg"), which preserves labeled test folders for later checking."""
    rel = path.relative_to(root)
    return "__".join(rel.parts) if label_folders and len(rel.parts) > 1 else path.name


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", type=Path)
    ap.add_argument("--api", default="http://localhost:8000")
    ap.add_argument("--library", help="library id (optional if you belong to exactly one)")
    ap.add_argument("--token", default=os.getenv("ALBUM_TOKEN"), help="access token (or set ALBUM_TOKEN)")
    ap.add_argument("--batch", type=int, default=25)
    ap.add_argument("--threads", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=1800, help="seconds to wait for processing")
    ap.add_argument("--label-folders", action="store_true",
                    help="store each photo's folder path in its filename (for labeled test folders)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    root = args.folder.expanduser()
    files = find_files(root)
    total_mb = sum(f.stat().st_size for f in files) / 1e6
    print(f"{len(files)} photo(s), {total_mb:.1f} MB in {args.folder}")
    if args.dry_run or not files:
        for f in files[:20]:
            print("  ", upload_name(f, root, args.label_folders))
        return 0
    if not args.token:
        print("No token. Sign in at /login, click 'Copy access token', then `export ALBUM_TOKEN=...`", file=sys.stderr)
        return 2

    http = requests.Session()
    http.headers["Authorization"] = f"Bearer {args.token}"

    def call(method, path, **kw):
        r = http.request(method, args.api + path, timeout=60, **kw)
        if r.status_code == 401:
            sys.exit("Token expired or invalid (they last about an hour). Get a fresh one from /login.")
        return r

    ids: dict[str, str] = {}  # photo_id -> filename
    errors: list[str] = []
    for start in range(0, len(files), args.batch):
        chunk = files[start:start + args.batch]
        specs = [{"filename": upload_name(f, root, args.label_folders), "size": f.stat().st_size, "content_type": "",
                  "last_modified": int(f.stat().st_mtime * 1000)} for f in chunk]
        body = {"files": specs}
        if args.library:
            body["library_id"] = args.library
        r = call("POST", "/api/uploads", json=body)
        if r.status_code != 200:
            sys.exit(f"Could not start uploads: HTTP {r.status_code} {r.text[:200]}")
        uploads = r.json()["uploads"]

        def send(pair):
            path, up = pair
            if "error" in up:
                return path, None, up["error"]
            with open(path, "rb") as fh:
                resp = requests.put(up["upload_url"], data=fh, headers=up["headers"], timeout=600)
            return path, up["photo_id"], None if resp.status_code in (200, 201) else f"storage HTTP {resp.status_code}"

        with ThreadPoolExecutor(args.threads) as pool:
            sent = []
            for path, pid, err in pool.map(send, zip(chunk, uploads)):
                if err:
                    errors.append(f"{upload_name(path, root, args.label_folders)}: {err}")
                else:
                    sent.append(pid)
                    ids[pid] = upload_name(path, root, args.label_folders)
        if sent:
            done = call("POST", "/api/uploads/complete", json={"photo_ids": sent}).json()["results"]
            for res in done:
                if res["status"] not in ("processing", "ready"):
                    errors.append(f"{ids.get(res['photo_id'])}: not accepted ({res['status']})")
        print(f"  uploaded {min(start + args.batch, len(files))}/{len(files)}")

    print(f"Waiting for the worker to process {len(ids)} photo(s)...")
    deadline, status = time.time() + args.timeout, {}
    while time.time() < deadline:
        pending = [pid for pid in ids if status.get(pid, {}).get("status") not in TERMINAL]
        if not pending:
            break
        for pid in pending:
            r = call("GET", f"/api/photos/{pid}")
            if r.status_code == 200:
                status[pid] = r.json()
        counts = Counter(s["status"] for s in status.values())
        print(f"  {dict(counts)}", end="\r", flush=True)
        time.sleep(2)
    print()

    counts = Counter(status.get(pid, {}).get("status", "unknown") for pid in ids)
    sources = Counter(s.get("taken_at_source") or "no date" for s in status.values() if s["status"] == "ready")
    print(f"\nResult: {dict(counts)}")
    print(f"Dates found via: {dict(sources)}")
    for pid, s in status.items():
        if s["status"] == "failed":
            errors.append(f"{ids[pid]}: {s.get('error')}")
    for e in errors:
        print("  PROBLEM:", e)
    return 1 if errors or counts.get("unknown") else 0


if __name__ == "__main__":
    sys.exit(main())
