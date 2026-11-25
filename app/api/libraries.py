"""Libraries, members, and invites.

There are two ways in: the shareable join link with its six-character code (what the app uses), and the older
single-use invite tied to an email address.

Roles: owner (manages members, invites, library settings), editor (uploads, labels people; the default
for an invite), viewer (read-only). A library always keeps at least one owner (enforced by a database trigger).
Authorization is enforced twice: explicit checks here for clear errors, and Row Level Security
in the database as the backstop.
"""
import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlmodel import Session

from app.auth.deps import current_user, get_rls_session, require_role
from app.auth.jwt import AuthUser
from app.auth.profiles import ensure_profile
from app.config import INVITE_TTL_DAYS, app_base_url
from app.db.audit import audit, valid_ip
from app.services import join as joining

router = APIRouter()
invites_router = APIRouter()

Role = Literal["owner", "editor", "viewer"]
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class LibraryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class RoleChange(BaseModel):
    role: Role


class InviteCreate(BaseModel):
    email: str = Field(max_length=254)
    role: Role = "editor"  # families share and name photos together; owners can pick viewer for a guest

    @field_validator("email")
    @classmethod
    def _valid_email(cls, v: str) -> str:
        v = v.strip().lower()
        if not _EMAIL.match(v):
            raise ValueError("not a valid email address")
        return v


class InviteAccept(BaseModel):
    token: str = Field(min_length=20, max_length=200)


class JoinLinkSettings(BaseModel):
    role: Literal["editor", "viewer"] | None = None  # nobody becomes an owner through a shareable link


class JoinRequest(BaseModel):
    token: str | None = Field(default=None, min_length=20, max_length=200)
    code: str | None = Field(default=None, min_length=1, max_length=40)

    @model_validator(mode="after")
    def _exactly_one(self):
        if (self.token is None) == (self.code is None):
            raise ValueError("send either a token or a code")
        return self


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _db_error_to_http(e: DBAPIError) -> HTTPException:
    msg = getattr(getattr(e.orig, "diag", None), "message_primary", None) or str(e.orig)
    if "last_owner" in msg:
        return HTTPException(409, "A library must keep at least one owner")
    if "invite_invalid" in msg:
        return HTTPException(400, "This invite is invalid, expired, or already used")
    if "invite_email_mismatch" in msg:
        return HTTPException(403, "This invite was sent to a different email address")
    if "row-level security" in msg or "permission denied" in msg:
        return HTTPException(403, "Insufficient permissions")
    raise e


# ----------------------------------------------------------------------------- libraries

@router.post("", status_code=201)
def create_library(body: LibraryCreate, request: Request, user: AuthUser = Depends(current_user),
                   session: Session = Depends(get_rls_session)):
    lib_id = session.execute(text("SELECT create_library(:n)"), {"n": body.name.strip()}).scalar()
    session.commit()
    audit(user.id, "library.create", library_id=lib_id, ip=_ip(request))
    return {"id": lib_id, "name": body.name.strip(), "role": "owner"}


@router.get("")
def list_libraries(user: AuthUser = Depends(current_user), session: Session = Depends(get_rls_session)):
    rows = session.execute(
        text("SELECT l.id, l.name, m.role FROM library l "
             "JOIN library_member m ON m.library_id = l.id AND m.profile_id = :u ORDER BY l.name"),
        {"u": user.id},
    ).all()
    return [{"id": r.id, "name": r.name, "role": r.role} for r in rows]


@router.patch("/{library_id}")
def rename_library(library_id: UUID, body: LibraryCreate, user: AuthUser = Depends(current_user),
                   session: Session = Depends(get_rls_session)):
    require_role(session, user.id, library_id, ("owner",))
    session.execute(text("UPDATE library SET name = :n WHERE id = :l"),
                    {"n": body.name.strip(), "l": library_id})
    session.commit()
    return {"id": library_id, "name": body.name.strip()}


# ------------------------------------------------------------------------------ members

@router.get("/{library_id}/members")
def list_members(library_id: UUID, user: AuthUser = Depends(current_user),
                 session: Session = Depends(get_rls_session)):
    require_role(session, user.id, library_id, ("owner", "editor", "viewer"))
    rows = session.execute(
        text("SELECT m.profile_id, p.handle, p.display_name, m.role, m.joined_at FROM library_member m "
             "JOIN profile p ON p.id = m.profile_id WHERE m.library_id = :l "
             "ORDER BY m.role, p.display_name"),
        {"l": library_id},
    ).all()
    return [{"profile_id": r.profile_id, "handle": r.handle, "display_name": r.display_name,
             "role": r.role, "joined_at": r.joined_at} for r in rows]


@router.patch("/{library_id}/members/{profile_id}")
def change_member_role(library_id: UUID, profile_id: UUID, body: RoleChange, request: Request,
                       user: AuthUser = Depends(current_user),
                       session: Session = Depends(get_rls_session)):
    require_role(session, user.id, library_id, ("owner",))
    try:
        res = session.execute(
            text("UPDATE library_member SET role = :r WHERE library_id = :l AND profile_id = :p"),
            {"r": body.role, "l": library_id, "p": profile_id},
        )
        session.commit()
    except DBAPIError as e:
        session.rollback()
        raise _db_error_to_http(e) from None
    if res.rowcount == 0:
        raise HTTPException(404, "Member not found")
    audit(user.id, "member.role_change", library_id=library_id, target=str(profile_id),
          ip=_ip(request), meta={"role": body.role})
    return {"profile_id": profile_id, "role": body.role}


@router.delete("/{library_id}/members/{profile_id}", status_code=204)
def remove_member(library_id: UUID, profile_id: UUID, request: Request,
                  user: AuthUser = Depends(current_user), session: Session = Depends(get_rls_session)):
    """Owners can remove anyone; any member can remove themselves (leave).

    Removing someone else also stops the library's invite link and code (a database trigger), so they cannot rejoin
    with it. The next time an owner opens the invite screen they get a new one.
    """
    role = require_role(session, user.id, library_id, ("owner", "editor", "viewer"))
    if role != "owner" and profile_id != user.id:
        raise HTTPException(403, "Insufficient permissions")
    try:
        res = session.execute(
            text("DELETE FROM library_member WHERE library_id = :l AND profile_id = :p"),
            {"l": library_id, "p": profile_id},
        )
        session.commit()
    except DBAPIError as e:
        session.rollback()
        raise _db_error_to_http(e) from None
    if res.rowcount == 0:
        raise HTTPException(404, "Member not found")
    audit(user.id, "member.leave" if profile_id == user.id else "member.remove",
          library_id=library_id, target=str(profile_id), ip=_ip(request))


# ------------------------------------------------------------------------------ invites

@router.post("/{library_id}/invites", status_code=201)
def create_invite(library_id: UUID, body: InviteCreate, request: Request,
                  user: AuthUser = Depends(current_user), session: Session = Depends(get_rls_session)):
    """Create an invite link. The token is returned once and only its hash is stored."""
    require_role(session, user.id, library_id, ("owner",))
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    expires_at = datetime.now(timezone.utc) + timedelta(days=INVITE_TTL_DAYS)
    try:
        # Re-inviting the same email replaces the previous open invite.
        session.execute(
            text("UPDATE library_invite SET revoked_at = now() WHERE library_id = :l AND email = :e "
                 "AND accepted_at IS NULL AND revoked_at IS NULL"),
            {"l": library_id, "e": body.email},
        )
        invite_id = session.execute(
            text("INSERT INTO library_invite (library_id, email, role, token_hash, created_by, expires_at) "
                 "VALUES (:l, :e, :r, :h, :u, :x) RETURNING id"),
            {"l": library_id, "e": body.email, "r": body.role, "h": token_hash, "u": user.id,
             "x": expires_at},
        ).scalar()
        session.commit()
    except DBAPIError as e:
        session.rollback()
        raise _db_error_to_http(e) from None
    audit(user.id, "invite.create", library_id=library_id, target=str(invite_id), ip=_ip(request),
          meta={"role": body.role})
    return {
        "id": invite_id,
        "email": body.email,
        "role": body.role,
        "expires_at": expires_at,
        "token": token,
        "link": f"{app_base_url()}/invite?token={token}",
    }


@router.get("/{library_id}/invites")
def list_invites(library_id: UUID, user: AuthUser = Depends(current_user),
                 session: Session = Depends(get_rls_session)):
    require_role(session, user.id, library_id, ("owner",))
    rows = session.execute(
        text("SELECT id, email, role, created_at, expires_at FROM library_invite "
             "WHERE library_id = :l AND accepted_at IS NULL AND revoked_at IS NULL AND expires_at > now() "
             "ORDER BY created_at DESC"),
        {"l": library_id},
    ).all()
    return [{"id": r.id, "email": r.email, "role": r.role, "created_at": r.created_at,
             "expires_at": r.expires_at} for r in rows]


@router.delete("/{library_id}/invites/{invite_id}", status_code=204)
def revoke_invite(library_id: UUID, invite_id: UUID, request: Request,
                  user: AuthUser = Depends(current_user), session: Session = Depends(get_rls_session)):
    require_role(session, user.id, library_id, ("owner",))
    res = session.execute(
        text("UPDATE library_invite SET revoked_at = now() WHERE id = :i AND library_id = :l "
             "AND accepted_at IS NULL AND revoked_at IS NULL"),
        {"i": invite_id, "l": library_id},
    )
    session.commit()
    if res.rowcount == 0:
        raise HTTPException(404, "Invite not found")
    audit(user.id, "invite.revoke", library_id=library_id, target=str(invite_id), ip=_ip(request))


# ------------------------------------------------------------- join link and code

def _link_body(row) -> dict:
    # The token goes after '#', which browsers never send to a server, so it stays out of access logs.
    return {"role": row.role, "code": joining.format_code(row.code),
            "link": f"{app_base_url()}/join#{row.token}", "expires_at": row.expires_at}


@router.get("/{library_id}/join-link")
def get_join_link(library_id: UUID, user: AuthUser = Depends(current_user),
                  session: Session = Depends(get_rls_session)):
    require_role(session, user.id, library_id, ("owner",))
    row = joining.active_link(session, library_id)
    if row is None:
        raise HTTPException(404, "There is no active invite link")
    return _link_body(row)


@router.put("/{library_id}/join-link")
def put_join_link(library_id: UUID, body: JoinLinkSettings, request: Request,
                  user: AuthUser = Depends(current_user), session: Session = Depends(get_rls_session)):
    """Show the invite link and code, creating them if needed, and optionally set what joiners can do."""
    require_role(session, user.id, library_id, ("owner",))
    row = joining.save_link(library_id, user.id, body.role)
    audit(user.id, "join_link.save", library_id=library_id, ip=_ip(request), meta={"role": row.role})
    return _link_body(row)


@router.post("/{library_id}/join-link/reset")
def reset_join_link(library_id: UUID, request: Request, user: AuthUser = Depends(current_user),
                    session: Session = Depends(get_rls_session)):
    """Replace the link and code. The old ones stop working at once."""
    require_role(session, user.id, library_id, ("owner",))
    row = joining.save_link(library_id, user.id, reset=True)
    audit(user.id, "join_link.reset", library_id=library_id, ip=_ip(request))
    return _link_body(row)


@router.delete("/{library_id}/join-link", status_code=204)
def delete_join_link(library_id: UUID, request: Request, user: AuthUser = Depends(current_user),
                     session: Session = Depends(get_rls_session)):
    require_role(session, user.id, library_id, ("owner",))
    session.execute(text("DELETE FROM library_join_link WHERE library_id = :l"), {"l": library_id})
    session.commit()
    audit(user.id, "join_link.delete", library_id=library_id, ip=_ip(request))


@invites_router.post("/join")
def join_library(body: JoinRequest, request: Request, user: AuthUser = Depends(current_user)):
    """Join with an invite link or a six-character code."""
    ensure_profile(user)
    try:
        joined = joining.join(user.id, token=body.token, code=body.code, ip=valid_ip(_ip(request)))
    except joining.TooManyAttempts:
        raise HTTPException(429, "Too many tries. Wait a few minutes, then try again.") from None
    except joining.InvalidInvite:
        detail = ("This invite no longer works. Ask for a new link." if body.code is None
                  else "That code didn't work. Check the letters and try again.")
        raise HTTPException(400, detail) from None
    if not joined.already_member:
        audit(user.id, "member.join", library_id=joined.library_id, ip=_ip(request), meta={"role": joined.role})
    return {"library_id": joined.library_id, "name": joined.name, "role": joined.role,
            "already_member": joined.already_member}


@invites_router.post("/accept")
def accept_invite(body: InviteAccept, request: Request, user: AuthUser = Depends(current_user),
                  session: Session = Depends(get_rls_session)):
    """Join a library with an invite token. Requires being signed in with the invited email."""
    token_hash = hashlib.sha256(body.token.encode()).hexdigest()
    try:
        library_id = session.execute(text("SELECT accept_invite(:h)"), {"h": token_hash}).scalar()
        session.commit()
    except DBAPIError as e:
        session.rollback()
        raise _db_error_to_http(e) from None
    audit(user.id, "invite.accept", library_id=library_id, ip=_ip(request))
    return {"library_id": library_id}
