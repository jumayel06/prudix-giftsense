"""SQLAlchemy models.

Foundation tables (shops, usage_logs, jobs, billing_events, processed_webhooks,
suppressed_emails) are copied from Prudix Commerce, trimmed to what GiftSense
uses. GiftSense feature tables are added below them as each feature ships.
Every table with a `shop_id` column must also be purged in app/purge.py
(enforced by tests/integration/test_purge_completeness.py).
"""
import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Index, Integer, JSON, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from core.db.types import Embedding


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
    # Merchant-selected AI tier ("standard" / "advanced" / "premium", app/ai_models.py);
    # must be in PLANS[plan_tier]["ai_tiers"]. Named selected_model for Commerce parity.
    selected_model: Mapped[str] = mapped_column(String, default="standard")
    # Admin override per slot, e.g. {"ai_premium": "claude-sonnet-5"}: holds a
    # shop on a model during a rollout. Retired pins are ignored.
    model_pins: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Paid AI re-reads of edited products this billing cycle, capped by
    # PLANS[tier]["product_rereads_per_month"]; reset when the cycle changes.
    catalog_rereads_used: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    catalog_rereads_cycle_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Merchant gift settings by section, e.g. {"notes": {tone, max_chars, banned_words}}
    # (app/services/gift_settings.py applies defaults).
    gift_settings: Mapped[dict | None] = mapped_column(JSON, nullable=True)
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


# ── Gift catalog (week 3) ─────────────────────────────────────────────────────

class CatalogProductRow(Base):
    """One Shopify product as the gift finder sees it: synced fields, the AI
    gift profile, merchant overrides and the profile's embedding.

    `content_hash` covers only fields that change what the product *is*
    (title, description, type, tags…); when it changes the profile is re-read
    by AI. Price and stock changes update in place for free.
    """
    __tablename__ = "catalog_products"
    __table_args__ = (
        UniqueConstraint("shop_id", "product_id", name="uq_catalog_products_shop_product"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    shop_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("shops.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    product_id: Mapped[str] = mapped_column(String, nullable=False)  # numeric Shopify id as text
    handle: Mapped[str | None] = mapped_column(String, nullable=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    product_type: Mapped[str] = mapped_column(String, nullable=False, default="", server_default="")
    vendor: Mapped[str] = mapped_column(String, nullable=False, default="", server_default="")
    tags: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    price_min: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    price_max: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    image_url: Mapped[str | None] = mapped_column(String, nullable=True)
    url: Mapped[str | None] = mapped_column(String, nullable=True)
    content_hash: Mapped[str] = mapped_column(String, nullable=False)

    gift_profile: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    profile_version: Mapped[str | None] = mapped_column(String, nullable=True)
    profile_hash: Mapped[str | None] = mapped_column(String, nullable=True)  # content_hash the profile was built from
    profile_fallback: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    merchant_overrides: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    excluded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    embedding: Mapped[list | None] = mapped_column(Embedding(512), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String, nullable=True)

    enriched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False,
    )


class CatalogSync(Base):
    """One catalog sync run (initial after install, nightly reconcile) with
    progress counters for the dashboard's "Analyzing your catalog…" bar."""
    __tablename__ = "catalog_syncs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    shop_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("shops.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    kind: Mapped[str] = mapped_column(String, nullable=False)        # "initial" | "reconcile"
    status: Mapped[str] = mapped_column(String, nullable=False, default="running")  # running | done | failed
    bulk_operation_id: Mapped[str | None] = mapped_column(String, nullable=True)
    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    processed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    enriched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class GiftSession(Base):
    """One storefront widget session (random `sid` from the shopper's browser,
    never customer data): the last brief and final picks, and how many AI
    searches it ran. Feeds refine, cart/order attribution (`_giftsense_sid`)
    and analytics. Deleted with the shop's data (app/purge.py)."""
    __tablename__ = "gift_sessions"
    __table_args__ = (UniqueConstraint("shop_id", "sid", name="uq_gift_sessions_shop_sid"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    shop_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("shops.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    sid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    intake: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    last_picks: Mapped[list] = mapped_column(JSON, nullable=False, default=list)   # product ids, best first
    searches: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    refines: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    # {gift_key: {"count": drafts so far, "last": last AI/template draft}}; gift_key
    # is the product id or "order". The last draft is compared with the order's
    # final note to measure draft acceptance.
    note_drafts: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False,
    )


class GiftOrder(Base):
    """A Shopify order that carried GiftSense gift data (orders/create). Order
    facts only: never the customer, never the note text (it stays on the
    order). `sid` links to the gift_sessions row for attribution. Registered in
    app/services/gdpr.py (order id) and deleted with the shop (app/purge.py)."""
    __tablename__ = "gift_orders"
    __table_args__ = (UniqueConstraint("shop_id", "order_id", name="uq_gift_orders_shop_order"),
                      Index("ix_gift_orders_hold_due", "hold_status", "ship_by"))

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    shop_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("shops.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    order_id: Mapped[str] = mapped_column(String, nullable=False)          # numeric Shopify id as text
    order_name: Mapped[str] = mapped_column(String, nullable=False, default="")
    sid: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    delivery_mode: Mapped[str | None] = mapped_column(String, nullable=True)   # direct | self
    gift_lines: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    gift_revenue: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    order_total: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String, nullable=False, default="")
    groups: Mapped[list] = mapped_column(JSON, nullable=False, default=list)     # labels/wrap/message, no notes
    note_source: Mapped[str | None] = mapped_column(String, nullable=True)      # ai_accepted | ai_edited | manual
    annotated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    # Arrive-by (Growth+): requested date, the ship-by date we computed, and the
    # fulfillment hold: held | released | late (couldn't be met, not held) |
    # none (nothing we could hold, e.g. 3PL) | failed.
    arrive_by: Mapped[date | None] = mapped_column(Date, nullable=True)
    ship_by: Mapped[date | None] = mapped_column(Date, nullable=True)
    hold_status: Mapped[str | None] = mapped_column(String, nullable=True)
    # Registry purchases on this order: the registry and those lines' revenue.
    registry_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    registry_revenue: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class GiftEvent(Base):
    """Storefront widget events (analytics beacon), kept 90 days
    (purge_old_gift_events cron). Only the random widget `sid` and a product
    id: no customer data."""
    __tablename__ = "gift_events"
    __table_args__ = (Index("ix_gift_events_shop_created", "shop_id", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    shop_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("shops.id", ondelete="CASCADE"), nullable=False)
    sid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)
    product_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class OrderCountDaily(Base):
    """Every order (gift or not) per shop per day, from orders/create: the
    denominator for "gift orders vs all orders"."""
    __tablename__ = "order_counts_daily"
    __table_args__ = (UniqueConstraint("shop_id", "day", name="uq_order_counts_daily_shop_day"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    shop_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("shops.id", ondelete="CASCADE"), nullable=False)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    orders: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    revenue: Mapped[float] = mapped_column(Numeric(14, 2), nullable=False, default=0)


class GiftMedia(Base):
    """A shopper's voice or video message (docs/TECHNICAL_PLAN.md §6.6). The
    file lives in R2 at `storage_key`; the row holds no customer data.

    `token` goes into the cart (the gift group's `message`, or the
    `_giftsense_message` attribute) and links the recording to its order on
    orders/create. `view_token` is the unguessable id of the recipient page.
    status: pending (upload URL issued) → uploaded (confirmed in R2) → linked
    (on an order; `expires_at` set). Unlinked ones are purged after 7 days,
    linked ones at `expires_at` (app/services/media.py). Registered in
    app/services/gdpr.py (order id) and deleted with the shop (app/purge.py)."""
    __tablename__ = "gift_media"
    __table_args__ = (Index("ix_gift_media_purge", "status", "created_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    shop_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("shops.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    token: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    view_token: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    sid: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    order_id: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)                 # voice | video
    storage_key: Mapped[str] = mapped_column(String, nullable=False)
    mime: Mapped[str] = mapped_column(String, nullable=False)
    bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_s: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    view_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Registry(Base):
    """A shopper's gift registry (Pro, docs/TECHNICAL_PLAN.md §6.9). The owner
    is the App Proxy's `logged_in_customer_id`, the only customer data we
    keep (no name, email or address). One registry per customer per shop.
    Guests open it by `share_token`. Registered in app/services/gdpr.py
    (customer id) and deleted with the shop (app/purge.py)."""
    __tablename__ = "registries"
    __table_args__ = (UniqueConstraint("shop_id", "customer_id", name="uq_registries_shop_customer"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    shop_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("shops.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    customer_id: Mapped[str] = mapped_column(String, nullable=False)
    share_token: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String, nullable=False, default="")
    occasion: Mapped[str] = mapped_column(String, nullable=False, default="just_because")
    event_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # AI suggestions, cached for the day: {"day": "YYYY-MM-DD", "picks": [...]}
    suggestions: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    views: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class RegistryItem(Base):
    """One wanted product variant on a registry. Display fields are a snapshot
    taken when it was added (title/variant/image/price), so the guest page
    renders without Admin API calls; carts use the live price."""
    __tablename__ = "registry_items"
    __table_args__ = (UniqueConstraint("registry_id", "variant_id", name="uq_registry_items_variant"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    registry_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("registries.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    shop_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("shops.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    product_id: Mapped[str] = mapped_column(String, nullable=False)
    variant_id: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False, default="")
    variant_title: Mapped[str] = mapped_column(String, nullable=False, default="")
    image_url: Mapped[str | None] = mapped_column(String, nullable=True)
    url: Mapped[str | None] = mapped_column(String, nullable=True)
    price: Mapped[float] = mapped_column(Numeric(12, 2), nullable=False, default=0)
    wanted_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    bought_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class SupportTicket(Base):
    """A merchant's support request from the dashboard (POST /api/support/ticket),
    ported from Prudix Commerce. Emailed to support@prudix.app; handled in
    /admin/support. Deleted with the shop (app/purge.py)."""
    __tablename__ = "support_tickets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ticket_number: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    shop_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("shops.id", ondelete="CASCADE"), nullable=False, index=True,
    )
    category: Mapped[str] = mapped_column(String, nullable=False)          # bug | billing | feature | other
    subject: Mapped[str] = mapped_column(String, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    screenshot_url: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="open", server_default="open")  # open | in_progress | resolved
    admin_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
