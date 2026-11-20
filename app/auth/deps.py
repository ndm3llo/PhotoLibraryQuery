"""FastAPI dependencies for authenticated, RLS-scoped database access.

Every route that touches tenant data should depend on `get_rls_session` (or `current_user`).
The user id always comes from the verified token, never from the request body or query.
"""
from typing import Iterator
from uuid import UUID

from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text
from sqlmodel import Session

from app.auth.jwt import AuthError, AuthUser, verify_token
from app.auth.profiles import ensure_profile
from app.db.rls import rls_session

_bearer = HTTPBearer(auto_error=False)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status_code=401, detail=detail, headers={"WWW-Authenticate": "Bearer"})


def current_user(creds: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> AuthUser:
    if creds is None or creds.scheme.lower() != "bearer" or not creds.credentials:
        raise _unauthorized("Not authenticated")
    try:
        return verify_token(creds.credentials)
    except AuthError:
        raise _unauthorized("Invalid or expired token") from None


def current_user_id(user: AuthUser = Depends(current_user)) -> UUID:
    return user.id


def get_rls_session(user: AuthUser = Depends(current_user)) -> Iterator[Session]:
    ensure_profile(user)
    with rls_session(user.id, {"email": user.email}) as session:
        yield session


def my_role(session: Session, user_id: UUID, library_id: UUID) -> str | None:
    return session.execute(
        text("SELECT role FROM library_member WHERE library_id = :l AND profile_id = :u"),
        {"l": library_id, "u": user_id},
    ).scalar()


def require_role(session: Session, user_id: UUID, library_id: UUID, roles: tuple[str, ...]) -> str:
    """404 if the caller is not a member (do not reveal that the library exists), 403 if the role is too low."""
    role = my_role(session, user_id, library_id)
    if role is None:
        raise HTTPException(status_code=404, detail="Library not found")
    if role not in roles:
        raise HTTPException(status_code=403, detail="Insufficient permissions")
    return role


def resolve_library(session: Session, user_id: UUID, library_id: UUID | None,
                    roles: tuple[str, ...] = ("owner", "editor", "viewer")) -> UUID:
    """Use the given library, or the caller's only library if they belong to exactly one."""
    if library_id is not None:
        require_role(session, user_id, library_id, roles)
        return library_id
    rows = session.execute(
        text("SELECT library_id FROM library_member WHERE profile_id = :u AND role = ANY(:r)"),
        {"u": user_id, "r": list(roles)},
    ).all()
    if len(rows) == 1:
        return rows[0][0]
    if not rows:
        raise HTTPException(status_code=403, detail="You do not have access to any library for this action")
    raise HTTPException(status_code=400, detail="library_id is required: you belong to several libraries")
