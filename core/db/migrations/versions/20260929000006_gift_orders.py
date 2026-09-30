"""gift_orders: orders that carried GiftSense gift data (order facts only).

Revision ID: 20260929000006
Revises: 20260929000005
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260929000006"
down_revision = "20260929000005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "gift_orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("shop_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("shops.id", ondelete="CASCADE"), nullable=False),
        sa.Column("order_id", sa.String(), nullable=False),
        sa.Column("order_name", sa.String(), nullable=False),
        sa.Column("sid", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("delivery_mode", sa.String(), nullable=True),
        sa.Column("gift_lines", sa.Integer(), nullable=False),
        sa.Column("gift_revenue", sa.Numeric(12, 2), nullable=False),
        sa.Column("order_total", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(), nullable=False),
        sa.Column("groups", sa.JSON(), nullable=False),
        sa.Column("note_source", sa.String(), nullable=True),
        sa.Column("annotated", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("shop_id", "order_id", name="uq_gift_orders_shop_order"),
    )
    op.create_index("ix_gift_orders_shop_id", "gift_orders", ["shop_id"])
    op.create_index("ix_gift_orders_sid", "gift_orders", ["sid"])


def downgrade() -> None:
    op.drop_index("ix_gift_orders_sid", table_name="gift_orders")
    op.drop_index("ix_gift_orders_shop_id", table_name="gift_orders")
    op.drop_table("gift_orders")
