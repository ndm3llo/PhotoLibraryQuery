"""Create a profile row the first time a signed-in user is seen."""
import re
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.auth.jwt import AuthUser
from app.db.rls import service_session

_ensured: set[UUID] = set()  # process-local cache so we do not write on every request


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "user"


def ensure_profile(user: AuthUser) -> None:
    if user.id in _ensured:
        return
    meta = user.claims.get("user_metadata") or {}
    local = (user.email or "").split("@")[0]
    display_name = (meta.get("full_name") or meta.get("name") or local or "New member")[:100]
    base = _slug(local or display_name)
    hex_id = user.id.hex

    # The handle must be unique; retry with a longer id suffix on the (unlikely) collision.
    for suffix_len in (6, 12, 32):
        handle = f"{base}-{hex_id[:suffix_len]}"
        try:
            with service_session() as s:
                s.execute(
                    text("INSERT INTO profile (id, handle, display_name) VALUES (:id, :h, :n) "
                         "ON CONFLICT (id) DO NOTHING"),
                    {"id": user.id, "h": handle, "n": display_name},
                )
            _ensured.add(user.id)
            return
        except IntegrityError:
            continue
    raise RuntimeError("could not allocate a unique handle")
