"""Row Level Security: a member of one library must never touch another library's data.

This is the most important test file in the repo. If you add a table with a library_id,
add a case here.
"""
import importlib
import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

# Users
A_OWNER, A_EDITOR, A_VIEWER, B_OWNER, OUTSIDER = (uuid.uuid4() for _ in range(5))
# Libraries
L1, L2 = uuid.uuid4(), uuid.uuid4()
# Rows
P1, P2 = uuid.uuid4(), uuid.uuid4()          # photo in L1 / L2
ALB1, ALB2 = uuid.uuid4(), uuid.uuid4()
PERSON1, PERSON2 = uuid.uuid4(), uuid.uuid4()
FACE1, FACE2 = uuid.uuid4(), uuid.uuid4()
VEC = "[" + ",".join(["0.1"] * 512) + "]"


@pytest.fixture(scope="module")
def env(make_db_module):
    db = make_db_module
    db.upgrade("head")
    eng = db.engine()
    with eng.begin() as c:
        for u, h in [(A_OWNER, "a-owner"), (A_EDITOR, "a-editor"), (A_VIEWER, "a-viewer"),
                     (B_OWNER, "b-owner"), (OUTSIDER, "outsider")]:
            c.execute(text("INSERT INTO profile (id, display_name, handle) VALUES (:i, :h, :h)"), {"i": u, "h": h})
        c.execute(text("INSERT INTO library (id, name, created_by) VALUES (:a,'L1',:ao),(:b,'L2',:bo)"),
                  {"a": L1, "b": L2, "ao": A_OWNER, "bo": B_OWNER})
        c.execute(text("""INSERT INTO library_member VALUES
            (:l1,:ao,'owner'),(:l1,:ae,'editor'),(:l1,:av,'viewer'),(:l2,:bo,'owner')"""),
                  {"l1": L1, "l2": L2, "ao": A_OWNER, "ae": A_EDITOR, "av": A_VIEWER, "bo": B_OWNER})
        c.execute(text("""INSERT INTO photo (id, library_id, uploader_id, user_id, storage_path, thumbnail_path, status)
            VALUES (:p1,:l1,:ao,:ao,'s','t','ready'),(:p2,:l2,:bo,:bo,'s','t','ready')"""),
                  {"p1": P1, "p2": P2, "l1": L1, "l2": L2, "ao": A_OWNER, "bo": B_OWNER})
        c.execute(text("""INSERT INTO album (id, name, created_at, created_by, library_id)
            VALUES (:a1,'a1',now(),:ao,:l1),(:a2,'a2',now(),:bo,:l2)"""),
                  {"a1": ALB1, "a2": ALB2, "ao": A_OWNER, "bo": B_OWNER, "l1": L1, "l2": L2})
        c.execute(text("""INSERT INTO person (id, library_id, display_name)
            VALUES (:p1,:l1,'Mia'),(:p2,:l2,'Zed')"""), {"p1": PERSON1, "p2": PERSON2, "l1": L1, "l2": L2})
        c.execute(text(f"""INSERT INTO face (id, photo_id, library_id, face_embedding)
            VALUES (:f1,:p1,:l1,'{VEC}'),(:f2,:p2,:l2,'{VEC}')"""),
                  {"f1": FACE1, "f2": FACE2, "p1": P1, "p2": P2, "l1": L1, "l2": L2})
        # Sensitive, service-role-only rows
        consent = uuid.uuid4()
        c.execute(text("INSERT INTO biometric_consent (id, profile_id, notice_version, granted_at) "
                       "VALUES (:c,:u,'v1',now())"), {"c": consent, "u": A_OWNER})
        c.execute(text("INSERT INTO face_credential (profile_id, embedding_enc, model_version, consent_id) "
                       "VALUES (:u, '\\x00', 'm', :c)"), {"u": A_OWNER, "c": consent})
        c.execute(text("INSERT INTO audit_log (action) VALUES ('seed')"))
    eng.dispose()

    os.environ["DATABASE_URL"] = db.url
    import app.db.session as session_mod
    importlib.reload(session_mod)
    import app.db.rls as rls_mod
    importlib.reload(rls_mod)
    return rls_mod


def q(env, user, sql, **params):
    with env.rls_session(user) as s:
        result = s.execute(text(sql), params)
        return result.all() if result.returns_rows else []


def ids(rows):
    return {r[0] for r in rows}


# ----------------------------------------------------------------------------- reads

@pytest.mark.parametrize("table,col,mine,theirs", [
    ("photo", "id", P1, P2),
    ("album", "id", ALB1, ALB2),
    ("person", "id", PERSON1, PERSON2),
    ("face", "id", FACE1, FACE2),
    ("library", "id", L1, L2),
])
def test_member_sees_only_their_library(env, table, col, mine, theirs):
    for user in (A_OWNER, A_EDITOR, A_VIEWER):
        assert ids(q(env, user, f"SELECT {col} FROM {table}")) == {mine}
    assert ids(q(env, B_OWNER, f"SELECT {col} FROM {table}")) == {theirs}


def test_outsider_sees_nothing(env):
    for table in ("photo", "album", "person", "face", "library", "library_member", "face_cluster"):
        assert q(env, OUTSIDER, f"SELECT 1 FROM {table}") == []


def test_library_members_are_scoped(env):
    assert ids(q(env, A_OWNER, "SELECT profile_id FROM library_member")) == {A_OWNER, A_EDITOR, A_VIEWER}


def test_profiles_visible_only_when_sharing_a_library_or_self(env):
    seen = ids(q(env, A_VIEWER, "SELECT id FROM profile"))
    assert seen == {A_OWNER, A_EDITOR, A_VIEWER}
    assert ids(q(env, OUTSIDER, "SELECT id FROM profile")) == {OUTSIDER}


# ---------------------------------------------------------------------------- writes

def _photo_insert(lib, uploader, pid=None):
    return ("INSERT INTO photo (id, library_id, uploader_id, user_id, storage_path, thumbnail_path) "
            "VALUES (:id, :lib, :up, :up, 's', 't')",
            {"id": pid or uuid.uuid4(), "lib": lib, "up": uploader})


def test_cannot_insert_photo_into_another_library(env):
    sql, p = _photo_insert(L2, A_OWNER)
    with pytest.raises(DBAPIError, match="row-level security"):
        q(env, A_OWNER, sql, **p)


def test_editor_can_insert_but_cannot_spoof_uploader(env):
    sql, p = _photo_insert(L1, A_EDITOR)
    q(env, A_EDITOR, sql, **p)
    sql, p = _photo_insert(L1, A_OWNER)  # claims someone else uploaded it
    with pytest.raises(DBAPIError, match="row-level security"):
        q(env, A_EDITOR, sql, **p)


def test_viewer_cannot_write(env):
    sql, p = _photo_insert(L1, A_VIEWER)
    with pytest.raises(DBAPIError, match="row-level security"):
        q(env, A_VIEWER, sql, **p)
    with env.rls_session(A_VIEWER) as s:
        assert s.execute(text("UPDATE photo SET status='failed' WHERE id=:i"), {"i": P1}).rowcount == 0
        assert s.execute(text("DELETE FROM photo WHERE id=:i"), {"i": P1}).rowcount == 0
        assert s.execute(text("UPDATE person SET display_name='x' WHERE id=:i"), {"i": PERSON1}).rowcount == 0


def test_cannot_update_or_delete_another_librarys_rows(env):
    with env.rls_session(A_OWNER) as s:
        assert s.execute(text("UPDATE photo SET status='failed' WHERE id=:i"), {"i": P2}).rowcount == 0
        assert s.execute(text("DELETE FROM photo WHERE id=:i"), {"i": P2}).rowcount == 0
        assert s.execute(text("UPDATE person SET display_name='x' WHERE id=:i"), {"i": PERSON2}).rowcount == 0
        assert s.execute(text("UPDATE face SET person_id=:p WHERE id=:f"), {"p": PERSON1, "f": FACE2}).rowcount == 0
        assert s.execute(text("DELETE FROM library WHERE id=:i"), {"i": L2}).rowcount == 0


def test_cannot_move_own_photo_into_another_library(env):
    with pytest.raises(DBAPIError, match="row-level security"):
        q(env, A_OWNER, "UPDATE photo SET library_id=:l WHERE id=:i", l=L2, i=P1)


def test_users_cannot_create_faces_directly(env):
    with pytest.raises(DBAPIError, match="permission denied"):
        q(env, A_OWNER, f"INSERT INTO face (photo_id, library_id, face_embedding) VALUES (:p,:l,'{VEC}')",
          p=P1, l=L1)


# ------------------------------------------------------------------------ membership

def test_non_owner_cannot_add_members_and_nobody_can_join_other_library(env):
    with pytest.raises(DBAPIError, match="row-level security"):
        q(env, A_EDITOR, "INSERT INTO library_member VALUES (:l,:u,'editor')", l=L1, u=OUTSIDER)
    with pytest.raises(DBAPIError, match="row-level security"):
        q(env, A_OWNER, "INSERT INTO library_member VALUES (:l,:u,'owner')", l=L2, u=A_OWNER)
    with pytest.raises(DBAPIError, match="row-level security"):
        q(env, OUTSIDER, "INSERT INTO library_member VALUES (:l,:u,'owner')", l=L1, u=OUTSIDER)


def test_owner_can_add_member_who_then_sees_the_library(env):
    q(env, A_OWNER, "INSERT INTO library_member VALUES (:l,:u,'viewer')", l=L1, u=OUTSIDER)
    try:
        seen = ids(q(env, OUTSIDER, "SELECT id FROM photo"))
        assert P1 in seen and P2 not in seen
    finally:
        with env.service_session() as s:
            s.execute(text("DELETE FROM library_member WHERE profile_id=:u"), {"u": OUTSIDER})


def test_cannot_escalate_own_role(env):
    with env.rls_session(A_EDITOR) as s:
        n = s.execute(text("UPDATE library_member SET role='owner' WHERE profile_id=:u"), {"u": A_EDITOR}).rowcount
        assert n == 0


def test_create_library_makes_caller_owner(env):
    with env.rls_session(OUTSIDER) as s:
        lib = s.execute(text("SELECT create_library('Mine')")).scalar()
        assert s.execute(text("SELECT role FROM library_member WHERE library_id=:l AND profile_id=:u"),
                         {"l": lib, "u": OUTSIDER}).scalar() == "owner"
    with env.service_session() as s:
        s.execute(text("DELETE FROM library WHERE id=:l"), {"l": lib})


def test_cannot_insert_library_directly(env):
    with pytest.raises(DBAPIError, match="permission denied"):
        q(env, OUTSIDER, "INSERT INTO library (name, created_by) VALUES ('x', :u)", u=OUTSIDER)


# ------------------------------------------------------------------------ albums

def test_cannot_link_foreign_photo_into_album(env):
    with pytest.raises(DBAPIError, match="row-level security"):
        q(env, A_OWNER, "INSERT INTO album_photo VALUES (:a,:p)", a=ALB1, p=P2)
    q(env, A_OWNER, "INSERT INTO album_photo VALUES (:a,:p)", a=ALB1, p=P1)
    assert ids(q(env, A_VIEWER, "SELECT photo_id FROM album_photo")) == {P1}
    assert q(env, B_OWNER, "SELECT 1 FROM album_photo") == []


# ----------------------------------------------------- sensitive tables stay sealed

@pytest.mark.parametrize("table", ["face_credential", "biometric_consent", "audit_log", "albummemberlink"])
def test_authenticated_cannot_touch_service_only_tables(env, table):
    with pytest.raises(DBAPIError, match="permission denied"):
        q(env, A_OWNER, f"SELECT * FROM {table}")


def test_anon_role_gets_nothing(env):
    for table in ("photo", "library", "person", "face", "profile"):
        with env.service_session() as s:
            s.execute(text("SET LOCAL ROLE anon"))
            with pytest.raises(DBAPIError, match="permission denied"):
                s.execute(text(f"SELECT 1 FROM {table}"))
            s.rollback()


def test_no_identity_means_no_rows(env):
    with env.service_session() as s:
        s.execute(text("SET LOCAL ROLE authenticated"))
        assert s.execute(text("SELECT 1 FROM photo")).all() == []
        assert s.execute(text("SELECT 1 FROM library")).all() == []
        with pytest.raises(DBAPIError, match="not authenticated"):
            s.execute(text("SELECT create_library('x')"))


# ------------------------------------------------------------------- ORM drift guard

def test_every_orm_table_is_queryable(env):
    from sqlmodel import SQLModel, select
    with env.service_session() as s:
        for table in SQLModel.metadata.sorted_tables:
            s.execute(select(table).limit(0))
