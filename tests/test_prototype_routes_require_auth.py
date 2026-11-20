"""Every route of the prototype API must reject anonymous callers (except the public health check)."""
import re
import sys
import types

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

PUBLIC = {("GET", "/")}


def _stub_heavy_modules(monkeypatch):
    """The real modules load ML models; routes are rejected before they would ever be called."""
    ai = types.ModuleType("app.services.ai")
    ai.ai_service = object()
    faces = types.ModuleType("app.services.img_face_recognition")
    faces.extract_face_embeddings = lambda b: []
    storage = types.ModuleType("app.services.storage")
    storage.storage_service = object()
    for name, mod in [("app.services.ai", ai), ("app.services.img_face_recognition", faces),
                      ("app.services.storage", storage)]:
        monkeypatch.setitem(sys.modules, name, mod)


@pytest.fixture
def app_with_all_routes(app_db, monkeypatch):
    _stub_heavy_modules(monkeypatch)
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    from app.api import (
        base,
        delete_all,
        images,
        libraries,
        me,
        search,
        search_face,
        search_faces_in_db,
        upload,
        upload_folder,
    )
    app = FastAPI()
    app.include_router(base.router)
    app.include_router(me.router, prefix="/api/me")
    app.include_router(libraries.router, prefix="/api/libraries")
    app.include_router(libraries.invites_router, prefix="/api/invites")
    app.include_router(images.router, prefix="/api/images")
    app.include_router(upload.router, prefix="/api/upload")
    app.include_router(upload_folder.router, prefix="/api/folder")
    app.include_router(search.router, prefix="/api/search")
    app.include_router(search_face.router, prefix="/api/search-face")
    app.include_router(search_faces_in_db.router, prefix="/api/debug")
    app.include_router(delete_all.router, prefix="/api/admin")
    return app


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "00000000-0000-0000-0000-000000000000", path)


def test_all_prototype_routes_reject_anonymous_requests(app_with_all_routes):
    client = TestClient(app_with_all_routes, raise_server_exceptions=False)
    checked = []
    for route in app_with_all_routes.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods - {"HEAD", "OPTIONS"}:
            if (method, route.path) in PUBLIC:
                continue
            for headers in ({}, {"Authorization": "Bearer garbage"}):
                r = client.request(method, _concrete(route.path), headers=headers, json={})
                assert r.status_code == 401, f"{method} {route.path} -> {r.status_code} with headers={headers}"
            checked.append((method, route.path))
    assert len(checked) >= 20, f"suspiciously few routes checked: {checked}"


def test_login_page_and_config_are_public_but_never_leak_secrets(app_db, monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "sb_publishable_testkey")
    monkeypatch.setenv("cloudflare_accesss_key", "SECRET-R2-KEY")
    from app.api import web
    app = FastAPI()
    app.include_router(web.router)
    client = TestClient(app)

    page = client.get("/login")
    assert page.status_code == 200 and "Email me a sign-in link" in page.text
    assert page.headers["x-frame-options"] == "DENY" and page.headers["cache-control"] == "no-store"
    assert client.get("/invite").status_code == 200

    cfg = client.get("/api/config")
    assert cfg.json() == {"supabase_url": "https://test.supabase.co", "supabase_anon_key": "sb_publishable_testkey"}
    assert "SECRET-R2-KEY" not in cfg.text + page.text

    monkeypatch.delenv("SUPABASE_ANON_KEY")
    assert client.get("/api/config").status_code == 503
