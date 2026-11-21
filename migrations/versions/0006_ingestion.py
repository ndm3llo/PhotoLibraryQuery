"""Phase 3: ingestion columns, durable job queue, and removal of the legacy photo columns.

- photo gets: phash (64-bit), content type / filename / size, client last-modified, upload batch,
  duplicate links, enrichment bookkeeping.
- Legacy columns storage_path, thumbnail_path, p_hash, captured_at are dropped (replaced by
  original_key / display_key / thumb_key, phash, taken_at). Any rows still holding only legacy data
  must be deleted BEFORE this runs; they cannot be served any more.
- content_sha256 uniqueness no longer applies to rows marked as duplicates.
- `job` is a Postgres-backed work queue (claimed with FOR UPDATE SKIP LOCKED). Service role only.

Revision ID: 0006
Revises: 0005
"""
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE photo
            ADD COLUMN phash bigint,
            ADD COLUMN content_type varchar,
            ADD COLUMN original_filename varchar,
            ADD COLUMN byte_size bigint,
            ADD COLUMN client_last_modified timestamptz,
            ADD COLUMN batch_id uuid,
            ADD COLUMN duplicate_of uuid REFERENCES photo (id) ON DELETE SET NULL,
            ADD COLUMN near_duplicate_of uuid REFERENCES photo (id) ON DELETE SET NULL,
            ADD COLUMN enriched_at timestamptz,
            ADD COLUMN enrich_error text""")
    op.execute("CREATE INDEX ix_photo_batch ON photo (batch_id)")

    op.execute("""
        ALTER TABLE photo
            DROP COLUMN IF EXISTS storage_path,
            DROP COLUMN IF EXISTS thumbnail_path,
            DROP COLUMN IF EXISTS p_hash,
            DROP COLUMN IF EXISTS captured_at""")

    # Rows marked 'duplicate' keep no hash, but be explicit: the uniqueness only guards real photos.
    op.execute("DROP INDEX IF EXISTS uq_photo_library_sha")
    op.execute("""
        CREATE UNIQUE INDEX uq_photo_library_sha ON photo (library_id, content_sha256)
        WHERE content_sha256 IS NOT NULL AND status <> 'duplicate'""")

    op.execute("""
        CREATE TABLE job (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            kind varchar NOT NULL,
            payload jsonb NOT NULL DEFAULT '{}'::jsonb,
            status varchar NOT NULL DEFAULT 'queued' CHECK (status IN ('queued', 'running', 'done', 'dead')),
            attempts integer NOT NULL DEFAULT 0,
            max_attempts integer NOT NULL DEFAULT 5,
            run_at timestamptz NOT NULL DEFAULT now(),
            lease_expires_at timestamptz,
            locked_by varchar,
            last_error text,
            dedupe_key varchar,
            created_at timestamptz NOT NULL DEFAULT now(),
            finished_at timestamptz
        )""")
    # One ACTIVE job per dedupe key (e.g. one process_photo per photo); finished jobs do not block a retry.
    op.execute("""
        CREATE UNIQUE INDEX uq_job_dedupe ON job (dedupe_key)
        WHERE dedupe_key IS NOT NULL AND status IN ('queued', 'running')""")
    op.execute("CREATE INDEX ix_job_queued ON job (run_at) WHERE status = 'queued'")
    op.execute("CREATE INDEX ix_job_running_lease ON job (lease_expires_at) WHERE status = 'running'")
    op.execute("ALTER TABLE job ENABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON job FROM anon, authenticated")


def downgrade() -> None:
    """Best effort: legacy columns return empty (nullable)."""
    op.execute("DROP TABLE IF EXISTS job")
    op.execute("DROP INDEX IF EXISTS uq_photo_library_sha")
    op.execute("""
        CREATE UNIQUE INDEX uq_photo_library_sha ON photo (library_id, content_sha256)""")
    op.execute("""
        ALTER TABLE photo
            ADD COLUMN storage_path varchar,
            ADD COLUMN thumbnail_path varchar,
            ADD COLUMN p_hash varchar,
            ADD COLUMN captured_at timestamptz""")
    op.execute("UPDATE photo SET captured_at = taken_at")
    op.execute("""
        ALTER TABLE photo
            DROP COLUMN enrich_error, DROP COLUMN enriched_at, DROP COLUMN near_duplicate_of,
            DROP COLUMN duplicate_of, DROP COLUMN batch_id, DROP COLUMN client_last_modified,
            DROP COLUMN byte_size, DROP COLUMN original_filename, DROP COLUMN content_type,
            DROP COLUMN phash""")
