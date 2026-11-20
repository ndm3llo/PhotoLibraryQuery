import os

from dotenv import dotenv_values, find_dotenv, load_dotenv
from sqlalchemy.pool import NullPool
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

engine = create_engine(database_url, poolclass=NullPool)


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
