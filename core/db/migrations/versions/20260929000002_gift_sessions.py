"""gift_sessions: one row per storefront widget session (sid, last brief,
final picks, search count) for refine, attribution and analytics.

Revision ID: 20260929000002
Revises: 20260929000001
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "20260929000002"
down_revision = "20260929000001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "gift_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("shop_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("shops.id", ondelete="CASCADE"), nullable=False),
        sa.Column("sid", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("intake", sa.JSON(), nullable=False),
        sa.Column("last_picks", sa.JSON(), nullable=False),
        sa.Column("searches", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("shop_id", "sid", name="uq_gift_sessions_shop_sid"),
    )
    op.create_index("ix_gift_sessions_shop_id", "gift_sessions", ["shop_id"])


def downgrade() -> None:
    op.drop_index("ix_gift_sessions_shop_id", table_name="gift_sessions")
    op.drop_table("gift_sessions")
