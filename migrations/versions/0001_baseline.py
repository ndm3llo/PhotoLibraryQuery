"""Baseline: the prototype schema exactly as SQLModel's create_all produced it.

An existing database that already has these tables must be marked, not re-created:

    alembic stamp 0001
    alembic upgrade head

Revision ID: 0001
Revises:
"""
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")

    op.execute("""
        CREATE TABLE profile (
            id uuid PRIMARY KEY,
            full_name varchar NOT NULL,
            avatar_url varchar,
            face_embedding vector(512)
        )""")
    op.execute("CREATE INDEX ix_profile_full_name ON profile (full_name)")

    op.execute("""
        CREATE TABLE album (
            id uuid PRIMARY KEY,
            name varchar NOT NULL,
            created_at timestamp NOT NULL,
            created_by uuid NOT NULL REFERENCES profile (id)
        )""")
    op.execute("CREATE INDEX ix_album_name ON album (name)")

    op.execute("""
        CREATE TABLE albummemberlink (
            album_id uuid NOT NULL REFERENCES album (id),
            user_id uuid NOT NULL REFERENCES profile (id),
            role varchar NOT NULL,
            PRIMARY KEY (album_id, user_id)
        )""")

    op.execute("""
        CREATE TABLE photo (
            id uuid PRIMARY KEY,
            user_id uuid NOT NULL REFERENCES profile (id),
            album_id uuid REFERENCES album (id),
            storage_path varchar NOT NULL,
            thumbnail_path varchar NOT NULL,
            clip_embedding vector(512),
            p_hash varchar,
            captured_at timestamp,
            location geography(POINT, 4326)
        )""")
    op.execute("CREATE INDEX ix_photo_user_id ON photo (user_id)")
    op.execute("CREATE INDEX ix_photo_album_id ON photo (album_id)")
    op.execute("CREATE INDEX ix_photo_p_hash ON photo (p_hash)")
    op.execute("CREATE INDEX ix_photo_captured_at ON photo (captured_at)")
    op.execute("CREATE INDEX idx_photo_location ON photo USING gist (location)")

    op.execute("""
        CREATE TABLE photoface (
            id uuid PRIMARY KEY,
            photo_id uuid NOT NULL REFERENCES photo (id),
            album_id uuid REFERENCES album (id),
            person_id uuid REFERENCES profile (id),
            face_embedding vector(512) NOT NULL
        )""")
    op.execute("CREATE INDEX ix_photoface_photo_id ON photoface (photo_id)")
    op.execute("CREATE INDEX ix_photoface_album_id ON photoface (album_id)")
    op.execute("CREATE INDEX ix_photoface_person_id ON photoface (person_id)")


def downgrade() -> None:
    for table in ("photoface", "photo", "albummemberlink", "album", "profile"):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
