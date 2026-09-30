"""gift_sessions.note_drafts: per-gift draft count and last draft.

Revision ID: 20260929000005
Revises: 20260929000004
"""
import sqlalchemy as sa
from alembic import op

revision = "20260929000005"
down_revision = "20260929000004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("gift_sessions") as batch:
        batch.add_column(sa.Column("note_drafts", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("gift_sessions") as batch:
        batch.drop_column("note_drafts")
