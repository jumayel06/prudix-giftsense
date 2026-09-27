"""SQLAlchemy models.

Foundation tables (shops, usage_logs, jobs, billing_events, processed_webhooks,
suppressed_emails) are copied from Prudix Commerce, trimmed to what GiftSense
uses. GiftSense feature tables are added below them as each feature ships.
Every table with a `shop_id` column must also be purged in app/purge.py
(enforced by tests/integration/test_purge_completeness.py).
"""
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, JSON, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


class Shop(Base):
    __tablename__ = "shops"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    shop_domain: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    access_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    refresh_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Lifecycle: pending → trial_active → active → cancelled / expired / declined
    # → uninstalled → purged. See docs/SHOPIFY_PLAYBOOK.md.
    plan_tier: Mapped[str] = mapped_column(String, default="none")
    plan_status: Mapped[str] = mapped_column(String, default="pending")
    shopify_charge_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Merchant-selected AI model; must be in PLANS[tier]["models_available"].
    selected_model: Mapped[str] = mapped_column(String, default="claude-haiku-4-5")
    trial_used: Mapped[bool] = mapped_column(Boolean, default=False)
    trial_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    trial_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    grace_period_ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    billing_cycle_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Deferred plan change (downgrade / same-tier switch). Shopify keeps the
    # merchant on their current plan until the cycle ends, then activates the
    # new one (applied via the app_subscriptions/update webhook, with
    # reconcile_scheduled_plan_changes as the safety net). These record the
    # pending change so the UI can show a "plan changes on X" banner.
    scheduled_plan_tier: Mapped[str | None] = mapped_column(String, nullable=True)
    scheduled_change_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Shop owner's contact email, fetched from `shop { email }` at install.
    # The weekly digest targets this; `owner@<shop_domain>` would bounce.
    shop_owner_email: Mapped[str | None] = mapped_column(String, nullable=True)
    store_timezone: Mapped[str] = mapped_column(String, default="UTC")
    digest_email_opt_in: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    digest_last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_prompt_shown: Mapped[bool] = mapped_column(Boolean, default=False)

    installed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    uninstalled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    data_purge_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_generation_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    onboarding_dismissed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    usage_logs: Mapped[list["UsageLog"]] = relationship(back_populates="shop")
    jobs: Mapped[list["Job"]] = relationship(back_populates="shop")


class UsageLog(Base):
    """One row per AI generation attempt. Refund pattern (from Commerce):
    +weight upfront, then (0, tokens) on success or (-weight, 0) on failure.
    Count usage with SUM(generations_consumed), never COUNT()."""
    __tablename__ = "usage_logs"
    # Every limit check filters usage by (shop_id, created_at >= cycle start).
    __table_args__ = (Index("ix_usage_logs_shop_id_created_at", "shop_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    shop_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("shops.id"), nullable=False)
    app_id: Mapped[str] = mapped_column(String, default="giftsense")
    action_type: Mapped[str] = mapped_column(String, nullable=False)
    generations_consumed: Mapped[int] = mapped_column(Integer, default=1)
    tokens_input: Mapped[int] = mapped_column(Integer, default=0)
    tokens_output: Mapped[int] = mapped_column(Integer, default=0)
    model_used: Mapped[str] = mapped_column(String, nullable=False)
    cost_usd: Mapped[float] = mapped_column(Numeric(10, 6), default=0)
    prompt_version: Mapped[str] = mapped_column(String, default="v1")
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("jobs.id"), nullable=True)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    shop: Mapped["Shop"] = relationship(back_populates="usage_logs")


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    shop_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("shops.id"), nullable=False)
    action_type: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, default="pending")
    priority: Mapped[int] = mapped_column(Integer, default=0)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    shop: Mapped["Shop"] = relationship(back_populates="jobs")


class BillingEvent(Base):
    __tablename__ = "billing_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    shop_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("shops.id"), nullable=False)
    event_type: Mapped[str] = mapped_column(String, nullable=False)
    plan_tier: Mapped[str | None] = mapped_column(String, nullable=True)
    shopify_charge_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProcessedWebhook(Base):
    """Webhook idempotency, keyed on X-Shopify-Webhook-Id. Global (no shop_id);
    rows older than 72h are deleted by cleanup_processed_webhooks."""
    __tablename__ = "processed_webhooks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    webhook_id: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    topic: Mapped[str] = mapped_column(String, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SuppressedEmail(Base):
    """Global email suppression list, filled by Postmark's Bounce and
    SpamComplaint webhook (POST /webhooks/postmark). Global rather than
    per-shop, matching Postmark's server-token-scoped suppression."""
    __tablename__ = "suppressed_emails"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Lowercased for case-insensitive lookups.
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    # "hard_bounce" | "spam_complaint" | "manual"
    reason: Mapped[str] = mapped_column(String, nullable=False)
    postmark_message_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Postmark's RecordType ("Bounce" / "SpamComplaint") or "manual".
    source: Mapped[str] = mapped_column(String, nullable=False)
    suppressed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
