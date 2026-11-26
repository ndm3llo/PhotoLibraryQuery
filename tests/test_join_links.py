"""Joining a library by shareable link or six-character code (real HTTP routes, real Postgres)."""
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from apiutil import User, build_ctx, join, make_library
from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError

WRONG = "ZZZ-ZZ9"


@pytest.fixture(scope="module")
def ctx(app_db):
    mp = pytest.MonkeyPatch()
    from app.api import libraries
    yield build_ctx(mp, [(libraries.router, "/api/libraries"), (libraries.invites_router, "/api/invites")])
    mp.undo()


def new_link(owner, lib, **body):
    r = owner.call("PUT", f"/api/libraries/{lib}/join-link", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def token_of(link):
    return link["link"].split("#", 1)[1]


def with_code(person, code):
    return person.call("POST", "/api/invites/join", json={"code": code})


def with_link(person, link):
    return person.call("POST", "/api/invites/join", json={"token": token_of(link)})


def roles(owner, lib):
    return {m["profile_id"]: m["role"] for m in owner.call("GET", f"/api/libraries/{lib}/members").json()}


def signed_in(ctx, label):
    """A user with a profile, for tests that call the service directly."""
    from app.auth.jwt import AuthUser
    from app.auth.profiles import ensure_profile
    u = User(ctx, label)
    ensure_profile(AuthUser(id=u.id, email=u.email))
    return u


def failures(ctx, user):
    with ctx.engine.connect() as c:
        return c.execute(text("SELECT count(*) FROM join_attempt WHERE profile_id = :u"), {"u": user.id}).scalar()


# ------------------------------------------------------------------------ the link and code

def test_owner_gets_a_link_and_a_readable_code_that_stay_the_same(ctx):
    owner = User(ctx, "owner")
    lib = make_library(owner)
    assert owner.call("GET", f"/api/libraries/{lib}/join-link").status_code == 404
    link = new_link(owner, lib)
    first, second = link["code"].split("-")
    assert len(first) == len(second) == 3
    from app.services.join import CODE_ALPHABET
    assert set(first + second) <= set(CODE_ALPHABET) and not set(CODE_ALPHABET) & set("0O1I")
    assert "/join#" in link["link"] and len(token_of(link)) >= 32
    assert link["role"] == "editor"
    assert new_link(owner, lib) == link, "asking again shows the same link, it does not replace it"
    assert owner.call("GET", f"/api/libraries/{lib}/join-link").json() == link


def test_many_people_can_join_with_the_link_and_no_email_is_needed(ctx):
    owner, ana, ben = User(ctx, "owner"), User(ctx, "ana"), User(ctx, "ben")
    lib = make_library(owner, "Beach")
    link = new_link(owner, lib)
    r = with_link(ana, link)
    assert r.status_code == 200, r.text
    assert r.json() == {"library_id": lib, "name": "Beach", "role": "editor", "already_member": False}
    assert with_link(ben, link).status_code == 200
    assert roles(owner, lib) == {str(owner.id): "owner", str(ana.id): "editor", str(ben.id): "editor"}


@pytest.mark.parametrize("typed", [
    lambda c: c, lambda c: c.lower(), lambda c: c.replace("-", ""), lambda c: " " + c.replace("-", " ") + " ",
    lambda c: c.replace("-", "–"),
])
def test_codes_ignore_capitals_spaces_and_dashes(ctx, typed):
    owner, ana = User(ctx, "owner"), User(ctx, "ana")
    lib = make_library(owner)
    code = new_link(owner, lib)["code"]
    r = with_code(ana, typed(code))
    assert r.status_code == 200, r.text
    assert r.json()["library_id"] == lib


def test_joiners_get_the_chosen_role_and_changing_it_keeps_the_link(ctx):
    owner, guest, helper = User(ctx, "owner"), User(ctx, "guest"), User(ctx, "helper")
    lib = make_library(owner)
    viewer_link = new_link(owner, lib, role="viewer")
    assert with_code(guest, viewer_link["code"]).json()["role"] == "viewer"
    assert new_link(owner, lib)["role"] == "viewer", "leaving the role out must not widen it"
    editor_link = new_link(owner, lib, role="editor")
    assert (editor_link["code"], editor_link["link"]) == (viewer_link["code"], viewer_link["link"])
    assert with_code(helper, editor_link["code"]).json()["role"] == "editor"
    assert roles(owner, lib)[str(guest.id)] == "viewer", "earlier joiners keep their role"


def test_nobody_becomes_an_owner_through_a_link(ctx):
    owner = User(ctx, "owner")
    lib = make_library(owner)
    assert owner.call("PUT", f"/api/libraries/{lib}/join-link", json={"role": "owner"}).status_code == 422


def test_existing_members_keep_their_role(ctx):
    owner, editor = User(ctx, "owner"), User(ctx, "editor")
    lib = make_library(owner)
    join(owner, lib, editor, "editor")
    link = new_link(owner, lib, role="viewer")
    for person, role in ((owner, "owner"), (editor, "editor")):
        r = with_link(person, link).json()
        assert r["already_member"] is True and r["role"] == role
    assert roles(owner, lib) == {str(owner.id): "owner", str(editor.id): "editor"}


def test_reset_stops_the_old_link_and_code_and_keeps_the_role(ctx):
    owner, ana = User(ctx, "owner"), User(ctx, "ana")
    lib = make_library(owner)
    old = new_link(owner, lib, role="viewer")
    new = owner.call("POST", f"/api/libraries/{lib}/join-link/reset").json()
    assert new["code"] != old["code"] and new["link"] != old["link"] and new["role"] == "viewer"
    assert with_link(ana, old).status_code == 400
    assert with_code(ana, old["code"]).status_code == 400
    assert with_code(ana, new["code"]).status_code == 200


def test_turning_the_link_off(ctx):
    owner, ana = User(ctx, "owner"), User(ctx, "ana")
    lib = make_library(owner)
    link = new_link(owner, lib)
    assert owner.call("DELETE", f"/api/libraries/{lib}/join-link").status_code == 204
    assert with_link(ana, link).status_code == 400
    assert owner.call("GET", f"/api/libraries/{lib}/join-link").status_code == 404


def test_links_expire_after_30_days_and_are_then_replaced(ctx):
    owner, ana = User(ctx, "owner"), User(ctx, "ana")
    lib = make_library(owner)
    old = new_link(owner, lib)
    with ctx.engine.begin() as c:
        days = c.execute(text("SELECT extract(epoch FROM expires_at - created_at) / 86400 FROM library_join_link "
                              "WHERE library_id = :l"), {"l": lib}).scalar()
        assert round(float(days)) == 30
        c.execute(text("UPDATE library_join_link SET expires_at = now() - interval '1 second' WHERE library_id = :l"),
                  {"l": lib})
    link_reply, code_reply = with_link(ana, old), with_code(ana, old["code"])
    assert link_reply.status_code == code_reply.status_code == 400
    assert "no longer works" in link_reply.json()["detail"] and "code didn't work" in code_reply.json()["detail"]
    assert owner.call("GET", f"/api/libraries/{lib}/join-link").status_code == 404
    assert new_link(owner, lib)["code"] != old["code"]


def test_a_code_collision_draws_a_new_code(ctx, monkeypatch):
    from app.services import join as joining
    a, b = User(ctx, "a"), User(ctx, "b")
    lib_a, lib_b = make_library(a), make_library(b)
    taken = new_link(a, lib_a)["code"].replace("-", "")
    draws = iter([taken, "HJK234"])
    monkeypatch.setattr(joining, "new_code", lambda: next(draws))
    assert new_link(b, lib_b)["code"] == "HJK-234"


def test_only_owners_manage_the_link(ctx):
    owner, editor, viewer, stranger = (User(ctx, x) for x in ("o", "e", "v", "s"))
    lib = make_library(owner)
    join(owner, lib, editor, "editor")
    join(owner, lib, viewer, "viewer")
    link = new_link(owner, lib)
    calls = [("GET", ""), ("PUT", ""), ("POST", "/reset"), ("DELETE", "")]
    for person, expected in ((editor, 403), (viewer, 403), (stranger, 404)):
        for method, suffix in calls:
            r = person.call(method, f"/api/libraries/{lib}/join-link{suffix}", json={})
            assert r.status_code == expected, f"{method}{suffix} -> {r.status_code}"
    assert owner.call("GET", f"/api/libraries/{lib}/join-link").json() == link, "nothing changed"


def test_join_needs_exactly_one_of_token_or_code(ctx):
    ana = User(ctx, "ana")
    for body in ({}, {"code": "ABC-234", "token": "t" * 40}):
        assert ana.call("POST", "/api/invites/join", json=body).status_code == 422


def test_owner_can_see_when_people_joined(ctx):
    owner, ana = User(ctx, "owner"), User(ctx, "ana")
    lib = make_library(owner)
    with_link(ana, new_link(owner, lib))
    members = owner.call("GET", f"/api/libraries/{lib}/members").json()
    assert all(m["joined_at"] for m in members)


def test_joining_is_audited_without_the_code_or_token(ctx):
    owner, ana = User(ctx, "owner"), User(ctx, "ana")
    lib = make_library(owner)
    link = new_link(owner, lib)
    with_code(ana, link["code"])
    with ctx.engine.connect() as c:
        rows = c.execute(text("SELECT action, coalesce(target, '') || coalesce(meta::text, '') AS extra "
                              "FROM audit_log WHERE library_id = :l"), {"l": lib}).all()
    assert {"join_link.save", "member.join"} <= {r.action for r in rows}
    everything = " ".join(r.extra for r in rows)
    assert link["code"].replace("-", "") not in everything and token_of(link) not in everything


# ------------------------------------------------------------------------- removing members

def remove(actor, lib, person):
    return actor.call("DELETE", f"/api/libraries/{lib}/members/{person.id}")


def test_a_removed_member_cannot_rejoin_with_the_old_link_or_code(ctx):
    owner, ana = User(ctx, "owner"), User(ctx, "ana")
    lib = make_library(owner)
    old = new_link(owner, lib, role="viewer")
    assert with_link(ana, old).status_code == 200
    assert remove(owner, lib, ana).status_code == 204
    assert with_link(ana, old).status_code == 400
    assert with_code(ana, old["code"]).status_code == 400
    assert str(ana.id) not in roles(owner, lib)
    assert owner.call("GET", f"/api/libraries/{lib}/join-link").status_code == 404

    new = new_link(owner, lib)
    assert new["code"] != old["code"] and new["link"] != old["link"]
    assert new["role"] == "viewer", "the replacement must not let people do more than before"


def test_leaving_on_your_own_keeps_the_link(ctx):
    owner, ana = User(ctx, "owner"), User(ctx, "ana")
    lib = make_library(owner)
    link = new_link(owner, lib)
    with_link(ana, link)
    assert remove(ana, lib, ana).status_code == 204
    assert owner.call("GET", f"/api/libraries/{lib}/join-link").json() == link
    assert with_link(ana, link).status_code == 200


def test_removal_only_stops_that_librarys_link(ctx):
    owner, ana = User(ctx, "owner"), User(ctx, "ana")
    lib, other = make_library(owner), make_library(owner)
    new_link(owner, lib)
    untouched = new_link(owner, other)
    join(owner, lib, ana, "editor")
    remove(owner, lib, ana)
    assert owner.call("GET", f"/api/libraries/{other}/join-link").json() == untouched


def test_a_refused_removal_keeps_the_link(ctx):
    owner, editor, viewer = User(ctx, "o"), User(ctx, "e"), User(ctx, "v")
    lib = make_library(owner)
    join(owner, lib, editor, "editor")
    join(owner, lib, viewer, "viewer")
    link = new_link(owner, lib)
    assert remove(editor, lib, viewer).status_code == 403
    assert remove(owner, lib, owner).status_code == 409
    assert owner.call("GET", f"/api/libraries/{lib}/join-link").json() == link


def test_removal_directly_in_the_database_also_stops_the_link(ctx):
    """Supabase exposes tables to signed-in users, so the rule cannot live only in the API."""
    from app.db.rls import rls_session
    owner, ana = User(ctx, "owner"), User(ctx, "ana")
    lib = make_library(owner)
    link = new_link(owner, lib)
    with_link(ana, link)
    with rls_session(owner.id) as s:
        s.execute(text("DELETE FROM library_member WHERE library_id = :l AND profile_id = :p"),
                  {"l": lib, "p": ana.id})
    assert with_link(ana, link).status_code == 400


def change_role(actor, lib, person, role):
    return actor.call("PATCH", f"/api/libraries/{lib}/members/{person.id}", json={"role": role})


@pytest.mark.parametrize("before,after", [("editor", "viewer"), ("owner", "editor"), ("owner", "viewer")])
def test_reducing_someones_access_stops_the_link(ctx, before, after):
    owner, ana = User(ctx, "owner"), User(ctx, "ana")
    lib = make_library(owner)
    join(owner, lib, ana, before)
    old = new_link(owner, lib, role="editor")
    assert change_role(owner, lib, ana, after).status_code == 200
    assert remove(ana, lib, ana).status_code == 204, "they leave..."
    assert with_link(ana, old).status_code == 400, "...and cannot come back with more access"
    assert with_code(ana, old["code"]).status_code == 400
    assert new_link(owner, lib)["role"] == "editor", "the setting for new joiners is unchanged"


@pytest.mark.parametrize("before,after", [("viewer", "editor"), ("editor", "owner"), ("editor", "editor")])
def test_raising_or_keeping_access_leaves_the_link(ctx, before, after):
    owner, ana = User(ctx, "owner"), User(ctx, "ana")
    lib = make_library(owner)
    join(owner, lib, ana, before)
    link = new_link(owner, lib)
    assert change_role(owner, lib, ana, after).status_code == 200
    assert owner.call("GET", f"/api/libraries/{lib}/join-link").json() == link


def test_stepping_down_yourself_keeps_the_link(ctx):
    owner, ana = User(ctx, "owner"), User(ctx, "ana")
    lib = make_library(owner)
    join(owner, lib, ana, "owner")
    link = new_link(owner, lib)
    assert change_role(ana, lib, ana, "editor").status_code == 200
    assert owner.call("GET", f"/api/libraries/{lib}/join-link").json() == link


# ------------------------------------------------------------------------------ rate limits

def test_wrong_codes_are_limited_but_a_real_link_still_works(ctx):
    owner, guesser, other = User(ctx, "owner"), User(ctx, "guesser"), User(ctx, "other")
    lib = make_library(owner)
    link = new_link(owner, lib)
    from app.config import JOIN_ATTEMPTS_PER_USER
    for _ in range(JOIN_ATTEMPTS_PER_USER):
        assert with_code(guesser, WRONG).status_code == 400
    assert with_code(guesser, link["code"]).status_code == 429, "even the right code, once limited"
    assert failures(ctx, guesser) == JOIN_ATTEMPTS_PER_USER, "blocked tries do not extend the wait"
    assert with_code(other, link["code"]).status_code == 200, "other people are unaffected"
    assert with_link(guesser, link).status_code == 200


def test_the_limit_wears_off(ctx):
    owner, ana = User(ctx, "owner"), User(ctx, "ana")
    lib = make_library(owner)
    code = new_link(owner, lib)["code"]
    from app.config import JOIN_ATTEMPT_WINDOW_MINUTES, JOIN_ATTEMPTS_PER_USER
    for _ in range(JOIN_ATTEMPTS_PER_USER):
        with_code(ana, WRONG)
    assert with_code(ana, code).status_code == 429
    with ctx.engine.begin() as c:
        c.execute(text("UPDATE join_attempt SET at = at - make_interval(mins => :m) WHERE profile_id = :u"),
                  {"m": JOIN_ATTEMPT_WINDOW_MINUTES + 1, "u": ana.id})
    assert with_code(ana, code).status_code == 200


def test_one_address_cannot_get_around_the_limit_with_many_accounts(ctx):
    from app.config import JOIN_ATTEMPTS_PER_IP, JOIN_ATTEMPTS_PER_USER
    from app.services import join as joining
    address = "203.0.113.7"
    made = 0
    while made < JOIN_ATTEMPTS_PER_IP:
        account = signed_in(ctx, "bot")
        for _ in range(min(JOIN_ATTEMPTS_PER_USER, JOIN_ATTEMPTS_PER_IP - made)):
            with pytest.raises(joining.InvalidInvite):
                joining.join(account.id, code=WRONG, ip=address)
            made += 1
    fresh = signed_in(ctx, "fresh")
    with pytest.raises(joining.TooManyAttempts):
        joining.join(fresh.id, code=WRONG, ip=address)
    with pytest.raises(joining.InvalidInvite):
        joining.join(fresh.id, code=WRONG, ip="198.51.100.9")


def test_parallel_guesses_cannot_slip_past_the_limit(ctx):
    from app.config import JOIN_ATTEMPTS_PER_USER
    from app.services import join as joining
    guesser = signed_in(ctx, "parallel")

    def guess(_):
        try:
            joining.join(guesser.id, code=WRONG)
        except (joining.InvalidInvite, joining.TooManyAttempts) as e:
            return type(e).__name__

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(guess, range(JOIN_ATTEMPTS_PER_USER * 3)))
    assert outcomes.count("InvalidInvite") == JOIN_ATTEMPTS_PER_USER
    assert failures(ctx, guesser) == JOIN_ATTEMPTS_PER_USER


# ------------------------------------------------------------------- database-level access

def test_the_database_only_shows_codes_to_that_librarys_owner(ctx):
    from app.db.rls import rls_session
    owner, editor, outsider = User(ctx, "o"), User(ctx, "e"), User(ctx, "x")
    lib = make_library(owner)
    make_library(outsider)
    join(owner, lib, editor, "editor")
    new_link(owner, lib)
    query = text("SELECT count(*) FROM library_join_link WHERE library_id = :l")
    for person, visible in ((owner, 1), (editor, 0), (outsider, 0)):
        with rls_session(person.id) as s:
            assert s.execute(query, {"l": lib}).scalar() == visible
    for person in (editor, outsider):
        with rls_session(person.id) as s:
            assert s.execute(text("DELETE FROM library_join_link WHERE library_id = :l"), {"l": lib}).rowcount == 0


@pytest.mark.parametrize("statement", [
    "INSERT INTO library_join_link (library_id, token, code, role, expires_at) "
    "VALUES (:l, repeat('a', 40), 'ABC234', 'editor', now() + interval '1 day')",
    "UPDATE library_join_link SET code = 'ABC234' WHERE library_id = :l",
    "SELECT count(*) FROM join_attempt WHERE profile_id = :l",
    "DELETE FROM join_attempt WHERE profile_id = :l",
])
def test_even_owners_cannot_choose_codes_or_touch_the_attempt_log(ctx, statement):
    from app.db.rls import rls_session
    owner = User(ctx, "owner")
    lib = make_library(owner)
    with pytest.raises(ProgrammingError, match="permission denied"):
        with rls_session(owner.id) as s:
            s.execute(text(statement), {"l": uuid.UUID(lib)})
