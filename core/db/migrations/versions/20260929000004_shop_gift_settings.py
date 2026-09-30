"""shops.gift_settings: merchant gift settings (notes: tone, max_chars, banned_words).

Revision ID: 20260929000004
Revises: 20260929000003
"""
import sqlalchemy as sa
from alembic import op

revision = "20260929000004"
down_revision = "20260929000003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("shops") as batch:
        batch.add_column(sa.Column("gift_settings", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("shops") as batch:
        batch.drop_column("gift_settings")
