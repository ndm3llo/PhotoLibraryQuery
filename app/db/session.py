import os

from dotenv import load_dotenv
from sqlalchemy.pool import NullPool
from sqlmodel import Session, create_engine, text

# Import all models so SQLModel's registry knows about them (used by ORM queries).
from app.models.model import (  # noqa: F401
    Album,
    AlbumPhoto,
    BiometricConsent,
    FaceCluster,
    FaceCredential,
    Library,
    LibraryMember,
    Person,
    Photo,
    PhotoFace,
    Profile,
)

load_dotenv()

# Use the 'Transaction' connection string from Supabase for serverless/FastAPI.
# DATABASE_URL (if set) wins; used by tests and local docker.
database_url = os.getenv("DATABASE_URL") or os.getenv("postgres_url")

engine = create_engine(database_url, poolclass=NullPool)


def init_db():
    """Connectivity check only. The schema is managed by Alembic: `alembic upgrade head`."""
    with Session(engine) as session:
        session.exec(text("SELECT 1"))
    print("Database connection OK. (Schema is managed by Alembic.)")
