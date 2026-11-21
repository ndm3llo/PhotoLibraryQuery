"""Helpers for HTTP-level API tests: users with real-looking tokens and a test client."""
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
from jwtutil import make_token, new_key
from sqlalchemy import text


class ApiCtx:
    def __init__(self, engine, key, client):
        self.engine, self.key, self.client = engine, key, client


def build_ctx(monkeypatch_ctx, routers):
    """routers: [(router, prefix)]. Signing key is faked; SUPABASE_URL set to match the test issuer."""
    import app.auth.jwt as auth_jwt
    from app.db.session import engine

    key = new_key()
    monkeypatch_ctx.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch_ctx.setattr(auth_jwt, "get_signing_key", lambda token: key.public_key())
    api = FastAPI()
    for router, prefix in routers:
        api.include_router(router, prefix=prefix)
    return ApiCtx(engine, key, TestClient(api))


class User:
    def __init__(self, ctx, label="u"):
        self.ctx = ctx
        self.id = uuid.uuid4()
        self.email = f"{label}-{self.id.hex[:6]}@test.dev"
        with ctx.engine.begin() as conn:
            conn.execute(text("INSERT INTO auth.users (id, email) VALUES (:i, :e)"), {"i": self.id, "e": self.email})
        self.headers = {"Authorization": "Bearer " + make_token(ctx.key, sub=self.id, email=self.email)}

    def call(self, method, path, **kw):
        return self.ctx.client.request(method, path, headers=self.headers, **kw)


def make_library(owner, name="Fam"):
    r = owner.call("POST", "/api/libraries", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def join(owner, lib, person, role):
    inv = owner.call("POST", f"/api/libraries/{lib}/invites", json={"email": person.email, "role": role})
    assert inv.status_code == 201, inv.text
    acc = person.call("POST", "/api/invites/accept", json={"token": inv.json()["token"]})
    assert acc.status_code == 200, acc.text
