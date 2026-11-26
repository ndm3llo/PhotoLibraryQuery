import os
import time

from dotenv import dotenv_values, find_dotenv, load_dotenv
from sqlalchemy import event, exc
from sqlmodel import Session, create_engine, text

# Import all models so SQLModel's registry knows about them (used by ORM queries).
from app.models.model import (  # noqa: F401
    Album,
    AlbumPhoto,
    BiometricConsent,
    FaceCluster,
    Library,
    LibraryInvite,
    LibraryMember,
    Person,
    Photo,
    PhotoFace,
    Profile,
)


def _warn_if_shell_overrides_dotenv() -> None:
    """load_dotenv() never overrides variables already set in the shell, so a stale exported
    value silently beats .env. Say so (names only, never values)."""
    path = find_dotenv()
    file_values = dotenv_values(path) if path else {}
    for name in ("DATABASE_URL", "postgres_url", "MIGRATION_DATABASE_URL"):
        shell_value, file_value = os.environ.get(name), file_values.get(name)
        if shell_value and file_value and shell_value != file_value:
            print(f"WARNING: {name} is set in your shell and differs from .env. The SHELL value is being used. "
                  f"Run `unset {name}` (or open a new terminal) to use .env.")


_warn_if_shell_overrides_dotenv()
load_dotenv()

# Use the 'Transaction' connection string from Supabase for serverless/FastAPI.
# DATABASE_URL (if set) wins; used by tests and local docker.
database_url = os.getenv("DATABASE_URL") or os.getenv("postgres_url")

if not database_url:
    raise RuntimeError("Set DATABASE_URL or postgres_url")

# Connections are kept open and reused: opening one to a hosted database takes most of a second. This works with
# Supabase's transaction pooler because identity is only ever set for one transaction (app/db/rls.py).
engine = create_engine(database_url, pool_size=5, max_overflow=10, pool_recycle=300)

# A connection that sat unused may have been closed by the server or the network. Test those before use, and only
# those: testing every time would add a network round trip to every request.
IDLE_CHECK_SECONDS = 30


@event.listens_for(engine, "checkin")
def _note_idle_start(_connection, record):
    record.info["idle_since"] = time.monotonic()


@event.listens_for(engine, "checkout")
def _replace_dead_connection(connection, record, _proxy):
    idle_since = record.info.get("idle_since")
    if idle_since is None or time.monotonic() - idle_since < IDLE_CHECK_SECONDS:
        return
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        connection.rollback()
    except Exception as e:
        raise exc.DisconnectionError() from e  # the pool opens a fresh one and carries on


def init_db():
    """Connectivity check only. The schema is managed by Alembic: `alembic upgrade head`."""
    try:
        with Session(engine) as session:
            session.exec(text("SELECT 1"))
    except Exception as e:
        url = engine.url
        raise RuntimeError(
            f"Cannot connect to the database as user '{url.username}' at {url.host}:{url.port} "
            f"({type(e).__name__}). Check the password in .env, and make sure no stale "
            "DATABASE_URL / postgres_url is exported in this shell (`unset` them)."
        ) from None
    print("Database connection OK. (Schema is managed by Alembic.)")
