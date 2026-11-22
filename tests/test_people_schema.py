"""Phase 4b schema: nicknames, selfie references, rejections. Who may read and write what.

Selfie references are biometric: only owners/editors and the person themselves may read them.
"""
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

OWNER, EDITOR, VIEWER, SELF, OTHER_OWNER = (uuid.uuid4() for _ in range(5))
L1, L2 = uuid.uuid4(), uuid.uuid4()
PERSON, SELF_PERSON, OTHER_PERSON = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
PHOTO, OTHER_PHOTO = uuid.uuid4(), uuid.uuid4()
FACE, OTHER_FACE = uuid.uuid4(), uuid.uuid4()
VEC = "[" + ",".join(["0.1"] * 512) + "]"


@pytest.fixture(scope="module")
def env(app_db):
    eng = app_db.engine()
    with eng.begin() as c:
        for u, h in [(OWNER, "p-owner"), (EDITOR, "p-editor"), (VIEWER, "p-viewer"), (SELF, "p-self"),
                     (OTHER_OWNER, "p-other")]:
            c.execute(text("INSERT INTO auth.users (id, email) VALUES (:i, :e)"), {"i": u, "e": f"{h}@test.dev"})
            c.execute(text("INSERT INTO profile (id, display_name, handle) VALUES (:i, :h, :h)"), {"i": u, "h": h})
        c.execute(text("INSERT INTO library (id, name, created_by) VALUES (:a,'L1',:o),(:b,'L2',:x)"),
                  {"a": L1, "b": L2, "o": OWNER, "x": OTHER_OWNER})
        c.execute(text("""INSERT INTO library_member VALUES (:l1,:o,'owner'),(:l1,:e,'editor'),(:l1,:v,'viewer'),
                          (:l1,:s,'viewer'),(:l2,:x,'owner')"""),
                  {"l1": L1, "l2": L2, "o": OWNER, "e": EDITOR, "v": VIEWER, "s": SELF, "x": OTHER_OWNER})
        c.execute(text("""INSERT INTO person (id, library_id, display_name, profile_id) VALUES
                          (:p,:l1,'Mia',NULL),(:sp,:l1,'Sam',:s),(:op,:l2,'Zed',NULL)"""),
                  {"p": PERSON, "sp": SELF_PERSON, "op": OTHER_PERSON, "l1": L1, "l2": L2, "s": SELF})
        c.execute(text("""INSERT INTO photo (id, library_id, uploader_id, status) VALUES
                          (:p1,:l1,:o,'ready'),(:p2,:l2,:x,'ready')"""),
                  {"p1": PHOTO, "p2": OTHER_PHOTO, "l1": L1, "l2": L2, "o": OWNER, "x": OTHER_OWNER})
        c.execute(text(f"""INSERT INTO face (id, photo_id, library_id, embedding) VALUES
                          (:f1,:p1,:l1,'{VEC}'),(:f2,:p2,:l2,'{VEC}')"""),
                  {"f1": FACE, "f2": OTHER_FACE, "p1": PHOTO, "p2": OTHER_PHOTO, "l1": L1, "l2": L2})
        c.execute(text(f"""INSERT INTO person_reference (library_id, person_id, embedding, model_version, source)
                          VALUES (:l1,:sp,'{VEC}','m','selfie'),(:l1,:p,'{VEC}','m','selfie')"""),
                  {"l1": L1, "sp": SELF_PERSON, "p": PERSON})
    eng.dispose()
    import app.db.rls as rls_mod
    return rls_mod


def run(env, user, sql, **params):
    with env.rls_session(user) as s:
        r = s.execute(text(sql), params)
        return r.all() if r.returns_rows else []


def denied(env, user, sql, **params):
    """True if the statement is refused (error) or touches no rows."""
    try:
        with env.rls_session(user) as s:
            r = s.execute(text(sql), params)
            return r.rowcount == 0
    except DBAPIError:
        return True


ADD_ALIAS = ("INSERT INTO person_alias (library_id, person_id, alias, created_by) "
             "VALUES (:l, :p, :a, :u)")


def test_editors_and_owners_add_aliases_viewers_do_not(env):
    assert not denied(env, OWNER, ADD_ALIAS, l=L1, p=PERSON, a="Mimi", u=OWNER)
    assert not denied(env, EDITOR, ADD_ALIAS, l=L1, p=PERSON, a="Meems", u=EDITOR)
    assert denied(env, VIEWER, ADD_ALIAS, l=L1, p=PERSON, a="Nope", u=VIEWER)


def test_everyone_in_the_library_reads_aliases_outsiders_do_not(env):
    assert {r[0] for r in run(env, VIEWER, "SELECT alias FROM person_alias WHERE person_id = :p", p=PERSON)} >= {"Mimi"}
    assert run(env, OTHER_OWNER, "SELECT 1 FROM person_alias") == []


def test_alias_cannot_be_filed_under_another_librarys_person(env):
    assert denied(env, OWNER, ADD_ALIAS, l=L1, p=OTHER_PERSON, a="Sneaky", u=OWNER)
    assert denied(env, OTHER_OWNER, ADD_ALIAS, l=L1, p=PERSON, a="Sneaky", u=OTHER_OWNER)


def test_alias_cannot_be_attributed_to_someone_else(env):
    assert denied(env, EDITOR, ADD_ALIAS, l=L1, p=PERSON, a="Forged", u=OWNER)


def test_alias_is_unique_per_person_ignoring_case_and_spaces(env):
    assert denied(env, OWNER, ADD_ALIAS, l=L1, p=PERSON, a="  MIMI ", u=OWNER)


def test_blank_alias_is_refused(env):
    assert denied(env, OWNER, ADD_ALIAS, l=L1, p=PERSON, a="   ", u=OWNER)


def test_viewers_cannot_delete_aliases(env):
    assert denied(env, VIEWER, "DELETE FROM person_alias WHERE person_id = :p", p=PERSON)
    assert not denied(env, EDITOR, "DELETE FROM person_alias WHERE alias = 'Meems'")


# ----------------------------------------------------------------- selfie references (biometric)

def test_references_are_readable_by_editors_and_owners(env):
    assert len(run(env, OWNER, "SELECT 1 FROM person_reference WHERE library_id = :l", l=L1)) == 2
    assert len(run(env, EDITOR, "SELECT 1 FROM person_reference WHERE library_id = :l", l=L1)) == 2


def test_a_person_reads_only_their_own_references(env):
    rows = run(env, SELF, "SELECT person_id FROM person_reference")
    assert {r[0] for r in rows} == {SELF_PERSON}


def test_other_viewers_and_outsiders_cannot_read_references(env):
    assert run(env, VIEWER, "SELECT 1 FROM person_reference") == []
    assert run(env, OTHER_OWNER, "SELECT 1 FROM person_reference") == []


@pytest.mark.parametrize("who", [OWNER, EDITOR, SELF])
def test_nobody_can_write_references_through_the_api_role(env, who):
    assert denied(env, who, f"INSERT INTO person_reference (library_id, person_id, embedding, model_version, source) "
                            f"VALUES (:l, :p, '{VEC}', 'm', 'selfie')", l=L1, p=PERSON)
    assert denied(env, who, "DELETE FROM person_reference")
    assert denied(env, who, "UPDATE person_reference SET quality = 1")


# ----------------------------------------------------------------------------- rejections

ADD_REJECTION = ("INSERT INTO face_rejection (face_id, person_id, library_id, rejected_by) "
                 "VALUES (:f, :p, :l, :u)")


def test_editors_record_rejections_viewers_cannot(env):
    assert not denied(env, EDITOR, ADD_REJECTION, f=FACE, p=PERSON, l=L1, u=EDITOR)
    assert denied(env, VIEWER, ADD_REJECTION, f=FACE, p=SELF_PERSON, l=L1, u=VIEWER)


def test_rejection_cannot_link_across_libraries(env):
    assert denied(env, OWNER, ADD_REJECTION, f=OTHER_FACE, p=PERSON, l=L1, u=OWNER)
    assert denied(env, OWNER, ADD_REJECTION, f=FACE, p=OTHER_PERSON, l=L1, u=OWNER)


def test_rejections_are_private_to_the_library(env):
    assert run(env, OTHER_OWNER, "SELECT 1 FROM face_rejection") == []
    assert len(run(env, VIEWER, "SELECT 1 FROM face_rejection")) == 1


def test_deleting_a_person_removes_their_aliases_references_and_rejections(env):
    with env.service_session() as s:
        s.execute(text("DELETE FROM person WHERE id = :p"), {"p": PERSON})
        for t in ("person_alias", "person_reference", "face_rejection"):
            assert s.execute(text(f"SELECT count(*) FROM {t} WHERE person_id = :p"), {"p": PERSON}).scalar() == 0


def test_opt_out_defaults_to_false(env):
    assert run(env, OWNER, "SELECT recognition_opt_out FROM person WHERE id = :p", p=SELF_PERSON) == [(False,)]
