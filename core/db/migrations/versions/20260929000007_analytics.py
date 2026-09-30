"""gift_events (storefront beacon, 90-day retention) + order_counts_daily.

Revision ID: 20260929000007
Revises: 20260929000006
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260929000007"
down_revision = "20260929000006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "gift_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("shop_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("shops.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("product_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_gift_events_shop_created", "gift_events", ["shop_id", "created_at"])
    op.create_table(
        "order_counts_daily",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("shop_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("shops.id", ondelete="CASCADE"), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("orders", sa.Integer(), nullable=False),
        sa.Column("revenue", sa.Numeric(14, 2), nullable=False),
        sa.UniqueConstraint("shop_id", "day", name="uq_order_counts_daily_shop_day"),
    )


def downgrade() -> None:
    op.drop_table("order_counts_daily")
    op.drop_index("ix_gift_events_shop_created", table_name="gift_events")
    op.drop_table("gift_events")
