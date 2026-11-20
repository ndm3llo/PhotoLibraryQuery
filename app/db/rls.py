"""Run database work as the signed-in user so Postgres Row Level Security applies.

The engine connects as the table owner (which bypasses RLS). For request-scoped work we switch
each transaction to the `authenticated` role and publish the caller's verified JWT claims where
`auth.uid()` reads them. This is re-applied by an `after_begin` hook on EVERY transaction the
session starts, because `SET LOCAL` ends with the transaction: without the hook, anything after a
mid-request `commit()` would silently run with full owner privileges.

Use `service_session()` only for the worker and admin scripts; it bypasses RLS and every query
through it must set `library_id` explicitly.
"""
import json
from contextlib import contextmanager
from typing import Iterator
from uuid import UUID

from sqlalchemy import event, text
from sqlmodel import Session

from app.db.session import engine


@contextmanager
def rls_session(user_id: UUID, extra_claims: dict | None = None) -> Iterator[Session]:
    claims = {"sub": str(user_id), "role": "authenticated", **(extra_claims or {})}
    payload = json.dumps(claims)
    session = Session(engine)

    @event.listens_for(session, "after_begin")
    def _apply_identity(_session, _transaction, connection):
        connection.exec_driver_sql("SET LOCAL ROLE authenticated")
        connection.execute(
            text("SELECT set_config('request.jwt.claims', :claims, true), "
                 "set_config('request.jwt.claim.sub', :sub, true)"),
            {"claims": payload, "sub": str(user_id)},
        )

    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def service_session() -> Iterator[Session]:
    """Bypasses RLS. Worker/admin only."""
    with Session(engine) as session:
        yield session
        session.commit()
