"""Minimal developer sign-in page and the public config it needs.

Everything returned here is public by design: the project URL and the anon/publishable key are
meant to be embedded in client apps. NEVER expose the service-role key.
"""
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

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


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
@router.get("/invite", response_class=HTMLResponse, include_in_schema=False)
def login_page():
    return HTMLResponse((_WEB / "login.html").read_text(encoding="utf-8"), headers=_HEADERS)


@router.get("/upload", response_class=HTMLResponse, include_in_schema=False)
def upload_page():
    return HTMLResponse((_WEB / "upload.html").read_text(encoding="utf-8"), headers=_HEADERS)
