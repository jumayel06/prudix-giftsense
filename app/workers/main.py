"""ARQ background worker.

Start with:
    arq app.workers.main.WorkerSettings

Foundation crons are copied from Prudix Commerce (see docs/SHOPIFY_PLAYBOOK.md
§7). Each cron isolates failures per shop, so one bad shop never aborts a
sweep, and ends with a `*_complete` log line carrying counts.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import structlog
from arq import cron, func
from arq.connections import RedisSettings
from sqlalchemy import delete, select

from app.config import GRACE_PERIOD_DAYS, derive_tier_from_subscription_name
from app.jobs import enqueue
from app.purge import purge_shop_data
from app.services import media
from app.workers.orders import annotate_gift_order, release_due_holds, sync_wrap
from app.workers.theme import check_theme
from app.workers.digest import send_weekly_digests
from app.workers.catalog import (
    catalog_analyze_shop, catalog_finish_bulk, catalog_start_sync, catalog_sync_product, kick_catalog_syncs,
    reconcile_catalogs,
)
from core.config import settings
from core.db.models import BillingEvent, GiftEvent, GiftSession, ProcessedWebhook, Shop
from core.db.session import AsyncSessionLocal
from core.shopify_auth import get_valid_access_token
from core.shopify_graphql import shopify_graphql_post

logger = structlog.get_logger()


async def startup(ctx: dict) -> None:
    logger.info("worker_starting")


async def shutdown(ctx: dict) -> None:
    logger.info("worker_stopping")


# ── Shared Shopify helpers ────────────────────────────────────────────────────

_ACTIVE_SUBSCRIPTIONS_QUERY = """
query {
  currentAppInstallation {
    activeSubscriptions {
      id
      name
      status
    }
  }
}
"""

# Sentinel: the API answered and there is no ACTIVE subscription.
NO_ACTIVE_SUBSCRIPTION = object()


async def _active_subscription(shop_domain: str, token: str):
    """Return the shop's ACTIVE AppSubscription dict, NO_ACTIVE_SUBSCRIPTION if
    Shopify answered with none, or None when there's no definitive answer
    (non-200 / GraphQL errors), in which case callers leave state alone."""
    resp = await shopify_graphql_post(shop_domain, token, _ACTIVE_SUBSCRIPTIONS_QUERY)
    if resp.status_code != 200:
        return None
    body = resp.json()
    if body.get("errors"):
        return None
    subs = ((body.get("data") or {}).get("currentAppInstallation") or {}).get("activeSubscriptions") or []
    for s in subs:
        if (s.get("status") or "").upper() == "ACTIVE":
            return s
    return NO_ACTIVE_SUBSCRIPTION


def _as_utc(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ── Crons ─────────────────────────────────────────────────────────────────────

async def cleanup_processed_webhooks(ctx: dict) -> None:
    """Delete webhook idempotency rows older than 72 hours."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=72)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            delete(ProcessedWebhook).where(ProcessedWebhook.received_at < cutoff)
        )
        await db.commit()
    logger.info("processed_webhooks_cleaned", deleted=result.rowcount)


async def reconcile_uninstalled_shops(ctx: dict) -> None:
    """Detect uninstalls that never delivered `app/uninstalled`.

    Webhook delivery is "at least once" in theory; in practice events go
    missing. Without this cron, a shop that uninstalled without a webhook stays
    active in our DB forever: stale tokens, blocked reinstalls, and no data
    retention timer.

    For each active-ish shop, ping the cheapest Admin GraphQL query. A 401 means
    the token was revoked: wait 5s and re-probe (Shopify auth can blip), and if
    it's still 401 run the same handler the webhook would have. Anything else
    (200, timeout, 5xx, network error) is a no-op, so healthy shops are never
    marked uninstalled.
    """
    from app.routes.webhooks import _handle_uninstalled

    async def _probe(shop_domain: str, token: str) -> int:
        resp = await shopify_graphql_post(shop_domain, token, "query { shop { id } }")
        return resp.status_code

    caught = 0
    async with AsyncSessionLocal() as db:
        shops = (await db.execute(
            select(Shop).where(
                Shop.plan_status.in_(["active", "trial_active", "grace", "pending", "frozen"]),
                Shop.access_token_encrypted != "",
            )
        )).scalars().all()
        for shop in shops:
            try:
                token = await get_valid_access_token(shop, db)
                if await _probe(shop.shop_domain, token) != 401:
                    continue
                await asyncio.sleep(5)
                if await _probe(shop.shop_domain, token) != 401:
                    logger.info("reconcile_401_recovered", shop=shop.shop_domain)
                    continue
                logger.info("reconcile_uninstall_detected", shop=shop.shop_domain)
                await _handle_uninstalled(shop.shop_domain, db, authoritative=True)
                caught += 1
            except Exception as e:  # noqa: BLE001 — per-shop defensive
                logger.warning(
                    "reconcile_uninstall_probe_failed",
                    shop=shop.shop_domain, error=str(e),
                )
    logger.info("reconcile_uninstalled_shops_complete", scanned=len(shops), caught=caught)


GIFT_EVENTS_RETENTION_DAYS = 90


GIFT_SESSIONS_RETENTION_DAYS = 90
NOTE_DRAFTS_RETENTION_DAYS = 30


async def purge_old_gift_sessions(ctx: dict, now: datetime | None = None) -> dict:
    """Retention promised in the privacy policy / data-protection answers:
    gift-finder sessions (brief, picks) 90 days after their last use, the AI
    note drafts kept on them 30 days. Ages by updated_at, so a session in use
    is never cut short."""
    from sqlalchemy import update
    now = now or datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        gone = await db.execute(delete(GiftSession).where(
            GiftSession.updated_at < now - timedelta(days=GIFT_SESSIONS_RETENTION_DAYS)))
        cleared = await db.execute(update(GiftSession).where(
            GiftSession.note_drafts.is_not(None),
            GiftSession.updated_at < now - timedelta(days=NOTE_DRAFTS_RETENTION_DAYS),
        ).values(note_drafts=None).execution_options(synchronize_session=False))
        await db.commit()
    result = {"sessions_deleted": gone.rowcount or 0, "drafts_cleared": cleared.rowcount or 0}
    logger.info("purge_old_gift_sessions_complete", **result)
    return result


async def purge_old_gift_events(ctx: dict) -> None:
    """Storefront analytics events are kept 90 days (docs/TECHNICAL_PLAN.md §7.1)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=GIFT_EVENTS_RETENTION_DAYS)
    async with AsyncSessionLocal() as db:
        result = await db.execute(delete(GiftEvent).where(GiftEvent.created_at < cutoff))
        await db.commit()
    logger.info("purge_old_gift_events_complete", deleted=result.rowcount or 0)


async def purge_expired_media(ctx: dict) -> None:
    """Voice/video messages: unlinked after 7 days, linked ones 90 days after
    delivery (app/services/media.py). Batched; leftovers go on the next run."""
    async with AsyncSessionLocal() as db:
        deleted = await media.purge_expired(db)
    logger.info("purge_expired_media_complete", deleted=deleted)


async def purge_uninstalled_shops(ctx: dict) -> None:
    """Delete all data for shops whose 30-day retention window has expired.

    Safety net for shops that never received (or whose app missed) the
    shop/redact GDPR webhook. Runs once daily.
    """
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        shops = (await db.execute(
            select(Shop).where(
                Shop.plan_status == "uninstalled",
                Shop.data_purge_at.is_not(None),
                Shop.data_purge_at <= now,
            )
        )).scalars().all()
        for shop in shops:
            await purge_shop_data(shop.id, db)
    logger.info("purge_uninstalled_shops_complete", purged=len(shops))


async def reconcile_scheduled_plan_changes(ctx: dict) -> None:
    """Safety net for deferred plan changes whose activation webhook was missed.

    A deferred downgrade is normally applied by the `app_subscriptions/update`
    webhook when Shopify activates the new plan at the cycle boundary. If that
    webhook is dropped, `scheduled_plan_tier` would stick forever and our
    records would disagree with Shopify's billing. Reconcile any shop at least
    an hour past its `scheduled_change_at` (so we never race the webhook)
    against Shopify's live active subscription.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=1)
    reconciled = 0
    async with AsyncSessionLocal() as db:
        shops = (await db.execute(
            select(Shop).where(
                Shop.scheduled_change_at.is_not(None),
                Shop.scheduled_change_at <= cutoff,
                Shop.access_token_encrypted != "",
            )
        )).scalars().all()

        for shop in shops:
            try:
                token = await get_valid_access_token(shop, db)
                active = await _active_subscription(shop.shop_domain, token)
                if active is None or active is NO_ACTIVE_SUBSCRIPTION:
                    # No definitive answer, or no active subscription (the
                    # cancel/uninstall paths own those states). Try next run.
                    continue

                actual_tier = derive_tier_from_subscription_name(
                    active.get("name"), fallback=shop.plan_tier,
                )
                active_charge_id = str(active.get("id") or "").rsplit("/", 1)[-1]
                # The switch happened if Shopify's live subscription differs from
                # ours by tier OR by charge. The charge id must follow too:
                # otherwise every later webhook for the real subscription
                # (CANCELLED, FROZEN, …) looks stale and is ignored.
                # (Commerce billing audit 41f8b2f.)
                if actual_tier != shop.plan_tier or (
                    active_charge_id and active_charge_id != (shop.shopify_charge_id or "")
                ):
                    # The activation webhook was missed: apply the switch now,
                    # anchoring the new cycle at the boundary Shopify actually
                    # switched so the quota window isn't stretched.
                    shop.plan_tier = actual_tier
                    if active_charge_id:
                        shop.shopify_charge_id = active_charge_id
                    shop.plan_status = "active"
                    shop.grace_period_ends_at = None
                    shop.billing_cycle_start = _as_utc(shop.scheduled_change_at) or now
                    db.add(BillingEvent(
                        id=uuid.uuid4(),
                        shop_id=shop.id,
                        event_type="change_reconciled",
                        plan_tier=actual_tier,
                        shopify_charge_id=str(shop.shopify_charge_id or ""),
                    ))
                    logger.info(
                        "scheduled_change_reconciled",
                        shop=shop.shop_domain, applied_tier=actual_tier,
                    )
                    reconciled += 1

                # Applied here or by the webhook: the pending change is resolved.
                shop.scheduled_plan_tier = None
                shop.scheduled_change_at = None
                await db.commit()
            except Exception as e:  # noqa: BLE001 — per-shop defensive
                await db.rollback()
                logger.warning(
                    "scheduled_change_reconcile_failed",
                    shop=shop.shop_domain, error=str(e),
                )

    logger.info(
        "reconcile_scheduled_plan_changes_complete",
        scanned=len(shops), reconciled=reconciled,
    )


async def reconcile_trial_conversions(ctx: dict) -> None:
    """Safety net for trials whose conversion (or expiry) webhook was missed.

    Trial → paid relies on Shopify's `app_subscriptions/update` ACTIVE webhook
    after the trial ends. Commerce had no fallback for a dropped one (listed as
    tech debt 2026-09-19): the shop would stay `trial_active` on trial limits
    forever. For shops at least an hour past `trial_ends_at`, check Shopify's
    live subscription:
      - ACTIVE subscription → converted: `active`, cycle anchored at trial end.
      - No active subscription → the trial ended unpaid: `expired` with the
        same 7-day read-only grace the EXPIRED webhook applies.
      - No definitive answer → leave it for the next run.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=1)
    converted = 0
    expired = 0
    async with AsyncSessionLocal() as db:
        shops = (await db.execute(
            select(Shop).where(
                Shop.plan_status == "trial_active",
                Shop.trial_ends_at.is_not(None),
                Shop.trial_ends_at <= cutoff,
                Shop.access_token_encrypted != "",
            )
        )).scalars().all()

        for shop in shops:
            try:
                token = await get_valid_access_token(shop, db)
                active = await _active_subscription(shop.shop_domain, token)
                if active is None:
                    continue
                if active is NO_ACTIVE_SUBSCRIPTION:
                    shop.plan_status = "expired"
                    shop.grace_period_ends_at = now + timedelta(days=GRACE_PERIOD_DAYS)
                    event_type = "trial_expired_reconciled"
                    expired += 1
                else:
                    shop.plan_status = "active"
                    shop.plan_tier = derive_tier_from_subscription_name(
                        active.get("name"), fallback=shop.plan_tier,
                    )
                    shop.shopify_charge_id = str(active.get("id", "")).rsplit("/", 1)[-1] or shop.shopify_charge_id
                    shop.grace_period_ends_at = None
                    shop.billing_cycle_start = _as_utc(shop.trial_ends_at) or now
                    event_type = "trial_converted_reconciled"
                    converted += 1
                db.add(BillingEvent(
                    id=uuid.uuid4(),
                    shop_id=shop.id,
                    event_type=event_type,
                    plan_tier=shop.plan_tier,
                    shopify_charge_id=str(shop.shopify_charge_id or ""),
                ))
                await db.commit()
                logger.info("trial_reconciled", shop=shop.shop_domain, outcome=event_type)
                if event_type == "trial_converted_reconciled":
                    # The plan's full product limit applies now.
                    await enqueue("catalog_start_sync", str(shop.id), "trial_converted")
            except Exception as e:  # noqa: BLE001 — per-shop defensive
                await db.rollback()
                logger.warning(
                    "trial_reconcile_failed",
                    shop=shop.shop_domain, error=str(e),
                )

    logger.info(
        "reconcile_trial_conversions_complete",
        scanned=len(shops), converted=converted, expired=expired,
    )


# ARQ's own log lines ("Starting worker for N functions", job start/finish)
# go to stderr by default, and Railway tags every stderr line as severity
# "error". Same format as ARQ's default, but on stdout, so real errors stand
# out. Used via `arq ... --custom-log-dict app.workers.main.ARQ_LOG_CONFIG`.
ARQ_LOG_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "arq.standard": {"level": "INFO", "class": "logging.StreamHandler",
                         "formatter": "arq.standard", "stream": "ext://sys.stdout"},
    },
    "formatters": {"arq.standard": {"format": "%(asctime)s: %(message)s", "datefmt": "%H:%M:%S"}},
    "loggers": {"arq": {"handlers": ["arq.standard"], "level": "INFO"}},
}


class WorkerSettings:
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    on_startup = startup
    on_shutdown = shutdown
    max_jobs = 10
    job_timeout = 60
    max_tries = 3
    keep_result = 3600
    # Heartbeat in Redis (arq:queue:health-check) every minute, so /admin/system
    # can tell a live worker from a stopped one (ARQ's default is hourly).
    health_check_interval = 60

    # Analyzing a catalog is many LLM + embedding calls: allow up to an hour.
    # Product jobs keep no result so a later update to the same product (same
    # _job_id) can be queued again as soon as the previous one ran.
    functions = [
        func(catalog_start_sync, timeout=120),
        func(catalog_finish_bulk, timeout=3600, max_tries=1),
        func(catalog_sync_product, timeout=300, keep_result=0),
        func(catalog_analyze_shop, timeout=3600, keep_result=0),
        func(annotate_gift_order, timeout=60),
        func(sync_wrap, timeout=120),
        func(check_theme, timeout=60, keep_result=0),
    ]

    cron_jobs = [
        cron(cleanup_processed_webhooks, hour={0, 6, 12, 18}, minute=0),
        # Missed `app/uninstalled` safety net. 01:00 UTC, before the purge.
        cron(reconcile_uninstalled_shops, hour=1, minute=0),
        # Missed deferred-plan-change webhook safety net. Hourly at :20.
        cron(reconcile_scheduled_plan_changes, minute=20),
        # Missed trial conversion/expiry webhook safety net. Hourly at :40.
        cron(reconcile_trial_conversions, minute=40),
        cron(purge_uninstalled_shops, hour=3, minute=0),
        cron(purge_old_gift_events, hour=3, minute=30),
        cron(purge_old_gift_sessions, hour=3, minute=40),
        cron(purge_expired_media, hour={3, 15}, minute=45, timeout=600),
        # Weekly GiftSense email (Growth+), Mondays 13:00 UTC.
        cron(send_weekly_digests, weekday=0, hour=13, minute=0, timeout=1800),
        # Arrive-by: release fulfillment holds on their ship-by day. Hourly at :05.
        cron(release_due_holds, minute=5, timeout=600),
        # First catalog sync for newly active shops + missed bulk_operations/finish.
        cron(kick_catalog_syncs, minute=set(range(0, 60, 5)), timeout=3600),
        # Nightly full re-export (missed product webhooks, deletions, upgrades).
        cron(reconcile_catalogs, hour=2, minute=30),
    ]
