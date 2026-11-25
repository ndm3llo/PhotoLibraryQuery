"""End-to-end tests of sign-in plumbing, libraries, roles and invites (real HTTP routes, real Postgres)."""
import hashlib
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jwtutil import make_token, new_key
from sqlalchemy import text


@pytest.fixture(scope="module")
def ctx(app_db):
    mp = pytest.MonkeyPatch()
    mp.setenv("SUPABASE_URL", "https://test.supabase.co")
    key = new_key()
    import app.auth.jwt as auth_jwt
    mp.setattr(auth_jwt, "get_signing_key", lambda token: key.public_key())

    from app.api import libraries, me
    from app.db.session import engine

    api = FastAPI()
    api.include_router(me.router, prefix="/api/me")
    api.include_router(libraries.router, prefix="/api/libraries")
    api.include_router(libraries.invites_router, prefix="/api/invites")

    class Ctx:
        pass

    c = Ctx()
    c.key, c.engine, c.client = key, engine, TestClient(api)
    yield c
    mp.undo()


class User:
    def __init__(self, ctx, label):
        self.id = uuid.uuid4()
        self.email = f"{label}-{self.id.hex[:6]}@test.dev"
        with ctx.engine.begin() as conn:
            conn.execute(text("INSERT INTO auth.users (id, email) VALUES (:i, :e)"), {"i": self.id, "e": self.email})
        self.headers = {"Authorization": "Bearer " + make_token(ctx.key, sub=self.id, email=self.email)}
        self.ctx = ctx

    def call(self, method, path, **kw):
        return self.ctx.client.request(method, path, headers=self.headers, **kw)


def make_library(owner, name="Fam"):
    r = owner.call("POST", "/api/libraries", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def invite_and_join(owner, lib, person, role):
    r = owner.call("POST", f"/api/libraries/{lib}/invites", json={"email": person.email, "role": role})
    assert r.status_code == 201, r.text
    acc = person.call("POST", "/api/invites/accept", json={"token": r.json()["token"]})
    assert acc.status_code == 200, acc.text
    return r.json()


# ----------------------------------------------------------------------- authentication

def test_requests_without_a_valid_token_are_rejected(ctx):
    for path in ("/api/me", "/api/libraries"):
        assert ctx.client.get(path).status_code == 401
        assert ctx.client.get(path, headers={"Authorization": "Bearer nonsense"}).status_code == 401
    expired = make_token(ctx.key, exp_in=-5)
    assert ctx.client.get("/api/me", headers={"Authorization": f"Bearer {expired}"}).status_code == 401


def test_first_request_creates_profile_and_it_is_idempotent(ctx):
    u = User(ctx, "newbie")
    first = u.call("GET", "/api/me")
    assert first.status_code == 200
    body = first.json()
    assert body["id"] == str(u.id) and body["libraries"] == []
    assert body["handle"].startswith("newbie-")
    assert u.call("GET", "/api/me").json()["handle"] == body["handle"]


def test_update_display_name_only_affects_me(ctx):
    a, b = User(ctx, "a"), User(ctx, "b")
    a.call("GET", "/api/me")
    b.call("GET", "/api/me")
    assert a.call("PATCH", "/api/me", json={"display_name": "Ana"}).json()["display_name"] == "Ana"
    assert b.call("GET", "/api/me").json()["display_name"] != "Ana"
    assert a.call("PATCH", "/api/me", json={"display_name": ""}).status_code == 422


# ------------------------------------------------------------------- libraries and roles

def test_create_and_list_library(ctx):
    owner, other = User(ctx, "owner"), User(ctx, "other")
    lib = make_library(owner, "Smiths")
    listed = owner.call("GET", "/api/libraries").json()
    assert {"id": lib, "name": "Smiths", "role": "owner"} in listed
    assert other.call("GET", "/api/libraries").json() == []
    assert other.call("GET", f"/api/libraries/{lib}/members").status_code == 404, "non-members learn nothing"
    assert other.call("PATCH", f"/api/libraries/{lib}", json={"name": "x"}).status_code == 404


def test_only_owner_can_rename(ctx):
    owner, editor = User(ctx, "o"), User(ctx, "e")
    lib = make_library(owner)
    invite_and_join(owner, lib, editor, "editor")
    assert editor.call("PATCH", f"/api/libraries/{lib}", json={"name": "Hijack"}).status_code == 403
    assert owner.call("PATCH", f"/api/libraries/{lib}", json={"name": "Renamed"}).status_code == 200


# ----------------------------------------------------------------------------- invites

def test_invite_flow_joins_with_the_invited_role(ctx):
    owner, bob = User(ctx, "owner"), User(ctx, "bob")
    lib = make_library(owner)
    inv = invite_and_join(owner, lib, bob, "editor")
    members = {m["profile_id"]: m["role"] for m in owner.call("GET", f"/api/libraries/{lib}/members").json()}
    assert members == {str(owner.id): "owner", str(bob.id): "editor"}
    assert inv["link"].endswith(inv["token"])
    assert bob.call("GET", "/api/me").json()["libraries"][0]["role"] == "editor"


def test_token_is_stored_hashed_and_never_listed(ctx):
    owner = User(ctx, "owner")
    lib = make_library(owner)
    r = owner.call("POST", f"/api/libraries/{lib}/invites", json={"email": "someone@test.dev"}).json()
    with ctx.engine.connect() as conn:
        stored = conn.execute(text("SELECT token_hash FROM library_invite WHERE id=:i"), {"i": r["id"]}).scalar()
    assert stored == hashlib.sha256(r["token"].encode()).hexdigest() and stored != r["token"]
    listed = owner.call("GET", f"/api/libraries/{lib}/invites").json()
    assert listed and "token" not in listed[0] and "token_hash" not in listed[0]


def test_invite_is_single_use(ctx):
    owner, bob, mallory = User(ctx, "owner"), User(ctx, "bob"), User(ctx, "mallory")
    lib = make_library(owner)
    inv = invite_and_join(owner, lib, bob, "viewer")
    assert bob.call("POST", "/api/invites/accept", json={"token": inv["token"]}).status_code == 400
    assert mallory.call("POST", "/api/invites/accept", json={"token": inv["token"]}).status_code == 400


def test_invite_requires_the_invited_email(ctx):
    owner, bob, carol = User(ctx, "owner"), User(ctx, "bob"), User(ctx, "carol")
    lib = make_library(owner)
    inv = owner.call("POST", f"/api/libraries/{lib}/invites", json={"email": bob.email, "role": "viewer"}).json()
    assert carol.call("POST", "/api/invites/accept", json={"token": inv["token"]}).status_code == 403
    assert carol.call("GET", f"/api/libraries/{lib}/members").status_code == 404, "no access was granted"
    assert bob.call("POST", "/api/invites/accept", json={"token": inv["token"]}).status_code == 200, "still usable by bob"


def test_email_match_is_case_insensitive(ctx):
    owner, bob = User(ctx, "owner"), User(ctx, "bob")
    lib = make_library(owner)
    r = owner.call("POST", f"/api/libraries/{lib}/invites", json={"email": f"  {bob.email.upper()} "})
    assert r.status_code == 201 and r.json()["email"] == bob.email
    assert bob.call("POST", "/api/invites/accept", json={"token": r.json()["token"]}).status_code == 200


def test_expired_and_revoked_invites_do_not_work(ctx):
    owner, bob, dan = User(ctx, "owner"), User(ctx, "bob"), User(ctx, "dan")
    lib = make_library(owner)
    expired = owner.call("POST", f"/api/libraries/{lib}/invites", json={"email": bob.email}).json()
    with ctx.engine.begin() as conn:
        conn.execute(text("UPDATE library_invite SET expires_at = now() - interval '1 minute' WHERE id=:i"),
                     {"i": expired["id"]})
    assert bob.call("POST", "/api/invites/accept", json={"token": expired["token"]}).status_code == 400

    revoked = owner.call("POST", f"/api/libraries/{lib}/invites", json={"email": dan.email}).json()
    assert owner.call("DELETE", f"/api/libraries/{lib}/invites/{revoked['id']}").status_code == 204
    assert dan.call("POST", "/api/invites/accept", json={"token": revoked["token"]}).status_code == 400


def test_reinviting_replaces_the_previous_invite(ctx):
    owner, bob = User(ctx, "owner"), User(ctx, "bob")
    lib = make_library(owner)
    old = owner.call("POST", f"/api/libraries/{lib}/invites", json={"email": bob.email}).json()
    new = owner.call("POST", f"/api/libraries/{lib}/invites", json={"email": bob.email, "role": "editor"}).json()
    assert bob.call("POST", "/api/invites/accept", json={"token": old["token"]}).status_code == 400
    assert bob.call("POST", "/api/invites/accept", json={"token": new["token"]}).status_code == 200
    assert bob.call("GET", "/api/me").json()["libraries"][0]["role"] == "editor"


def test_only_owners_manage_invites(ctx):
    owner, editor, viewer = User(ctx, "o"), User(ctx, "e"), User(ctx, "v")
    lib = make_library(owner)
    invite_and_join(owner, lib, editor, "editor")
    invite_and_join(owner, lib, viewer, "viewer")
    for u in (editor, viewer):
        assert u.call("POST", f"/api/libraries/{lib}/invites", json={"email": "x@test.dev"}).status_code == 403
        assert u.call("GET", f"/api/libraries/{lib}/invites").status_code == 403


def test_invite_input_validation(ctx):
    owner = User(ctx, "owner")
    lib = make_library(owner)
    assert owner.call("POST", f"/api/libraries/{lib}/invites", json={"email": "not-an-email"}).status_code == 422
    assert owner.call("POST", f"/api/libraries/{lib}/invites",
                      json={"email": "a@test.dev", "role": "admin"}).status_code == 422
    assert owner.call("POST", "/api/invites/accept", json={"token": "short"}).status_code == 422


# ------------------------------------------------------------------------ member rules

def test_role_permissions(ctx):
    owner, editor, viewer = User(ctx, "o"), User(ctx, "e"), User(ctx, "v")
    lib = make_library(owner)
    invite_and_join(owner, lib, editor, "editor")
    invite_and_join(owner, lib, viewer, "viewer")

    for u in (editor, viewer):
        assert u.call("GET", f"/api/libraries/{lib}/members").status_code == 200
        assert u.call("PATCH", f"/api/libraries/{lib}/members/{u.id}", json={"role": "owner"}).status_code == 403
        assert u.call("DELETE", f"/api/libraries/{lib}/members/{owner.id}").status_code == 403

    assert owner.call("PATCH", f"/api/libraries/{lib}/members/{viewer.id}", json={"role": "editor"}).status_code == 200
    assert viewer.call("GET", "/api/me").json()["libraries"][0]["role"] == "editor"


def test_members_can_leave_and_owners_can_remove(ctx):
    owner, editor, viewer = User(ctx, "o"), User(ctx, "e"), User(ctx, "v")
    lib = make_library(owner)
    invite_and_join(owner, lib, editor, "editor")
    invite_and_join(owner, lib, viewer, "viewer")
    assert editor.call("DELETE", f"/api/libraries/{lib}/members/{editor.id}").status_code == 204
    assert editor.call("GET", f"/api/libraries/{lib}/members").status_code == 404
    assert owner.call("DELETE", f"/api/libraries/{lib}/members/{viewer.id}").status_code == 204
    assert owner.call("DELETE", f"/api/libraries/{lib}/members/{viewer.id}").status_code == 404
    assert [m["profile_id"] for m in owner.call("GET", f"/api/libraries/{lib}/members").json()] == [str(owner.id)]


def test_last_owner_is_protected(ctx):
    owner, second = User(ctx, "o"), User(ctx, "s")
    lib = make_library(owner)
    assert owner.call("DELETE", f"/api/libraries/{lib}/members/{owner.id}").status_code == 409
    assert owner.call("PATCH", f"/api/libraries/{lib}/members/{owner.id}", json={"role": "editor"}).status_code == 409

    invite_and_join(owner, lib, second, "owner")
    assert owner.call("PATCH", f"/api/libraries/{lib}/members/{owner.id}", json={"role": "editor"}).status_code == 200
    assert second.call("DELETE", f"/api/libraries/{lib}/members/{second.id}").status_code == 409, "second is now the last owner"
    assert second.call("PATCH", f"/api/libraries/{lib}/members/{owner.id}", json={"role": "owner"}).status_code == 200
    assert second.call("DELETE", f"/api/libraries/{lib}/members/{second.id}").status_code == 204


def test_libraries_are_isolated_from_each_other(ctx):
    a, b = User(ctx, "a"), User(ctx, "b")
    lib_a, lib_b = make_library(a, "A"), make_library(b, "B")
    for path in (f"/api/libraries/{lib_b}/members", f"/api/libraries/{lib_b}/invites"):
        assert a.call("GET", path).status_code == 404
    assert a.call("POST", f"/api/libraries/{lib_b}/invites", json={"email": "x@test.dev"}).status_code == 404
    assert a.call("PATCH", f"/api/libraries/{lib_b}/members/{b.id}", json={"role": "viewer"}).status_code == 404
    assert b.call("GET", f"/api/libraries/{lib_a}/members").status_code == 404


def test_actions_are_audited(ctx):
    owner, bob = User(ctx, "owner"), User(ctx, "bob")
    lib = make_library(owner)
    invite_and_join(owner, lib, bob, "viewer")
    with ctx.engine.connect() as conn:
        actions = {r[0] for r in conn.execute(text("SELECT action FROM audit_log WHERE library_id=:l"), {"l": lib})}
    assert {"library.create", "invite.create", "invite.accept"} <= actions


def test_an_invite_without_a_role_makes_an_editor_and_a_guest_can_still_be_a_viewer(ctx):
    owner, relative, guest = User(ctx, "owner"), User(ctx, "relative"), User(ctx, "guest")
    lib = make_library(owner)
    for person, body in [(relative, {"email": relative.email}), (guest, {"email": guest.email, "role": "viewer"})]:
        inv = owner.call("POST", f"/api/libraries/{lib}/invites", json=body)
        assert inv.status_code == 201, inv.text
        assert person.call("POST", "/api/invites/accept", json={"token": inv.json()["token"]}).status_code == 200
    members = {m["profile_id"]: m["role"] for m in owner.call("GET", f"/api/libraries/{lib}/members").json()}
    assert members[str(relative.id)] == "editor" and members[str(guest.id)] == "viewer"
