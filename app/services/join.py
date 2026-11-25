"""Join a library with its shareable link, or with the six-character code as a backup.

Everything here runs with the service role, so callers MUST check that the user may do it first: only owners
create, change or reset a link. `join` needs only a signed-in user, because holding the link or code is the
permission.
"""
import re
import secrets
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.config import (
    JOIN_ATTEMPT_WINDOW_MINUTES,
    JOIN_ATTEMPTS_PER_IP,
    JOIN_ATTEMPTS_PER_USER,
    JOIN_LINK_TTL_DAYS,
)
from app.db.rls import service_session

# No 0/O or 1/I, which look alike when read aloud or copied by hand. Must match the check in migration 0009.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 6
# Spaces and any kind of dash (phone keyboards like to swap a hyphen for a longer one).
_CODE_FILLER = re.compile(r"[\s\-‐-―−]+")
_COLUMNS = "library_id, token, code, role, expires_at"


class InvalidInvite(Exception):
    """The link or code is wrong, expired or has been reset."""


class TooManyAttempts(Exception):
    """Too many wrong codes recently; try again later."""


@dataclass(frozen=True)
class Joined:
    library_id: UUID
    name: str
    role: str
    already_member: bool


def new_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def normalize_code(raw: str) -> str:
    """'k7m 4px' and 'K7M-4PX' are the same code."""
    return _CODE_FILLER.sub("", raw).upper()


def format_code(code: str) -> str:
    return f"{code[:3]}-{code[3:]}"


def active_link(session: Session, library_id: UUID):
    return session.execute(
        text(f"SELECT {_COLUMNS} FROM library_join_link WHERE library_id = :l AND expires_at > now()"),
        {"l": library_id},
    ).first()


def save_link(library_id: UUID, user_id: UUID, role: str | None = None, *, reset: bool = False):
    """Return the library's active link, creating one if needed.

    `role` changes what new members can do without changing the link (None keeps it; a new link defaults to
    editor). `reset` replaces the link and code so the old ones stop working.
    """
    for attempt in range(5):
        try:
            with service_session() as s:
                # One writer per library at a time, so two taps cannot hand out two different links.
                s.execute(text("SELECT id FROM library WHERE id = :l FOR NO KEY UPDATE"), {"l": library_id})
                current = active_link(s, library_id)
                if current is not None and not reset:
                    if role is not None and role != current.role:
                        s.execute(text("UPDATE library_join_link SET role = :r WHERE library_id = :l"),
                                  {"r": role, "l": library_id})
                else:
                    # An expired link still remembers what joiners may do, so replacing it never widens access.
                    previous = s.execute(text("SELECT role FROM library_join_link WHERE library_id = :l"),
                                         {"l": library_id}).scalar()
                    keep = previous or "editor"
                    s.execute(
                        text("INSERT INTO library_join_link (library_id, token, code, role, created_by, expires_at) "
                             "VALUES (:l, :t, :c, :r, :u, now() + make_interval(days => :d)) "
                             "ON CONFLICT (library_id) DO UPDATE SET token = EXCLUDED.token, code = EXCLUDED.code, "
                             "role = EXCLUDED.role, created_by = EXCLUDED.created_by, created_at = now(), "
                             "expires_at = EXCLUDED.expires_at"),
                        {"l": library_id, "t": secrets.token_urlsafe(32), "c": new_code(), "r": role or keep,
                         "u": user_id, "d": JOIN_LINK_TTL_DAYS},
                    )
                return active_link(s, library_id)
        except IntegrityError:
            # Another library already has this code (very unlikely): draw again.
            if attempt == 4:
                raise


def _limit_reached(s: Session, user_id: UUID, ip: str | None) -> bool:
    """Count recent failures. Locks first, so parallel guesses cannot all slip under the limit."""
    keys = [f"join:user:{user_id}"] + ([f"join:ip:{ip}"] if ip else [])
    for key in sorted(keys):  # a fixed order, so two requests cannot deadlock
        s.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))"), {"k": key})
    s.execute(text("DELETE FROM join_attempt WHERE at < now() - interval '1 day'"))
    recent = "SELECT count(*) FROM join_attempt WHERE at > now() - make_interval(mins => :m) AND "
    window = JOIN_ATTEMPT_WINDOW_MINUTES
    if s.execute(text(recent + "profile_id = :u"), {"m": window, "u": user_id}).scalar() >= JOIN_ATTEMPTS_PER_USER:
        return True
    return bool(ip) and s.execute(
        text(recent + "ip = CAST(:ip AS inet)"), {"m": window, "ip": ip}
    ).scalar() >= JOIN_ATTEMPTS_PER_IP


def join(user_id: UUID, *, token: str | None = None, code: str | None = None, ip: str | None = None) -> Joined:
    """Add the user to the library behind a link token or a code (exactly one of them).

    Only codes are rate limited: a link token is far too long to guess, so a working link keeps working even for
    someone who has just mistyped a code too many times. Someone who is already a member keeps their role.
    """
    if (token is None) == (code is None):
        raise ValueError("give a token or a code")
    result: Joined | None = None
    limited = False
    with service_session() as s:
        if code is not None:
            limited = _limit_reached(s, user_id, ip)
        if not limited:
            column, value = ("token", token) if code is None else ("code", normalize_code(code))
            # FOR SHARE: a reset that is happening right now finishes first, or waits for this join.
            link = s.execute(
                text(f"SELECT library_id, role FROM library_join_link WHERE {column} = :v AND expires_at > now() "
                     "FOR SHARE"),
                {"v": value},
            ).first()
            if link is None:
                if code is not None:
                    # Recorded inside this transaction; the error is raised after it commits.
                    s.execute(text("INSERT INTO join_attempt (profile_id, ip) VALUES (:u, CAST(:ip AS inet))"),
                              {"u": user_id, "ip": ip})
            else:
                added = s.execute(
                    text("INSERT INTO library_member (library_id, profile_id, role) VALUES (:l, :u, :r) "
                         "ON CONFLICT (library_id, profile_id) DO NOTHING RETURNING role"),
                    {"l": link.library_id, "u": user_id, "r": link.role},
                ).first()
                row = s.execute(
                    text("SELECT l.name, m.role FROM library l JOIN library_member m ON m.library_id = l.id "
                         "WHERE l.id = :l AND m.profile_id = :u"),
                    {"l": link.library_id, "u": user_id},
                ).one()
                result = Joined(link.library_id, row.name, row.role, already_member=added is None)
    if limited:
        raise TooManyAttempts
    if result is None:
        raise InvalidInvite
    return result
