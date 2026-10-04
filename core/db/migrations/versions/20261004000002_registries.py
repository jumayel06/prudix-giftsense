"""registries + registry_items (Pro gift registries), and registry attribution on gift_orders.

Revision ID: 20261004000002
Revises: 20261004000001
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261004000002"
down_revision = "20261004000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "registries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("shop_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("shops.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("customer_id", sa.String(), nullable=False),
        sa.Column("share_token", sa.String(), nullable=False, unique=True),
        sa.Column("title", sa.String(), nullable=False, server_default=""),
        sa.Column("occasion", sa.String(), nullable=False, server_default="just_because"),
        sa.Column("event_date", sa.Date(), nullable=True),
        sa.Column("suggestions", sa.JSON(), nullable=True),
        sa.Column("views", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("shop_id", "customer_id", name="uq_registries_shop_customer"),
    )
    op.create_index("ix_registries_shop_id", "registries", ["shop_id"])
    op.create_table(
        "registry_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("registry_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("registries.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("shop_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("shops.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("product_id", sa.String(), nullable=False),
        sa.Column("variant_id", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False, server_default=""),
        sa.Column("variant_title", sa.String(), nullable=False, server_default=""),
        sa.Column("image_url", sa.String(), nullable=True),
        sa.Column("url", sa.String(), nullable=True),
        sa.Column("price", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("wanted_qty", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("bought_qty", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("registry_id", "variant_id", name="uq_registry_items_variant"),
    )
    op.create_index("ix_registry_items_registry_id", "registry_items", ["registry_id"])
    op.create_index("ix_registry_items_shop_id", "registry_items", ["shop_id"])
    with op.batch_alter_table("gift_orders") as batch:
        batch.add_column(sa.Column("registry_id", postgresql.UUID(as_uuid=True), nullable=True))
        batch.add_column(sa.Column("registry_revenue", sa.Numeric(12, 2), nullable=False, server_default="0"))
    op.create_index("ix_gift_orders_registry_id", "gift_orders", ["registry_id"])


def downgrade() -> None:
    op.drop_index("ix_gift_orders_registry_id", table_name="gift_orders")
    with op.batch_alter_table("gift_orders") as batch:
        batch.drop_column("registry_revenue")
        batch.drop_column("registry_id")
    op.drop_table("registry_items")
    op.drop_table("registries")
