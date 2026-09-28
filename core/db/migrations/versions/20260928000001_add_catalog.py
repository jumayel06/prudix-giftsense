"""add catalog_products (with vector(512) embedding) and catalog_syncs

Revision ID: 20260928000001
Revises: 20260927000001
Create Date: 2026-09-28

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from core.db.types import Embedding

revision: str = "20260928000001"
down_revision: Union[str, None] = "20260927000001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "catalog_products",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("shop_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("shops.id", ondelete="CASCADE"), nullable=False),
        sa.Column("product_id", sa.String(), nullable=False),
        sa.Column("handle", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("product_type", sa.String(), nullable=False, server_default=""),
        sa.Column("vendor", sa.String(), nullable=False, server_default=""),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("price_min", sa.Numeric(12, 2), nullable=False),
        sa.Column("price_max", sa.Numeric(12, 2), nullable=False),
        sa.Column("available", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("image_url", sa.String(), nullable=True),
        sa.Column("url", sa.String(), nullable=True),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("gift_profile", sa.JSON(), nullable=True),
        sa.Column("profile_version", sa.String(), nullable=True),
        sa.Column("profile_hash", sa.String(), nullable=True),
        sa.Column("profile_fallback", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("merchant_overrides", sa.JSON(), nullable=True),
        sa.Column("excluded", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("embedding", Embedding(512), nullable=True),
        sa.Column("embedding_model", sa.String(), nullable=True),
        sa.Column("enriched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("shop_id", "product_id", name="uq_catalog_products_shop_product"),
    )
    op.create_index("ix_catalog_products_shop_id", "catalog_products", ["shop_id"])
    # No ANN (HNSW) index: per-shop catalogs are small enough for an exact scan
    # filtered by shop_id, which also avoids filtered-ANN recall loss (plan §4.4).

    op.create_table(
        "catalog_syncs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("shop_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("shops.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("bulk_operation_id", sa.String(), nullable=True),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column("processed", sa.Integer(), nullable=False),
        sa.Column("enriched", sa.Integer(), nullable=False),
        sa.Column("failed", sa.Integer(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_catalog_syncs_shop_id", "catalog_syncs", ["shop_id"])


def downgrade() -> None:
    op.drop_index("ix_catalog_syncs_shop_id", table_name="catalog_syncs")
    op.drop_table("catalog_syncs")
    op.drop_index("ix_catalog_products_shop_id", table_name="catalog_products")
    op.drop_table("catalog_products")
