"""gift_media: shoppers' voice and video messages (files in Cloudflare R2).

Revision ID: 20261004000001
Revises: 20261002000001
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20261004000001"
down_revision = "20261002000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "gift_media",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("shop_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("shops.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("token", sa.String(), nullable=False, unique=True),
        sa.Column("view_token", sa.String(), nullable=False, unique=True),
        sa.Column("sid", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("order_id", sa.String(), nullable=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("storage_key", sa.String(), nullable=False),
        sa.Column("mime", sa.String(), nullable=False),
        sa.Column("bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duration_s", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("view_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_gift_media_shop_id", "gift_media", ["shop_id"])
    op.create_index("ix_gift_media_order_id", "gift_media", ["order_id"])
    op.create_index("ix_gift_media_purge", "gift_media", ["status", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_gift_media_purge", table_name="gift_media")
    op.drop_index("ix_gift_media_order_id", table_name="gift_media")
    op.drop_index("ix_gift_media_shop_id", table_name="gift_media")
    op.drop_table("gift_media")
