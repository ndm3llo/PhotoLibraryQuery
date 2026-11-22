"""Phase 4b: nicknames, selfie references, opt-out, and remembered rejections.

- person_alias: nicknames ("Grandma", "Abuela") so search can resolve any name a family uses.
  Owners and editors manage them; every member can read them.
- person_reference: face embeddings a person supplied for themselves (selfies). EMBEDDINGS ONLY, never the
  image. Biometric, so readable only by owners/editors of the library and by the person themselves (via
  person.profile_id). No write policy: references are created by the service layer after consent checks.
- person.recognition_opt_out: when true the person is never matched automatically and has no references.
- face_rejection: "this face is NOT person X". Remembered so a rejected suggestion never comes back.
- person.qualifier: tells two people with the same name apart ("cousin", "wife of Ben"). The pair
  (name, qualifier) must be unique in a library, so two Sarahs are allowed but never indistinguishable.
- face_cluster.model_version: clusters are only meaningful within one face model.

Revision ID: 0008
Revises: 0007
"""
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

EDITOR = "ARRAY['owner', 'editor']"


def upgrade() -> None:
    op.execute("ALTER TABLE person ADD COLUMN recognition_opt_out boolean NOT NULL DEFAULT false")
    op.execute("ALTER TABLE person ADD COLUMN qualifier varchar(60) "
               "CHECK (qualifier IS NULL OR length(btrim(qualifier)) > 0)")
    op.execute("CREATE UNIQUE INDEX uq_person_name ON person "
               "(library_id, lower(btrim(display_name)), lower(btrim(COALESCE(qualifier, ''))))")
    op.execute("ALTER TABLE face_cluster ADD COLUMN model_version varchar NOT NULL DEFAULT 'facenet512'")

    op.execute("""
        CREATE TABLE person_alias (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            library_id uuid NOT NULL REFERENCES library (id) ON DELETE CASCADE,
            person_id uuid NOT NULL REFERENCES person (id) ON DELETE CASCADE,
            alias varchar(100) NOT NULL CHECK (length(btrim(alias)) > 0),
            created_by uuid REFERENCES profile (id) ON DELETE SET NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )""")
    # One spelling per person, ignoring case and surrounding spaces.
    op.execute("CREATE UNIQUE INDEX uq_person_alias ON person_alias (person_id, lower(btrim(alias)))")
    op.execute("CREATE INDEX ix_person_alias_lookup ON person_alias (library_id, lower(btrim(alias)))")

    op.execute("""
        CREATE TABLE person_reference (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            library_id uuid NOT NULL REFERENCES library (id) ON DELETE CASCADE,
            person_id uuid NOT NULL REFERENCES person (id) ON DELETE CASCADE,
            embedding vector(512) NOT NULL,
            model_version varchar NOT NULL,
            quality real,
            source varchar NOT NULL CHECK (source IN ('selfie', 'confirmed_face')),
            created_at timestamptz NOT NULL DEFAULT now()
        )""")
    op.execute("CREATE INDEX ix_person_reference_person ON person_reference (person_id)")

    op.execute("""
        CREATE TABLE face_rejection (
            face_id uuid NOT NULL REFERENCES face (id) ON DELETE CASCADE,
            person_id uuid NOT NULL REFERENCES person (id) ON DELETE CASCADE,
            library_id uuid NOT NULL REFERENCES library (id) ON DELETE CASCADE,
            rejected_by uuid REFERENCES profile (id) ON DELETE SET NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (face_id, person_id)
        )""")
    op.execute("CREATE INDEX ix_face_rejection_person ON face_rejection (person_id)")

    for t in ("person_alias", "person_reference", "face_rejection"):
        op.execute(f"REVOKE ALL ON {t} FROM anon")
        op.execute(f"REVOKE ALL ON {t} FROM authenticated")
        op.execute(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY")
    op.execute("GRANT SELECT, INSERT, DELETE ON person_alias TO authenticated")
    op.execute("GRANT SELECT ON person_reference TO authenticated")          # writes: service layer only
    op.execute("GRANT SELECT, INSERT, DELETE ON face_rejection TO authenticated")

    # The row's library_id must match the person's (and, for rejections, the face's) library, so a
    # row can never be filed under one library while pointing at another library's person.
    op.execute("CREATE POLICY alias_read ON person_alias FOR SELECT TO authenticated USING (is_member(library_id))")
    op.execute(f"""
        CREATE POLICY alias_insert ON person_alias FOR INSERT TO authenticated
        WITH CHECK (has_role(library_id, {EDITOR}) AND created_by = auth.uid()
            AND EXISTS (SELECT 1 FROM person p WHERE p.id = person_alias.person_id
                        AND p.library_id = person_alias.library_id))""")
    op.execute(f"CREATE POLICY alias_delete ON person_alias FOR DELETE TO authenticated "
               f"USING (has_role(library_id, {EDITOR}))")

    op.execute(f"""
        CREATE POLICY reference_read ON person_reference FOR SELECT TO authenticated
        USING (has_role(library_id, {EDITOR})
               OR EXISTS (SELECT 1 FROM person p WHERE p.id = person_reference.person_id
                          AND p.profile_id = auth.uid()))""")

    op.execute("CREATE POLICY rejection_read ON face_rejection FOR SELECT TO authenticated "
               "USING (is_member(library_id))")
    op.execute(f"""
        CREATE POLICY rejection_insert ON face_rejection FOR INSERT TO authenticated
        WITH CHECK (has_role(library_id, {EDITOR}) AND rejected_by = auth.uid()
            AND EXISTS (SELECT 1 FROM face f WHERE f.id = face_rejection.face_id
                        AND f.library_id = face_rejection.library_id)
            AND EXISTS (SELECT 1 FROM person p WHERE p.id = face_rejection.person_id
                        AND p.library_id = face_rejection.library_id))""")
    op.execute(f"CREATE POLICY rejection_delete ON face_rejection FOR DELETE TO authenticated "
               f"USING (has_role(library_id, {EDITOR}))")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS face_rejection")
    op.execute("DROP TABLE IF EXISTS person_reference")
    op.execute("DROP TABLE IF EXISTS person_alias")
    op.execute("ALTER TABLE face_cluster DROP COLUMN IF EXISTS model_version")
    op.execute("DROP INDEX IF EXISTS uq_person_name")
    op.execute("ALTER TABLE person DROP COLUMN IF EXISTS qualifier")
    op.execute("ALTER TABLE person DROP COLUMN IF EXISTS recognition_opt_out")
