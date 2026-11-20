"""Run database work as the signed-in user so Postgres Row Level Security applies.

The engine connects as the table owner (which bypasses RLS). For request-scoped work we open
a transaction, drop to the `authenticated` role, and publish the caller's JWT claims where
`auth.uid()` reads them. Both settings are transaction-local (`SET LOCAL` / `set_config(..., true)`),
so they cannot leak to another request on a pooled connection.

Use `service_session()` only for the worker and admin scripts; it bypasses RLS and every
query through it must set `library_id` explicitly.
"""
import json
from contextlib import contextmanager
from typing import Iterator
from uuid import UUID

from sqlalchemy import text
from sqlmodel import Session

from app.db.session import engine


@contextmanager
def rls_session(user_id: UUID, extra_claims: dict | None = None) -> Iterator[Session]:
    claims = {"sub": str(user_id), "role": "authenticated", **(extra_claims or {})}
    session = Session(engine)
    try:
        session.exec(text("SET LOCAL ROLE authenticated"))
        session.exec(
            text("SELECT set_config('request.jwt.claims', :claims, true), "
                 "set_config('request.jwt.claim.sub', :sub, true)"),
            params={"claims": json.dumps(claims), "sub": str(user_id)},
        )
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
