"""gift_sessions.refines: "Not quite right?" refinements used (max 2 per session).

Revision ID: 20260929000003
Revises: 20260929000002
"""
import sqlalchemy as sa
from alembic import op

revision = "20260929000003"
down_revision = "20260929000002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("gift_sessions") as batch:
        batch.add_column(sa.Column("refines", sa.Integer(), server_default="0", nullable=False))


def downgrade() -> None:
    with op.batch_alter_table("gift_sessions") as batch:
        batch.drop_column("refines")
