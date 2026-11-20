"""Tenancy (library), people, new photo/face columns, and Row Level Security.

EXPAND step of an expand/contract migration: new structures are added next to the
legacy ones so the prototype keeps working. Legacy columns are removed in later phases:

  Phase 2: photo.user_id, albummemberlink
  Phase 3: photo.storage_path, thumbnail_path, p_hash, captured_at
  Phase 4: face.album_id, photo.album_id (replaced by album_photo); face.face_embedding -> embedding

DESTRUCTIVE NOTE: profile.face_embedding is dropped. It held unconsented biometric data
and nothing reads it. The downgrade re-adds the column empty.

Revision ID: 0002
Revises: 0001
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

TENANT_TABLES = [
    "library", "library_member", "person", "photo", "face", "face_cluster", "album", "album_photo",
]
SERVICE_ONLY_TABLES = ["biometric_consent", "face_credential", "audit_log", "albummemberlink"]


def upgrade() -> None:
    # Naive timestamps are interpreted as UTC when cast to timestamptz below.
    op.execute("SET LOCAL TIME ZONE 'UTC'")

    # ------------------------------------------------------------------ new tables
    op.execute("""
        CREATE TABLE library (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            name varchar NOT NULL,
            created_by uuid NOT NULL REFERENCES profile (id),
            created_at timestamptz NOT NULL DEFAULT now()
        )""")

    op.execute("""
        CREATE TABLE library_member (
            library_id uuid NOT NULL REFERENCES library (id) ON DELETE CASCADE,
            profile_id uuid NOT NULL REFERENCES profile (id) ON DELETE CASCADE,
            role varchar NOT NULL CHECK (role IN ('owner', 'editor', 'viewer')),
            PRIMARY KEY (library_id, profile_id)
        )""")
    op.execute("CREATE INDEX ix_library_member_profile ON library_member (profile_id)")

    op.execute("""
        CREATE TABLE person (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            library_id uuid NOT NULL REFERENCES library (id) ON DELETE CASCADE,
            display_name varchar NOT NULL,
            birth_date date,
            death_date date,
            profile_id uuid REFERENCES profile (id) ON DELETE SET NULL,
            cover_face_id uuid,
            created_by uuid REFERENCES profile (id),
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (library_id, profile_id)
        )""")
    op.execute("CREATE INDEX ix_person_library ON person (library_id)")

    op.execute("""
        CREATE TABLE face_cluster (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            library_id uuid NOT NULL REFERENCES library (id) ON DELETE CASCADE,
            person_id uuid REFERENCES person (id) ON DELETE SET NULL
        )""")
    op.execute("CREATE INDEX ix_face_cluster_library ON face_cluster (library_id)")

    op.execute("""
        CREATE TABLE biometric_consent (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            profile_id uuid NOT NULL REFERENCES profile (id) ON DELETE CASCADE,
            notice_version varchar NOT NULL,
            granted_at timestamptz NOT NULL,
            revoked_at timestamptz
        )""")

    op.execute("""
        CREATE TABLE face_credential (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            profile_id uuid NOT NULL REFERENCES profile (id) ON DELETE CASCADE,
            embedding_enc bytea NOT NULL,
            model_version varchar NOT NULL,
            consent_id uuid NOT NULL REFERENCES biometric_consent (id),
            created_at timestamptz NOT NULL DEFAULT now()
        )""")
    op.execute("CREATE INDEX ix_face_credential_profile ON face_credential (profile_id)")

    op.execute("""
        CREATE TABLE audit_log (
            id bigserial PRIMARY KEY,
            at timestamptz NOT NULL DEFAULT now(),
            actor uuid,
            library_id uuid,
            action varchar NOT NULL,
            target varchar,
            ip inet,
            meta jsonb
        )""")

    # -------------------------------------------------------------- profile changes
    op.execute("ALTER TABLE profile RENAME COLUMN full_name TO display_name")
    op.execute("ALTER INDEX IF EXISTS ix_profile_full_name RENAME TO ix_profile_display_name")
    op.execute("ALTER TABLE profile ADD COLUMN handle varchar")
    op.execute("ALTER TABLE profile ADD COLUMN created_at timestamptz NOT NULL DEFAULT now()")
    op.execute("""
        UPDATE profile SET handle =
            coalesce(nullif(trim(both '-' from lower(regexp_replace(display_name, '[^a-zA-Z0-9]+', '-', 'g'))), ''), 'user')
            || '-' || left(id::text, 6)""")
    op.execute("ALTER TABLE profile ALTER COLUMN handle SET NOT NULL")
    op.execute("ALTER TABLE profile ADD CONSTRAINT profile_handle_key UNIQUE (handle)")
    op.execute("ALTER TABLE profile DROP COLUMN face_embedding")

    # --------------------------------------------------------------- album changes
    op.execute("ALTER TABLE album ADD COLUMN library_id uuid REFERENCES library (id) ON DELETE CASCADE")
    op.execute("""
        CREATE TABLE album_photo (
            album_id uuid NOT NULL REFERENCES album (id) ON DELETE CASCADE,
            photo_id uuid NOT NULL REFERENCES photo (id) ON DELETE CASCADE,
            PRIMARY KEY (album_id, photo_id)
        )""")
    op.execute("CREATE INDEX ix_album_photo_photo ON album_photo (photo_id)")

    # --------------------------------------------------------------- photo changes
    # library_id stays nullable until Phase 2 removes the legacy upload path.
    op.execute("""
        ALTER TABLE photo
            ADD COLUMN library_id uuid REFERENCES library (id) ON DELETE CASCADE,
            ADD COLUMN uploader_id uuid REFERENCES profile (id),
            ADD COLUMN status varchar NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'processing', 'ready', 'failed', 'duplicate')),
            ADD COLUMN error text,
            ADD COLUMN original_key varchar,
            ADD COLUMN display_key varchar,
            ADD COLUMN thumb_key varchar,
            ADD COLUMN width integer,
            ADD COLUMN height integer,
            ADD COLUMN content_sha256 varchar,
            ADD COLUMN taken_at timestamptz,
            ADD COLUMN taken_at_source varchar CHECK (taken_at_source IN (
                'exif_original', 'exif_digitized', 'quicktime', 'filename', 'file_mtime',
                'neighbor_inferred', 'manual', 'legacy')),
            ADD COLUMN taken_at_confidence real,
            ADD COLUMN taken_tz varchar,
            ADD COLUMN created_at timestamptz NOT NULL DEFAULT now()""")
    op.execute("""
        UPDATE photo SET
            uploader_id = user_id,
            status = CASE WHEN storage_path = 'pending' THEN 'pending' ELSE 'ready' END,
            original_key = CASE WHEN storage_path = 'pending' THEN NULL
                ELSE regexp_replace(storage_path, '^https?://[^/]+/', '') END,
            thumb_key = CASE WHEN thumbnail_path = 'pending' THEN NULL
                ELSE regexp_replace(thumbnail_path, '^https?://[^/]+/', '') END,
            taken_at = captured_at::timestamptz,
            taken_at_source = CASE WHEN captured_at IS NULL THEN NULL ELSE 'legacy' END""")
    op.execute("CREATE INDEX ix_photo_library_taken ON photo (library_id, taken_at DESC)")
    op.execute("CREATE INDEX ix_photo_library_status ON photo (library_id, status)")
    op.execute("CREATE UNIQUE INDEX uq_photo_library_sha ON photo (library_id, content_sha256)")

    # ---------------------------------------------------------------- face changes
    op.execute("ALTER TABLE photoface RENAME TO face")
    op.execute("ALTER INDEX IF EXISTS ix_photoface_photo_id RENAME TO ix_face_photo_id")
    op.execute("ALTER INDEX IF EXISTS ix_photoface_album_id RENAME TO ix_face_album_id")
    op.execute("ALTER INDEX IF EXISTS ix_photoface_person_id RENAME TO ix_face_person_id")
    # person_id used to point at profile and was never populated; re-point it at person.
    op.execute("ALTER TABLE face DROP CONSTRAINT photoface_person_id_fkey")
    op.execute("UPDATE face SET person_id = NULL")
    op.execute("""
        ALTER TABLE face
            ADD CONSTRAINT face_person_id_fkey FOREIGN KEY (person_id) REFERENCES person (id) ON DELETE SET NULL,
            ADD COLUMN library_id uuid REFERENCES library (id) ON DELETE CASCADE,
            ADD COLUMN bbox integer[],
            ADD COLUMN det_score real,
            ADD COLUMN quality real,
            ADD COLUMN model_version varchar NOT NULL DEFAULT 'facenet512',
            ADD COLUMN cluster_id uuid REFERENCES face_cluster (id) ON DELETE SET NULL,
            ADD COLUMN person_source varchar CHECK (person_source IN ('manual', 'confirmed', 'auto')),
            ADD COLUMN person_confidence real""")
    op.execute("ALTER TABLE face DROP CONSTRAINT photoface_photo_id_fkey")
    op.execute("ALTER TABLE face ADD CONSTRAINT face_photo_id_fkey FOREIGN KEY (photo_id) REFERENCES photo (id) ON DELETE CASCADE")
    op.execute("CREATE INDEX ix_face_library_person ON face (library_id, person_id)")
    op.execute("CREATE INDEX ix_face_cluster ON face (cluster_id)")
    op.execute("""
        ALTER TABLE person ADD CONSTRAINT person_cover_face_fkey
            FOREIGN KEY (cover_face_id) REFERENCES face (id) ON DELETE SET NULL""")

    # -------------------------------------------------- backfill into one library
    op.execute("""
    DO $$
    DECLARE
        lib uuid;
        owner_id uuid;
    BEGIN
        SELECT p.id INTO owner_id FROM profile p
        ORDER BY (SELECT count(*) FROM photo WHERE user_id = p.id) DESC, p.id LIMIT 1;

        IF owner_id IS NOT NULL THEN
            INSERT INTO library (name, created_by) VALUES ('Family', owner_id) RETURNING id INTO lib;

            INSERT INTO library_member (library_id, profile_id, role)
            SELECT lib, p.id,
                   CASE
                     WHEN p.id = owner_id THEN 'owner'
                     WHEN EXISTS (SELECT 1 FROM photo WHERE user_id = p.id)
                       OR EXISTS (SELECT 1 FROM album WHERE created_by = p.id)
                     THEN 'editor'
                     ELSE 'viewer'
                   END
            FROM profile p;

            UPDATE photo SET library_id = lib;
            UPDATE album SET library_id = lib;
            UPDATE face f SET library_id = p.library_id FROM photo p WHERE p.id = f.photo_id;
            INSERT INTO album_photo (album_id, photo_id)
            SELECT album_id, id FROM photo WHERE album_id IS NOT NULL;
        END IF;
    END $$;""")

    # ------------------------------------------------------------------ functions
    op.execute("""
        CREATE FUNCTION is_member(lib uuid) RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
            SELECT EXISTS (SELECT 1 FROM library_member m WHERE m.library_id = lib AND m.profile_id = auth.uid())
        $$""")
    op.execute("""
        CREATE FUNCTION has_role(lib uuid, roles text[]) RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
            SELECT EXISTS (SELECT 1 FROM library_member m
                           WHERE m.library_id = lib AND m.profile_id = auth.uid() AND m.role = ANY (roles))
        $$""")
    op.execute("""
        CREATE FUNCTION shares_library_with(other uuid) RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $$
            SELECT EXISTS (SELECT 1 FROM library_member a JOIN library_member b ON a.library_id = b.library_id
                           WHERE a.profile_id = auth.uid() AND b.profile_id = other)
        $$""")
    op.execute("""
        CREATE FUNCTION create_library(p_name text) RETURNS uuid
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
        DECLARE lid uuid;
        BEGIN
            IF auth.uid() IS NULL THEN RAISE EXCEPTION 'not authenticated'; END IF;
            INSERT INTO library (name, created_by) VALUES (p_name, auth.uid()) RETURNING id INTO lid;
            INSERT INTO library_member (library_id, profile_id, role) VALUES (lid, auth.uid(), 'owner');
            RETURN lid;
        END $$""")
    for fn in ("is_member(uuid)", "has_role(uuid, text[])", "shares_library_with(uuid)", "create_library(text)"):
        op.execute(f"REVOKE ALL ON FUNCTION {fn} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {fn} TO authenticated, service_role")

    # --------------------------------------------------------------- grants + RLS
    all_tables = TENANT_TABLES + ["profile"] + SERVICE_ONLY_TABLES
    for t in all_tables:
        op.execute(f"REVOKE ALL ON {t} FROM anon")
        op.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
    for t in SERVICE_ONLY_TABLES:
        # RLS on with no policy, and no grant: unreachable except by owner / service role.
        op.execute(f"REVOKE ALL ON {t} FROM authenticated")
    op.execute("""
        DO $$ BEGIN
            IF to_regclass('public.album_member_link') IS NOT NULL THEN
                REVOKE ALL ON album_member_link FROM anon, authenticated;
                ALTER TABLE album_member_link ENABLE ROW LEVEL SECURITY;
            END IF;
        END $$;""")
    op.execute("REVOKE ALL ON profile FROM authenticated")
    op.execute("GRANT SELECT, UPDATE ON profile TO authenticated")
    op.execute("REVOKE ALL ON library FROM authenticated")
    op.execute("GRANT SELECT, UPDATE, DELETE ON library TO authenticated")  # insert only via create_library()

    editor = "ARRAY['owner', 'editor']"
    policies = [
        # profile
        ("profile", "profile_read", "SELECT", "USING (id = auth.uid() OR shares_library_with(id))"),
        ("profile", "profile_update_self", "UPDATE", "USING (id = auth.uid()) WITH CHECK (id = auth.uid())"),
        # library
        ("library", "library_read", "SELECT", "USING (is_member(id))"),
        ("library", "library_update", "UPDATE",
         "USING (has_role(id, ARRAY['owner'])) WITH CHECK (has_role(id, ARRAY['owner']))"),
        ("library", "library_delete", "DELETE", "USING (has_role(id, ARRAY['owner']))"),
        # library_member
        ("library_member", "member_read", "SELECT", "USING (is_member(library_id))"),
        ("library_member", "member_insert", "INSERT", "WITH CHECK (has_role(library_id, ARRAY['owner']))"),
        ("library_member", "member_update", "UPDATE",
         "USING (has_role(library_id, ARRAY['owner'])) WITH CHECK (has_role(library_id, ARRAY['owner']))"),
        ("library_member", "member_delete", "DELETE",
         "USING (has_role(library_id, ARRAY['owner']) OR profile_id = auth.uid())"),
        # person
        ("person", "person_read", "SELECT", "USING (is_member(library_id))"),
        ("person", "person_insert", "INSERT", f"WITH CHECK (has_role(library_id, {editor}))"),
        ("person", "person_update", "UPDATE",
         f"USING (has_role(library_id, {editor})) WITH CHECK (has_role(library_id, {editor}))"),
        ("person", "person_delete", "DELETE", f"USING (has_role(library_id, {editor}))"),
        # photo
        ("photo", "photo_read", "SELECT", "USING (is_member(library_id))"),
        ("photo", "photo_insert", "INSERT",
         f"WITH CHECK (has_role(library_id, {editor}) AND uploader_id = auth.uid())"),
        ("photo", "photo_update", "UPDATE",
         f"USING (has_role(library_id, {editor})) WITH CHECK (has_role(library_id, {editor}))"),
        ("photo", "photo_delete", "DELETE", f"USING (has_role(library_id, {editor}))"),
        # face: created and deleted only by the worker (service role); editors may label
        ("face", "face_read", "SELECT", "USING (is_member(library_id))"),
        ("face", "face_update", "UPDATE",
         f"USING (has_role(library_id, {editor})) WITH CHECK (has_role(library_id, {editor}))"),
        # face_cluster
        ("face_cluster", "cluster_read", "SELECT", "USING (is_member(library_id))"),
        ("face_cluster", "cluster_insert", "INSERT", f"WITH CHECK (has_role(library_id, {editor}))"),
        ("face_cluster", "cluster_update", "UPDATE",
         f"USING (has_role(library_id, {editor})) WITH CHECK (has_role(library_id, {editor}))"),
        ("face_cluster", "cluster_delete", "DELETE", f"USING (has_role(library_id, {editor}))"),
        # album
        ("album", "album_read", "SELECT", "USING (is_member(library_id))"),
        ("album", "album_insert", "INSERT",
         f"WITH CHECK (has_role(library_id, {editor}) AND created_by = auth.uid())"),
        ("album", "album_update", "UPDATE",
         f"USING (has_role(library_id, {editor})) WITH CHECK (has_role(library_id, {editor}))"),
        ("album", "album_delete", "DELETE", f"USING (has_role(library_id, {editor}))"),
        # album_photo: the photo and the album must belong to the same library
        ("album_photo", "album_photo_read", "SELECT",
         "USING (EXISTS (SELECT 1 FROM album a WHERE a.id = album_photo.album_id AND is_member(a.library_id)))"),
        ("album_photo", "album_photo_insert", "INSERT",
         f"WITH CHECK (EXISTS (SELECT 1 FROM album a JOIN photo p ON p.library_id = a.library_id "
         f"WHERE a.id = album_photo.album_id AND p.id = album_photo.photo_id "
         f"AND has_role(a.library_id, {editor})))"),
        ("album_photo", "album_photo_delete", "DELETE",
         f"USING (EXISTS (SELECT 1 FROM album a WHERE a.id = album_photo.album_id "
         f"AND has_role(a.library_id, {editor})))"),
    ]
    for table, name, cmd, clause in policies:
        op.execute(f"CREATE POLICY {name} ON {table} FOR {cmd} TO authenticated {clause}")

    # Table-level privileges for the authenticated role (policies still gate every row).
    for t in TENANT_TABLES:
        if t == "library":
            continue
        op.execute(f"REVOKE ALL ON {t} FROM authenticated")
        op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {t} TO authenticated")
    op.execute("REVOKE INSERT, DELETE ON face FROM authenticated")


def downgrade() -> None:
    """Best effort and LOSSY: profile.face_embedding comes back empty."""
    for fn in ("create_library(text)", "shares_library_with(uuid)", "has_role(uuid, text[])", "is_member(uuid)"):
        op.execute(f"DROP FUNCTION IF EXISTS {fn} CASCADE")  # also drops policies that use them
    op.execute("DROP POLICY IF EXISTS profile_update_self ON profile")

    op.execute("ALTER TABLE person DROP CONSTRAINT IF EXISTS person_cover_face_fkey")
    op.execute("""
        ALTER TABLE face
            DROP COLUMN person_confidence, DROP COLUMN person_source, DROP COLUMN cluster_id,
            DROP COLUMN model_version, DROP COLUMN quality, DROP COLUMN det_score,
            DROP COLUMN bbox, DROP COLUMN library_id""")
    op.execute("ALTER TABLE face DROP CONSTRAINT IF EXISTS face_person_id_fkey")
    op.execute("UPDATE face SET person_id = NULL")
    op.execute("ALTER TABLE face ADD CONSTRAINT photoface_person_id_fkey FOREIGN KEY (person_id) REFERENCES profile (id)")
    op.execute("ALTER TABLE face DROP CONSTRAINT IF EXISTS face_photo_id_fkey")
    op.execute("ALTER TABLE face ADD CONSTRAINT photoface_photo_id_fkey FOREIGN KEY (photo_id) REFERENCES photo (id)")
    op.execute("ALTER INDEX IF EXISTS ix_face_photo_id RENAME TO ix_photoface_photo_id")
    op.execute("ALTER INDEX IF EXISTS ix_face_album_id RENAME TO ix_photoface_album_id")
    op.execute("ALTER INDEX IF EXISTS ix_face_person_id RENAME TO ix_photoface_person_id")
    op.execute("ALTER TABLE face RENAME TO photoface")

    op.execute("""
        ALTER TABLE photo
            DROP COLUMN created_at, DROP COLUMN taken_tz, DROP COLUMN taken_at_confidence,
            DROP COLUMN taken_at_source, DROP COLUMN taken_at, DROP COLUMN content_sha256,
            DROP COLUMN height, DROP COLUMN width, DROP COLUMN thumb_key, DROP COLUMN display_key,
            DROP COLUMN original_key, DROP COLUMN error, DROP COLUMN status,
            DROP COLUMN uploader_id, DROP COLUMN library_id""")
    op.execute("DROP TABLE IF EXISTS album_photo")
    op.execute("ALTER TABLE album DROP COLUMN library_id")

    op.execute("ALTER TABLE profile ADD COLUMN face_embedding vector(512)")
    op.execute("ALTER TABLE profile DROP COLUMN created_at")
    op.execute("ALTER TABLE profile DROP CONSTRAINT profile_handle_key")
    op.execute("ALTER TABLE profile DROP COLUMN handle")
    op.execute("ALTER INDEX IF EXISTS ix_profile_display_name RENAME TO ix_profile_full_name")
    op.execute("ALTER TABLE profile RENAME COLUMN display_name TO full_name")

    for t in ("audit_log", "face_credential", "biometric_consent", "face_cluster", "person",
              "library_member", "library"):
        op.execute(f"DROP TABLE IF EXISTS {t} CASCADE")
    for t in ("profile", "albummemberlink", "photo", "album"):
        op.execute(f"ALTER TABLE {t} DISABLE ROW LEVEL SECURITY")
