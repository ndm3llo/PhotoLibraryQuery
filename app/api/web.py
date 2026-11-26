"""The web pages (the Memoria app and the older developer pages) and the public config they need.

Everything returned here is public by design: the project URL and the anon/publishable key are
meant to be embedded in client apps. NEVER expose the service-role key.
"""
import os
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, Response

from app.config import supabase_url

router = APIRouter()

_WEB = Path(__file__).resolve().parent.parent / "web"
_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


@router.get("/api/config")
def public_config():
    anon_key = (os.getenv("SUPABASE_ANON_KEY") or "").strip()
    if not anon_key:
        raise HTTPException(status_code=503, detail="SUPABASE_ANON_KEY is not set")
    return {"supabase_url": supabase_url(), "supabase_anon_key": anon_key}


def _app_policy() -> str:
    """Only our own script and style may run; the page talks to this server and the sign-in service only."""
    try:
        auth = " " + supabase_url()
    except RuntimeError:
        auth = ""
    # Saving a photo on a phone reads the file from storage, so the page may connect there too.
    store = urlsplit((os.getenv("cloudflare_endpoint") or "").strip())
    files = f" {store.scheme}://{store.netloc}" if store.scheme in ("http", "https") and store.netloc else ""
    return ("default-src 'none'; script-src 'self'; style-src 'self'; "
            # Photos come from signed storage links (https), or a local stand-in during development.
            "img-src 'self' data: https: http://localhost:* http://127.0.0.1:*; "
            f"connect-src 'self'{auth}{files}; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")


# "/join" is where an invite link lands; the token follows "#", so it never reaches the server.
@router.get("/app", response_class=HTMLResponse, include_in_schema=False)
@router.get("/join", response_class=HTMLResponse, include_in_schema=False)
def app_page():
    return HTMLResponse((_WEB / "app.html").read_text(encoding="utf-8"),
                        headers={**_HEADERS, "Content-Security-Policy": _app_policy()})


@router.get("/assets/app.js", include_in_schema=False)
def app_script():
    return Response((_WEB / "app.js").read_text(encoding="utf-8"), media_type="text/javascript", headers=_HEADERS)


@router.get("/assets/viewer.js", include_in_schema=False)
def viewer_script():
    return Response((_WEB / "viewer.js").read_text(encoding="utf-8"), media_type="text/javascript", headers=_HEADERS)


@router.get("/assets/app.css", include_in_schema=False)
def app_style():
    return Response((_WEB / "app.css").read_text(encoding="utf-8"), media_type="text/css", headers=_HEADERS)


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
@router.get("/invite", response_class=HTMLResponse, include_in_schema=False)
def login_page():
    return HTMLResponse((_WEB / "login.html").read_text(encoding="utf-8"), headers=_HEADERS)


@router.get("/upload", response_class=HTMLResponse, include_in_schema=False)
def upload_page():
    return HTMLResponse((_WEB / "upload.html").read_text(encoding="utf-8"), headers=_HEADERS)


@router.get("/people", response_class=HTMLResponse, include_in_schema=False)
def people_page():
    return HTMLResponse((_WEB / "people.html").read_text(encoding="utf-8"), headers=_HEADERS)
