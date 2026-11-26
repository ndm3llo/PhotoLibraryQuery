"""Database connections are reused, so one request's identity must never carry over into the next."""
import uuid

import pytest
from sqlalchemy import text

WHO = text("SELECT pg_backend_pid(), current_user, "
           "coalesce(current_setting('request.jwt.claims', true), ''), "
           "coalesce(current_setting('request.jwt.claim.sub', true), '')")


def who(session):
    return tuple(session.execute(WHO).one())


def test_connections_are_reused_and_come_back_clean(app_db):
    from app.db.rls import rls_session, service_session
    from app.db.session import engine
    engine.dispose()  # start from an empty pool, so there is exactly one connection to hand around
    user = uuid.uuid4()

    with rls_session(user) as s:
        pid, role, claims, sub = who(s)
        assert role == "authenticated" and str(user) in claims and sub == str(user)
        s.commit()
        assert who(s)[1:] == (role, claims, sub), "still the same person after a commit"
    with service_session() as s:
        assert who(s) == (pid, "postgres", "", ""), "same connection, with nothing left over"

    with pytest.raises(RuntimeError):
        with rls_session(user) as s:
            who(s)
            raise RuntimeError("request failed")
    with service_session() as s:
        assert who(s) == (pid, "postgres", "", "")


def test_a_connection_that_died_while_idle_is_replaced(app_db, monkeypatch):
    import app.db.session as db
    db.engine.dispose()
    with db.Session(db.engine) as s:
        first = s.execute(text("SELECT pg_backend_pid()")).scalar()
    admin = app_db.engine()
    with admin.connect() as c:
        c.execute(text("SELECT pg_terminate_backend(:p)"), {"p": first})
        c.commit()
    admin.dispose()
    monkeypatch.setattr(db, "IDLE_CHECK_SECONDS", 0)
    with db.Session(db.engine) as s:
        assert s.execute(text("SELECT pg_backend_pid()")).scalar() != first
