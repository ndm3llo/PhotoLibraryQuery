"""Join by link or six-character code, and when each member joined.

Each library has at most one active join link. Unlike the email invites in 0004, the link and code are not tied to
an email address, can be used by many people, and are stored readable so an owner can show them again. Hashing the
link token would protect nothing: the short code next to it grants the same access and is easy to brute-force from
a hash. Only owners can read them (Row Level Security). Nothing is writable by users except deleting the link: the
API writes it with the service role after checking ownership, because a user-chosen code would turn the unique
index into a way to test whether another library's code exists.

join_attempt records failed code attempts for rate limiting and is service-role only.

When a member is removed by someone else, the library's link expires in the same transaction, so the removed person
cannot walk back in with it. It is a trigger so that removal through any path does it. Leaving on your own does not.

Revision ID: 0009
Revises: 0008
"""
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE library_join_link (
            library_id uuid PRIMARY KEY REFERENCES library (id) ON DELETE CASCADE,
            token text NOT NULL UNIQUE CHECK (length(token) >= 32),
            code text NOT NULL UNIQUE CHECK (code ~ '^[A-HJ-NP-Z2-9]{6}$'),
            role varchar NOT NULL CHECK (role IN ('editor', 'viewer')),
            created_by uuid REFERENCES profile (id) ON DELETE SET NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL
        )""")
    op.execute("ALTER TABLE library_join_link ENABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON library_join_link FROM anon, authenticated")
    op.execute("GRANT SELECT, DELETE ON library_join_link TO authenticated")
    op.execute("""CREATE POLICY join_link_read ON library_join_link FOR SELECT TO authenticated
                  USING (has_role(library_id, ARRAY['owner']))""")
    op.execute("""CREATE POLICY join_link_delete ON library_join_link FOR DELETE TO authenticated
                  USING (has_role(library_id, ARRAY['owner']))""")

    op.execute("""
        CREATE TABLE join_attempt (
            id bigserial PRIMARY KEY,
            at timestamptz NOT NULL DEFAULT now(),
            profile_id uuid NOT NULL REFERENCES profile (id) ON DELETE CASCADE,
            ip inet
        )""")
    op.execute("CREATE INDEX ix_join_attempt_profile ON join_attempt (profile_id, at)")
    op.execute("CREATE INDEX ix_join_attempt_ip ON join_attempt (ip, at)")
    # RLS on with no policy and no grant: unreachable except by the owner / service role.
    op.execute("ALTER TABLE join_attempt ENABLE ROW LEVEL SECURITY")
    op.execute("REVOKE ALL ON join_attempt FROM anon, authenticated")
    op.execute("REVOKE ALL ON SEQUENCE join_attempt_id_seq FROM anon, authenticated")

    op.execute("""
        CREATE FUNCTION expire_join_link_on_removal() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
        BEGIN
            -- auth.uid() is NULL for the service role (account deletion, scripts): nobody was "removed" then.
            IF auth.uid() IS NOT NULL AND auth.uid() <> OLD.profile_id THEN
                UPDATE library_join_link SET expires_at = now() - interval '1 second'
                 WHERE library_id = OLD.library_id;
            END IF;
            RETURN OLD;
        END $$""")
    op.execute("REVOKE ALL ON FUNCTION expire_join_link_on_removal() FROM PUBLIC")
    op.execute("""
        CREATE TRIGGER trg_expire_join_link AFTER DELETE ON library_member
        FOR EACH ROW EXECUTE FUNCTION expire_join_link_on_removal()""")

    # Added without a default first, so existing members stay NULL ("unknown") instead of "joined today".
    op.execute("ALTER TABLE library_member ADD COLUMN joined_at timestamptz")
    op.execute("ALTER TABLE library_member ALTER COLUMN joined_at SET DEFAULT now()")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_expire_join_link ON library_member")
    op.execute("DROP FUNCTION IF EXISTS expire_join_link_on_removal()")
    op.execute("ALTER TABLE library_member DROP COLUMN IF EXISTS joined_at")
    op.execute("DROP TABLE IF EXISTS join_attempt")
    op.execute("DROP TABLE IF EXISTS library_join_link")
