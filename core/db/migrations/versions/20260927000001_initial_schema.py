"""initial schema: foundation tables + pgvector extension

Foundation tables copied from Prudix Commerce, trimmed to GiftSense
(core/db/models.py). Also enables the `vector` extension for the gift-matching
engine's product embeddings (feature tables arrive in later migrations).

Revision ID: 20260927000001
Revises:
Create Date: 2026-09-27

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260927000001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Supabase keeps extensions in the `extensions` schema, which is on the
    # default search_path. Idempotent: a no-op if enabled from the dashboard.
    if op.get_bind().dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA extensions")

    op.create_table(
        "shops",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("shop_domain", sa.String(), nullable=False),
        sa.Column("access_token_encrypted", sa.Text(), nullable=False),
        sa.Column("refresh_token_encrypted", sa.Text(), nullable=True),
        sa.Column("access_token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refresh_token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("plan_tier", sa.String(), nullable=False, server_default="none"),
        sa.Column("plan_status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("shopify_charge_id", sa.String(), nullable=True),
        sa.Column("selected_model", sa.String(), nullable=False, server_default="claude-haiku-4-5"),
        sa.Column("trial_used", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("trial_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trial_ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("grace_period_ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("billing_cycle_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scheduled_plan_tier", sa.String(), nullable=True),
        sa.Column("scheduled_change_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("shop_owner_email", sa.String(), nullable=True),
        sa.Column("store_timezone", sa.String(), nullable=False, server_default="UTC"),
        sa.Column("digest_email_opt_in", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("digest_last_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_prompt_shown", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("installed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("uninstalled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("data_purge_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_generation_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("onboarding_dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_shops_shop_domain", "shops", ["shop_domain"], unique=True)

    op.create_table(
        "jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("shop_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("shops.id"), nullable=False),
        sa.Column("action_type", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_jobs_created_at", "jobs", ["created_at"])

    op.create_table(
        "usage_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("shop_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("shops.id"), nullable=False),
        sa.Column("app_id", sa.String(), nullable=False, server_default="giftsense"),
        sa.Column("action_type", sa.String(), nullable=False),
        sa.Column("generations_consumed", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("tokens_input", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens_output", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("model_used", sa.String(), nullable=False),
        sa.Column("cost_usd", sa.Numeric(10, 6), nullable=False, server_default="0"),
        sa.Column("prompt_version", sa.String(), nullable=False, server_default="v1"),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("jobs.id"), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_usage_logs_created_at", "usage_logs", ["created_at"])
    # Every limit check filters usage by (shop_id, created_at >= cycle start).
    op.create_index("ix_usage_logs_shop_id_created_at", "usage_logs", ["shop_id", "created_at"])

    op.create_table(
        "billing_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("shop_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("shops.id"), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("plan_tier", sa.String(), nullable=True),
        sa.Column("shopify_charge_id", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "processed_webhooks",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("webhook_id", sa.String(), nullable=False),
        sa.Column("topic", sa.String(), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_processed_webhooks_webhook_id", "processed_webhooks", ["webhook_id"], unique=True)

    op.create_table(
        "suppressed_emails",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("postmark_message_id", sa.String(), nullable=True),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("suppressed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_suppressed_emails_email", "suppressed_emails", ["email"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_suppressed_emails_email", table_name="suppressed_emails")
    op.drop_table("suppressed_emails")
    op.drop_index("ix_processed_webhooks_webhook_id", table_name="processed_webhooks")
    op.drop_table("processed_webhooks")
    op.drop_table("billing_events")
    op.drop_index("ix_usage_logs_shop_id_created_at", table_name="usage_logs")
    op.drop_index("ix_usage_logs_created_at", table_name="usage_logs")
    op.drop_table("usage_logs")
    op.drop_index("ix_jobs_created_at", table_name="jobs")
    op.drop_table("jobs")
    op.drop_index("ix_shops_shop_domain", table_name="shops")
    op.drop_table("shops")
    # The `vector` extension is left in place: other schemas may depend on it.
