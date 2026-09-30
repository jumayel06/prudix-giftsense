"""
Shared helper for purging all data associated with an uninstalled shop
(copied from Prudix Commerce).

Called by the 30-day `purge_uninstalled_shops` cron and by the `shop/redact`
GDPR webhook. Every table with a `shop_id` column must be deleted here:
tests/integration/test_purge_completeness.py fails CI otherwise.

Deletion order respects FK constraints (children before parents):
  usage_logs → jobs
  billing_events
  catalog_products, catalog_syncs, gift_sessions, gift_orders, gift_events, order_counts_daily
  then anonymise the shop row (kept so trial_used=True survives reinstall)
"""

import uuid

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db.models import (
    BillingEvent, CatalogProductRow, CatalogSync, GiftEvent, GiftOrder, GiftSession, Job, OrderCountDaily, Shop,
    UsageLog,
)

logger = structlog.get_logger()


async def purge_shop_data(shop_id: uuid.UUID, db: AsyncSession) -> None:
    """Delete all data for `shop_id` and anonymise the shop row."""
    await db.execute(delete(UsageLog).where(UsageLog.shop_id == shop_id))
    await db.execute(delete(Job).where(Job.shop_id == shop_id))
    await db.execute(delete(BillingEvent).where(BillingEvent.shop_id == shop_id))
    await db.execute(delete(CatalogProductRow).where(CatalogProductRow.shop_id == shop_id))
    await db.execute(delete(CatalogSync).where(CatalogSync.shop_id == shop_id))
    await db.execute(delete(GiftSession).where(GiftSession.shop_id == shop_id))
    await db.execute(delete(GiftOrder).where(GiftOrder.shop_id == shop_id))
    await db.execute(delete(GiftEvent).where(GiftEvent.shop_id == shop_id))
    await db.execute(delete(OrderCountDaily).where(OrderCountDaily.shop_id == shop_id))

    # Anonymise shop row — keep it so trial_used=True survives a future reinstall
    result = await db.execute(select(Shop).where(Shop.id == shop_id))
    shop = result.scalar_one_or_none()
    if shop:
        shop.access_token_encrypted = ""
        shop.refresh_token_encrypted = None
        shop.access_token_expires_at = None
        shop.refresh_token_expires_at = None
        shop.shopify_charge_id = None
        shop.billing_cycle_start = None
        shop.trial_started_at = None
        shop.trial_ends_at = None
        shop.grace_period_ends_at = None
        shop.scheduled_plan_tier = None
        shop.scheduled_change_at = None
        shop.data_purge_at = None
        shop.shop_owner_email = None
        shop.plan_tier = "none"
        shop.plan_status = "purged"
        shop.selected_model = "standard"  # AI tier
        shop.review_prompt_shown = False

    await db.commit()
    logger.info("shop_data_purged", shop_id=str(shop_id))
