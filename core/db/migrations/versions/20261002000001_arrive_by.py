"""gift_orders: arrive-by date, ship-by date and fulfillment hold status.

Revision ID: 20261002000001
Revises: 20260929000007
"""
import sqlalchemy as sa
from alembic import op

revision = "20261002000001"
down_revision = "20260929000007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("gift_orders") as batch:
        batch.add_column(sa.Column("arrive_by", sa.Date(), nullable=True))
        batch.add_column(sa.Column("ship_by", sa.Date(), nullable=True))
        batch.add_column(sa.Column("hold_status", sa.String(), nullable=True))
    op.create_index("ix_gift_orders_hold_due", "gift_orders", ["hold_status", "ship_by"])


def downgrade() -> None:
    op.drop_index("ix_gift_orders_hold_due", table_name="gift_orders")
    with op.batch_alter_table("gift_orders") as batch:
        batch.drop_column("hold_status")
        batch.drop_column("ship_by")
        batch.drop_column("arrive_by")
