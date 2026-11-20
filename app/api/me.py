from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlmodel import Session

from app.auth.deps import current_user, get_rls_session
from app.auth.jwt import AuthUser

router = APIRouter()


class ProfileUpdate(BaseModel):
    display_name: str = Field(min_length=1, max_length=100)


def _me(session: Session, user: AuthUser) -> dict:
    profile = session.execute(
        text("SELECT id, handle, display_name FROM profile WHERE id = :u"), {"u": user.id}
    ).one()
    libraries = session.execute(
        text("SELECT l.id, l.name, m.role FROM library l "
             "JOIN library_member m ON m.library_id = l.id AND m.profile_id = :u ORDER BY l.name"),
        {"u": user.id},
    ).all()
    return {
        "id": profile.id,
        "handle": profile.handle,
        "display_name": profile.display_name,
        "email": user.email,
        "libraries": [{"id": r.id, "name": r.name, "role": r.role} for r in libraries],
    }


@router.get("")
def get_me(user: AuthUser = Depends(current_user), session: Session = Depends(get_rls_session)):
    """Who am I? Also creates my profile the first time I sign in."""
    return _me(session, user)


@router.patch("")
def update_me(body: ProfileUpdate, user: AuthUser = Depends(current_user),
              session: Session = Depends(get_rls_session)):
    session.execute(text("UPDATE profile SET display_name = :n WHERE id = :u"),
                    {"n": body.display_name.strip(), "u": user.id})
    session.commit()
    return _me(session, user)

