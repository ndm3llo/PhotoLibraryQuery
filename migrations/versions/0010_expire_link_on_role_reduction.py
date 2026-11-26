"""Also expire the join link when someone's access is reduced.

0009 expires the link when a member is removed. Without this, someone changed to "viewer" could leave and rejoin
through an editor link and have their old access back. As with removal, only a change made by someone else counts,
and it is a trigger so that every path is covered. Raising someone's access leaves the link alone.

Revision ID: 0010
Revises: 0009
"""
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Reuses 0009's function, which already returns OLD; an AFTER trigger ignores the return value.
    op.execute("""
        CREATE TRIGGER trg_expire_join_link_on_demotion AFTER UPDATE OF role ON library_member
        FOR EACH ROW
        WHEN (array_position(ARRAY['viewer', 'editor', 'owner']::text[], NEW.role::text)
              < array_position(ARRAY['viewer', 'editor', 'owner']::text[], OLD.role::text))
        EXECUTE FUNCTION expire_join_link_on_removal()""")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_expire_join_link_on_demotion ON library_member")
