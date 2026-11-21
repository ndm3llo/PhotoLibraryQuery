"""ORM models.

The database schema is owned by Alembic (migrations/). These classes are for querying only;
never call SQLModel.metadata.create_all. Columns marked LEGACY are still read by the
prototype endpoints and are dropped in later phases (see migrations/versions/).
"""
from datetime import date, datetime
from typing import Any, List, Optional
from uuid import UUID, uuid4

from geoalchemy2 import Geography
from pgvector.sqlalchemy import Vector
from sqlalchemy import ARRAY, Integer
from sqlmodel import Column, Field, SQLModel

# --- Identity & tenancy ---


class Profile(SQLModel, table=True):
    """A signed-in account. Matches the Supabase Auth user id."""

    id: UUID = Field(primary_key=True)
    handle: str = Field(unique=True)
    display_name: str = Field(index=True)
    avatar_url: Optional[str] = None
    created_at: Optional[datetime] = None


class Library(SQLModel, table=True):
    """One per family. The isolation unit for every query."""

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    name: str
    created_by: UUID = Field(foreign_key="profile.id")
    created_at: Optional[datetime] = None


class LibraryMember(SQLModel, table=True):
    __tablename__ = "library_member"

    library_id: UUID = Field(foreign_key="library.id", primary_key=True)
    profile_id: UUID = Field(foreign_key="profile.id", primary_key=True)
    role: str  # 'owner' | 'editor' | 'viewer'


class Person(SQLModel, table=True):
    """A named human in the family. Does not need an account."""

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    library_id: UUID = Field(foreign_key="library.id", index=True)
    display_name: str
    birth_date: Optional[date] = None
    death_date: Optional[date] = None
    profile_id: Optional[UUID] = Field(default=None, foreign_key="profile.id")
    cover_face_id: Optional[UUID] = None
    created_by: Optional[UUID] = Field(default=None, foreign_key="profile.id")
    created_at: Optional[datetime] = None


# --- Albums ---


class Album(SQLModel, table=True):
    id: UUID = Field(default_factory=uuid4, primary_key=True)
    name: str = Field(index=True)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    created_by: UUID = Field(foreign_key="profile.id")
    library_id: Optional[UUID] = Field(default=None, foreign_key="library.id")


class AlbumPhoto(SQLModel, table=True):
    __tablename__ = "album_photo"

    album_id: UUID = Field(foreign_key="album.id", primary_key=True)
    photo_id: UUID = Field(foreign_key="photo.id", primary_key=True)


# --- Photos ---


class Photo(SQLModel, table=True):
    id: UUID = Field(default_factory=uuid4, primary_key=True)

    # Tenancy
    library_id: UUID = Field(foreign_key="library.id")
    uploader_id: UUID = Field(foreign_key="profile.id")

    # Processing state: pending (awaiting upload) | processing | ready | failed | duplicate
    status: str = Field(default="pending")
    error: Optional[str] = None
    enriched_at: Optional[datetime] = None
    enrich_error: Optional[str] = None

    # Storage: object keys, never URLs. Bucket is private; clients get short-lived signed URLs.
    original_key: Optional[str] = None
    display_key: Optional[str] = None
    thumb_key: Optional[str] = None
    content_type: Optional[str] = None
    original_filename: Optional[str] = None
    byte_size: Optional[int] = None
    width: Optional[int] = None
    height: Optional[int] = None
    content_sha256: Optional[str] = None
    phash: Optional[int] = None
    batch_id: Optional[UUID] = None
    client_last_modified: Optional[datetime] = None
    duplicate_of: Optional[UUID] = Field(default=None, foreign_key="photo.id")
    near_duplicate_of: Optional[UUID] = Field(default=None, foreign_key="photo.id")

    # Time, with provenance
    taken_at: Optional[datetime] = None
    taken_at_source: Optional[str] = None
    taken_at_confidence: Optional[float] = None
    taken_tz: Optional[str] = None

    location: Optional[Any] = Field(
        default=None, sa_column=Column(Geography(geometry_type="POINT", srid=4326))
    )
    clip_embedding: Optional[List[float]] = Field(default=None, sa_column=Column(Vector(512)))

    # LEGACY (dropped in Phase 4 with album_photo)
    album_id: Optional[UUID] = Field(default=None, foreign_key="album.id", index=True)

    created_at: Optional[datetime] = None


# --- Faces ---


class FaceCluster(SQLModel, table=True):
    __tablename__ = "face_cluster"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    library_id: UUID = Field(foreign_key="library.id", index=True)
    person_id: Optional[UUID] = Field(default=None, foreign_key="person.id")


class PhotoFace(SQLModel, table=True):
    """A face found in a photo. Table is `face`; class name kept for the legacy endpoints."""

    __tablename__ = "face"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    photo_id: UUID = Field(foreign_key="photo.id", index=True)
    library_id: Optional[UUID] = Field(default=None, foreign_key="library.id")

    face_embedding: List[float] = Field(sa_column=Column(Vector(512), nullable=False))  # -> embedding (Phase 4)
    bbox: Optional[List[int]] = Field(default=None, sa_column=Column(ARRAY(Integer)))
    det_score: Optional[float] = None
    quality: Optional[float] = None
    model_version: str = "facenet512"

    cluster_id: Optional[UUID] = Field(default=None, foreign_key="face_cluster.id")
    person_id: Optional[UUID] = Field(default=None, foreign_key="person.id", index=True)
    person_source: Optional[str] = None  # manual | confirmed | auto
    person_confidence: Optional[float] = None

    # LEGACY: dropped in Phase 4
    album_id: Optional[UUID] = Field(default=None, foreign_key="album.id", index=True)


# --- Consent and invites ---


class LibraryInvite(SQLModel, table=True):
    __tablename__ = "library_invite"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    library_id: UUID = Field(foreign_key="library.id")
    email: str
    role: str
    token_hash: str
    created_by: UUID = Field(foreign_key="profile.id")
    created_at: Optional[datetime] = None
    expires_at: datetime
    accepted_at: Optional[datetime] = None
    accepted_by: Optional[UUID] = Field(default=None, foreign_key="profile.id")
    revoked_at: Optional[datetime] = None


class BiometricConsent(SQLModel, table=True):
    __tablename__ = "biometric_consent"

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    profile_id: UUID = Field(foreign_key="profile.id")
    notice_version: str
    granted_at: datetime
    revoked_at: Optional[datetime] = None
