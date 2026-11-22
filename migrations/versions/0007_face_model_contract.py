"""Phase 4a: face model contract.

- face.face_embedding is renamed to embedding (it is no longer tied to one model; model_version says which).
- album links now live in album_photo, so the legacy photo.album_id and face.album_id are dropped.
- photo.faces_model records which face model last produced this photo's faces, so re-processing with the
  SAME model never wipes the labels people have attached to those faces.

Existing Facenet512 faces keep model_version 'facenet512'. They are replaced the first time a photo is
re-enriched with the new model (unlabeled ones only), see app/services/pipeline.py.

Revision ID: 0007
Revises: 0006
"""
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE face RENAME COLUMN face_embedding TO embedding")
    op.execute("ALTER TABLE face DROP COLUMN IF EXISTS album_id")
    op.execute("ALTER TABLE photo DROP COLUMN IF EXISTS album_id")
    op.execute("ALTER TABLE photo ADD COLUMN faces_model varchar")
    op.execute("CREATE INDEX ix_face_library_model ON face (library_id, model_version)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_face_library_model")
    op.execute("ALTER TABLE photo DROP COLUMN IF EXISTS faces_model")
    op.execute("ALTER TABLE photo ADD COLUMN album_id uuid REFERENCES album (id)")
    op.execute("ALTER TABLE face ADD COLUMN album_id uuid REFERENCES album (id)")
    op.execute("ALTER TABLE face RENAME COLUMN embedding TO face_embedding")
