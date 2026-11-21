"""Live check of the real bucket: signed uploads, signed reads, CORS, and (informational) public access.

    python scripts/check_storage.py [--origin http://localhost:8000]

Uses a throwaway object under `_healthcheck/` and deletes it. Never prints secrets or signed URLs.
Exit code is non-zero if anything that must work does not.

moto (used in the test suite) does not verify signatures, so this is the only check that proves R2
accepts the presigned requests the browser will make, and that infra/r2-cors.json is applied.
"""
import argparse
import os
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # run from anywhere

from app.services.storage import Storage  # noqa: E402

failures = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'ok' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not ok:
        failures.append(name)
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--origin", default="http://localhost:8000", help="browser origin to test CORS for")
    args = ap.parse_args()

    storage = Storage.from_env()
    host = urlparse(os.getenv("cloudflare_endpoint", "")).hostname
    print(f"bucket={storage.bucket!r} endpoint host={host}")

    key = f"_healthcheck/{uuid.uuid4()}"
    body = b"healthcheck " + uuid.uuid4().bytes
    try:
        print("\nSigned upload (what the browser does)")
        url = storage.presign_put(key, "image/jpeg")
        r = httpx.put(url, content=body, headers={"Content-Type": "image/jpeg"})
        check("PUT with only Content-Type is accepted", r.status_code in (200, 201), f"HTTP {r.status_code}")
        head = storage.head(key)
        check("object exists with the right size", bool(head) and head["size"] == len(body))

        print("\nCORS preflight for the upload origin", args.origin)
        pre = httpx.options(url, headers={"Origin": args.origin, "Access-Control-Request-Method": "PUT",
                                          "Access-Control-Request-Headers": "content-type"})
        h = {k.lower(): v for k, v in pre.headers.items()}
        check("preflight answered", pre.status_code in (200, 204), f"HTTP {pre.status_code}")
        check("origin allowed", h.get("access-control-allow-origin") in (args.origin, "*"),
              f"allow-origin={h.get('access-control-allow-origin')!r}")
        check("PUT allowed", "PUT" in h.get("access-control-allow-methods", "").upper(),
              f"allow-methods={h.get('access-control-allow-methods')!r}")
        check("Content-Type header allowed", "content-type" in h.get("access-control-allow-headers", "").lower(),
              f"allow-headers={h.get('access-control-allow-headers')!r}")

        evil = httpx.options(url, headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "PUT"})
        check("a foreign origin is NOT allowed",
              "access-control-allow-origin" not in {k.lower() for k in evil.headers}, f"HTTP {evil.status_code}")

        print("\nSigned read")
        get_url = storage.presign_get(key)
        g = httpx.get(get_url, headers={"Origin": args.origin})
        check("GET via signed link returns the bytes", g.status_code == 200 and g.content == body, f"HTTP {g.status_code}")
        check("ETag readable by the page (ExposeHeaders)",
              "etag" in g.headers.get("access-control-expose-headers", "").lower(),
              f"expose={g.headers.get('access-control-expose-headers')!r}")
        unsigned = httpx.get(get_url.split("?")[0])
        check("the same URL WITHOUT its signature is refused", unsigned.status_code in (400, 401, 403), f"HTTP {unsigned.status_code}")

        print("\nPublic access (informational: should be OFF once the new flow is live)")
        public_base = (os.getenv("R2_PUBLIC_URL") or "").rstrip("/")
        if public_base:
            p = httpx.get(f"{public_base}/{key}")
            state = "STILL PUBLIC: turn off public access in Cloudflare" if p.status_code == 200 else "not public"
            print(f"  [info] bucket via R2_PUBLIC_URL: HTTP {p.status_code} -> {state}")
        else:
            print("  [info] R2_PUBLIC_URL not set")
    finally:
        try:
            storage.delete(key)
            print("\ncleanup: test object deleted")
        except Exception as e:
            print(f"\ncleanup FAILED, delete {key!r} by hand: {e}")

    print("\n" + ("ALL CHECKS PASSED" if not failures else f"{len(failures)} CHECK(S) FAILED: {failures}"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
