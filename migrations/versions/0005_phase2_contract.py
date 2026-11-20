"""Phase 2 contract step: remove structures the new auth model replaces.

- photo.user_id            -> uploader_id (already backfilled in 0002)
- albummemberlink          -> library_member (already backfilled in 0002)
- face_credential          -> face login is not planned
- photo.library_id / uploader_id become required
- profile.id gets a foreign key to Supabase's auth.users, added NOT VALID so an existing
  profile that predates sign-in does not block the migration. Run
  `ALTER TABLE profile VALIDATE CONSTRAINT profile_id_auth_fkey` after scripts/link_owner.py.

Revision ID: 0005
Revises: 0004
"""
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE photo DROP COLUMN user_id")
    op.execute("DROP TABLE IF EXISTS albummemberlink")
    op.execute("DROP TABLE IF EXISTS face_credential")
    op.execute("ALTER TABLE photo ALTER COLUMN library_id SET NOT NULL")
    op.execute("ALTER TABLE photo ALTER COLUMN uploader_id SET NOT NULL")
    op.execute("""
        DO $$ BEGIN
            IF to_regclass('auth.users') IS NOT NULL THEN
                ALTER TABLE profile ADD CONSTRAINT profile_id_auth_fkey
                    FOREIGN KEY (id) REFERENCES auth.users (id) ON DELETE CASCADE NOT VALID;
            END IF;
        END $$""")


def downgrade() -> None:
    """Best effort. Recreated tables are empty; photo.user_id is rebuilt from uploader_id."""
    op.execute("ALTER TABLE profile DROP CONSTRAINT IF EXISTS profile_id_auth_fkey")
    op.execute("ALTER TABLE photo ALTER COLUMN uploader_id DROP NOT NULL")
    op.execute("ALTER TABLE photo ALTER COLUMN library_id DROP NOT NULL")
    op.execute("""
        CREATE TABLE face_credential (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            profile_id uuid NOT NULL REFERENCES profile (id) ON DELETE CASCADE,
            embedding_enc bytea NOT NULL,
            model_version varchar NOT NULL,
            consent_id uuid NOT NULL REFERENCES biometric_consent (id),
            created_at timestamptz NOT NULL DEFAULT now()
        )""")
    op.execute("ALTER TABLE face_credential ENABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON face_credential FROM anon, authenticated")
    op.execute("""
        CREATE TABLE albummemberlink (
            album_id uuid NOT NULL REFERENCES album (id),
            user_id uuid NOT NULL REFERENCES profile (id),
            role varchar NOT NULL,
            PRIMARY KEY (album_id, user_id)
        )""")
    op.execute("ALTER TABLE albummemberlink ENABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON albummemberlink FROM anon, authenticated")
    op.execute("ALTER TABLE photo ADD COLUMN user_id uuid REFERENCES profile (id)")
    op.execute("UPDATE photo SET user_id = uploader_id")
    op.execute("ALTER TABLE photo ALTER COLUMN user_id SET NOT NULL")
    op.execute("CREATE INDEX ix_photo_user_id ON photo (user_id)")
