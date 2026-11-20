"""Drop the unused, empty `album_member_link` table.

It is a leftover from an earlier prototype naming (`albummemberlink` is the real one), has no
model, no code references, and held 0 rows when dropped. IF EXISTS makes this a no-op on fresh
databases, which never had it. The downgrade intentionally does not recreate it.

Revision ID: 0003
Revises: 0002
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TABLE IF EXISTS album_member_link")


def downgrade() -> None:
    pass
