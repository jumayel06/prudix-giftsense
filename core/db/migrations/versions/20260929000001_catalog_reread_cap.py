"""shops.catalog_rereads_used / catalog_rereads_cycle_start: monthly cap on
paid AI re-reads of edited products (PLANS product_rereads_per_month).

Revision ID: 20260929000001
Revises: 20260928000004
"""
import sqlalchemy as sa
from alembic import op

revision = "20260929000001"
down_revision = "20260928000004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("shops") as batch:
        batch.add_column(sa.Column("catalog_rereads_used", sa.Integer(), server_default="0", nullable=False))
        batch.add_column(sa.Column("catalog_rereads_cycle_start", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("shops") as batch:
        batch.drop_column("catalog_rereads_cycle_start")
        batch.drop_column("catalog_rereads_used")
