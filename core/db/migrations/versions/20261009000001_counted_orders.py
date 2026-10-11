"""counted_orders: each order counted once in order_counts_daily (webhook + hourly reconcile).

Revision ID: 20261009000001
Revises: 20261004000003
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261009000001"
down_revision = "20261004000003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "counted_orders",
        sa.Column("shop_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("shops.id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("order_id", sa.String(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_counted_orders_created_at", "counted_orders", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_counted_orders_created_at", table_name="counted_orders")
    op.drop_table("counted_orders")
