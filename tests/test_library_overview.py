"""The home screen's album list: counts, cover pictures, and only your own albums."""
import uuid

import pytest
from apiutil import User, build_ctx, join, make_library
from sqlalchemy import text


@pytest.fixture(scope="module")
def ctx(app_db, storage):
    mp = pytest.MonkeyPatch()
    from app.api import libraries
    yield build_ctx(mp, [(libraries.router, "/api/libraries"), (libraries.invites_router, "/api/invites")])
    mp.undo()


def add_photo(ctx, lib, uploader, status="ready", taken="2020-01-01", thumb=True):
    pid = uuid.uuid4()
    with ctx.engine.begin() as c:
        c.execute(text("INSERT INTO photo (id, library_id, uploader_id, status, taken_at, thumb_key) "
                       "VALUES (:i, :l, :u, :s, CAST(:t AS timestamptz), :k)"),
                  {"i": pid, "l": lib, "u": uploader.id, "s": status, "t": taken,
                   "k": f"{lib}/thumbs/{pid}.webp" if thumb else None})
    return str(pid)


def overview(user):
    r = user.call("GET", "/api/libraries/overview")
    assert r.status_code == 200, r.text
    return r.json()


def test_counts_ready_photos_and_uses_the_newest_as_cover(ctx):
    owner = User(ctx, "owner")
    full, empty = make_library(owner, "A full"), make_library(owner, "B empty")
    add_photo(ctx, full, owner, taken="2019-05-01")
    newest = add_photo(ctx, full, owner, taken="2024-05-01")
    add_photo(ctx, full, owner, status="processing", taken="2025-05-01")
    first, second = overview(owner)
    assert (first["id"], first["photo_count"], first["role"]) == (full, 2, "owner")
    assert newest in first["cover_url"] and "Signature" in first["cover_url"], "a signed link to the newest photo"
    assert (second["id"], second["photo_count"], second["cover_url"]) == (empty, 0, None)


def test_only_my_albums_are_listed(ctx):
    owner, friend, stranger = User(ctx, "o"), User(ctx, "f"), User(ctx, "s")
    shared, private = make_library(owner, "Shared"), make_library(owner, "Private")
    join(owner, shared, friend, "viewer")
    add_photo(ctx, private, owner)
    assert [(a["id"], a["role"]) for a in overview(friend)] == [(shared, "viewer")]
    assert overview(stranger) == []


def test_requires_sign_in(ctx):
    assert ctx.client.get("/api/libraries/overview").status_code == 401


def test_album_names_cannot_be_blank_and_are_trimmed(ctx):
    owner = User(ctx, "owner")
    assert owner.call("POST", "/api/libraries", json={"name": "   "}).status_code == 422
    lib = make_library(owner, "  Beach  ")
    assert overview(owner)[0]["name"] == "Beach"
    assert owner.call("PATCH", f"/api/libraries/{lib}", json={"name": "  "}).status_code == 422
