"""Library invites, and a database-level guard so a library can never lose its last owner.

Invites are single-use, expiring, tied to an email address and a role. Only a SHA-256 hash of
the token is stored; the raw token is shown once to the inviting owner. Accepting goes through
accept_invite(), which checks the signed-in user's verified email against the invite.

Revision ID: 0004
Revises: 0003
"""
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE library_invite (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            library_id uuid NOT NULL REFERENCES library (id) ON DELETE CASCADE,
            email text NOT NULL CHECK (email = lower(email)),
            role varchar NOT NULL CHECK (role IN ('owner', 'editor', 'viewer')),
            token_hash text NOT NULL UNIQUE,
            created_by uuid NOT NULL REFERENCES profile (id),
            created_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL,
            accepted_at timestamptz,
            accepted_by uuid REFERENCES profile (id),
            revoked_at timestamptz
        )""")
    # At most one open invite per (library, email).
    op.execute("""
        CREATE UNIQUE INDEX uq_invite_open ON library_invite (library_id, email)
        WHERE accepted_at IS NULL AND revoked_at IS NULL""")

    op.execute("ALTER TABLE library_invite ENABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON library_invite FROM anon, authenticated")
    # token_hash is deliberately not selectable; only owners can see or change invites at all.
    op.execute("""
        GRANT SELECT (id, library_id, email, role, created_by, created_at, expires_at,
                      accepted_at, accepted_by, revoked_at) ON library_invite TO authenticated""")
    op.execute("GRANT INSERT ON library_invite TO authenticated")
    op.execute("GRANT UPDATE (revoked_at) ON library_invite TO authenticated")
    op.execute("""CREATE POLICY invite_read ON library_invite FOR SELECT TO authenticated
                  USING (has_role(library_id, ARRAY['owner']))""")
    op.execute("""CREATE POLICY invite_insert ON library_invite FOR INSERT TO authenticated
                  WITH CHECK (has_role(library_id, ARRAY['owner']) AND created_by = auth.uid())""")
    op.execute("""CREATE POLICY invite_revoke ON library_invite FOR UPDATE TO authenticated
                  USING (has_role(library_id, ARRAY['owner']))
                  WITH CHECK (has_role(library_id, ARRAY['owner']))""")

    op.execute("""
        CREATE FUNCTION accept_invite(p_token_hash text) RETURNS uuid
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
        DECLARE
            inv library_invite%ROWTYPE;
            uid uuid := auth.uid();
            claim_email text;
        BEGIN
            IF uid IS NULL THEN RAISE EXCEPTION 'not authenticated'; END IF;
            claim_email := lower(nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'email');

            SELECT * INTO inv FROM library_invite WHERE token_hash = p_token_hash FOR UPDATE;
            IF NOT FOUND OR inv.revoked_at IS NOT NULL OR inv.accepted_at IS NOT NULL
               OR inv.expires_at < now() THEN
                RAISE EXCEPTION 'invite_invalid';
            END IF;
            IF claim_email IS NULL OR claim_email <> inv.email THEN
                RAISE EXCEPTION 'invite_email_mismatch';
            END IF;

            INSERT INTO library_member (library_id, profile_id, role)
            VALUES (inv.library_id, uid, inv.role)
            ON CONFLICT (library_id, profile_id) DO NOTHING;

            UPDATE library_invite SET accepted_at = now(), accepted_by = uid WHERE id = inv.id;
            RETURN inv.library_id;
        END $$""")
    op.execute("REVOKE ALL ON FUNCTION accept_invite(text) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION accept_invite(text) TO authenticated, service_role")

    # A library must always keep at least one owner. Deleting the library itself (which cascades
    # to its members) is allowed: the library row is already gone when the cascade runs.
    op.execute("""
        CREATE FUNCTION protect_last_owner() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
        BEGIN
            IF OLD.role = 'owner' AND (TG_OP = 'DELETE' OR NEW.role <> 'owner')
               AND EXISTS (SELECT 1 FROM library WHERE id = OLD.library_id)
               AND NOT EXISTS (SELECT 1 FROM library_member m
                               WHERE m.library_id = OLD.library_id AND m.role = 'owner'
                                 AND m.profile_id <> OLD.profile_id) THEN
                RAISE EXCEPTION 'last_owner';
            END IF;
            IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
            RETURN NEW;
        END $$""")
    op.execute("""
        CREATE TRIGGER trg_protect_last_owner BEFORE UPDATE OF role OR DELETE ON library_member
        FOR EACH ROW EXECUTE FUNCTION protect_last_owner()""")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_protect_last_owner ON library_member")
    op.execute("DROP FUNCTION IF EXISTS protect_last_owner()")
    op.execute("DROP FUNCTION IF EXISTS accept_invite(text)")
    op.execute("DROP TABLE IF EXISTS library_invite")
