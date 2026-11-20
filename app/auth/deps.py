"""FastAPI dependencies for authenticated, RLS-scoped database access.

Phase 1 wires the plumbing. `current_user_id` deliberately rejects every request until
Phase 2 adds Supabase JWT verification, so nothing can reach tenant data unauthenticated
by accident.
"""
from typing import Iterator
from uuid import UUID

from fastapi import Depends, HTTPException
from sqlmodel import Session

from app.db.rls import rls_session


def current_user_id() -> UUID:
    raise HTTPException(status_code=401, detail="Authentication is not implemented yet (Phase 2).")


def get_rls_session(user_id: UUID = Depends(current_user_id)) -> Iterator[Session]:
    with rls_session(user_id) as session:
        yield session
