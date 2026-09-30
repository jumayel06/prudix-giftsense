"""Shopify + Postmark webhooks (copied from Prudix Commerce).

All Shopify topics POST to /webhooks; the three GDPR compliance topics have
their own endpoints. HMAC is required everywhere (App Store review finding on
Commerce, commit ad253d0). Topics must match the toml [[webhooks.subscriptions]]
1:1 — see docs/SHOPIFY_PLAYBOOK.md §5 and §8.
"""
import base64
import hashlib
import hmac
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import (
    PLAN_DEFAULT_AI_TIER,
    CYCLE_DAYS,
    DATA_RETENTION_DAYS,
    GRACE_PERIOD_DAYS,
    PLANS,
)
from app.jobs import enqueue
from app.services.gift_orders import metafield_value, note_source, parse_gift_order
from core.config import settings
from core.db.models import (
    BillingEvent, CatalogProductRow, GiftOrder, GiftSession, OrderCountDaily, ProcessedWebhook, Shop, SuppressedEmail,
)
from core.db.session import get_db

logger = structlog.get_logger()
router = APIRouter()


def _verify_hmac(body: bytes, signature: str) -> bool:
    digest = hmac.new(
        settings.shopify_api_secret.encode("utf-8"), body, hashlib.sha256
    ).digest()
    computed = base64.b64encode(digest).decode()
    return hmac.compare_digest(computed, signature or "")


def _require_hmac(body: bytes, signature: str) -> None:
    if not _verify_hmac(body, signature):
        raise HTTPException(status_code=401, detail="HMAC verification failed")


async def _mark_processed(webhook_id: str, topic: str, db: AsyncSession) -> bool:
    """Returns True if already processed (duplicate). Otherwise records it."""
    result = await db.execute(
        select(ProcessedWebhook).where(ProcessedWebhook.webhook_id == webhook_id)
    )
    if result.scalar_one_or_none():
        return True
    db.add(ProcessedWebhook(id=uuid.uuid4(), webhook_id=webhook_id, topic=topic))
    await db.commit()
    return False


@router.post("/webhooks")
async def handle_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_shopify_topic: str = Header(...),
    x_shopify_hmac_sha256: str = Header(...),
    x_shopify_shop_domain: str = Header(...),
    x_shopify_webhook_id: Optional[str] = Header(default=None),
    x_shopify_triggered_at: Optional[str] = Header(default=None),
):
    body = await request.body()
    logger.info("webhook_arrived", topic=x_shopify_topic, shop=x_shopify_shop_domain)
    _require_hmac(body, x_shopify_hmac_sha256)

    # Use the Shopify-provided ID if available; fall back to a deterministic hash
    # of topic + shop + body so a redelivered header-less webhook still dedupes
    # (Commerce's random uuid4 fallback never deduped; fixed there 2026-09-27).
    webhook_id = x_shopify_webhook_id or "sha256:" + hashlib.sha256(
        x_shopify_topic.encode() + b"|" + x_shopify_shop_domain.encode() + b"|" + body
    ).hexdigest()

    if await _mark_processed(webhook_id, x_shopify_topic, db):
        return {"ok": True, "duplicate": True}

    try:
        payload = json.loads(body) if body else {}
    except Exception:
        payload = {}

    topic = x_shopify_topic
    shop_domain = x_shopify_shop_domain
    logger.info("webhook_received", topic=topic, shop=shop_domain)

    if topic == "app/uninstalled":
        await _handle_uninstalled(shop_domain, db, x_shopify_triggered_at)
    elif topic == "app_subscriptions/update":
        await _handle_subscription_update(shop_domain, payload, db)
    elif topic in ("products/create", "products/update"):
        await _handle_product_changed(shop_domain, payload, db)
    elif topic == "products/delete":
        await _handle_product_deleted(shop_domain, payload, db)
    elif topic == "bulk_operations/finish":
        await _handle_bulk_finished(shop_domain, payload, db)
    elif topic == "orders/create":
        await _handle_order_created(shop_domain, payload, db)

    return {"ok": True}


# ── GDPR mandatory endpoints ──────────────────────────────────────────────────

async def _gdpr_shop(payload: dict, db: AsyncSession) -> Optional[Shop]:
    shop_domain = payload.get("shop_domain", "")
    if not shop_domain:
        return None
    result = await db.execute(select(Shop).where(Shop.shop_domain == shop_domain))
    return result.scalar_one_or_none()


@router.post("/webhooks/gdpr/customers/data_request")
async def gdpr_data_request(
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_shopify_hmac_sha256: str = Header(...),
):
    """Collect everything held for the customer (rows keyed by customer / order
    ID — see app/services/gdpr.py) and email it to the store owner, who answers
    the shopper. Shopify doesn't read the response body. Ported from Commerce."""
    from app.services import postmark_client
    from app.services.gdpr import collect_customer_data, render_data_request_email

    body = await request.body()
    _require_hmac(body, x_shopify_hmac_sha256)
    payload = json.loads(body) if body else {}
    shop = await _gdpr_shop(payload, db)
    if not shop:
        return {"ok": True}

    data = await collect_customer_data(shop.id, payload, db)
    logger.info(
        "gdpr_customer_data_request",
        shop=shop.shop_domain,
        data_request_id=(payload.get("data_request") or {}).get("id"),
        tables={k: len(v) for k, v in data.items()},
    )
    if not shop.shop_owner_email:
        # Surfaces in Sentry — must be answered manually within 30 days.
        logger.error("gdpr_customer_data_request_no_owner_email", shop=shop.shop_domain)
        return {"ok": True}
    subject, html_body, text_body = render_data_request_email(shop.shop_domain, payload, data)
    try:
        await postmark_client.send_email(
            to_email=shop.shop_owner_email, subject=subject,
            html_body=html_body, text_body=text_body, tag="gdpr-data-request",
        )
    except Exception as e:
        # 500 → Shopify retries the webhook, so the request isn't silently lost.
        logger.error("gdpr_customer_data_request_email_failed", shop=shop.shop_domain, error=str(e))
        raise HTTPException(status_code=500, detail="Could not deliver data request")
    return {"ok": True}


@router.post("/webhooks/gdpr/customers/redact")
async def gdpr_customers_redact(
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_shopify_hmac_sha256: str = Header(...),
):
    """Delete every row tied to the customer + orders_to_redact (customer and
    order IDs count as personal data — see app/services/gdpr.py). Errors
    propagate as 500 so Shopify retries rather than silently skipping."""
    from app.services import gdpr

    body = await request.body()
    _require_hmac(body, x_shopify_hmac_sha256)
    payload = json.loads(body) if body else {}
    shop = await _gdpr_shop(payload, db)
    if not shop:
        return {"ok": True}
    counts = await gdpr.redact_customer(shop.id, payload, db)
    logger.info("gdpr_customer_redact_complete", shop=shop.shop_domain, deleted=counts)
    return {"ok": True}


# ── Postmark bounce / complaint webhook ───────────────────────────────────────

def _verify_postmark_basic_auth(header: str | None) -> bool:
    """Constant-time compare against POSTMARK_WEBHOOK_USER/PASSWORD.

    Postmark's webhook config uses Basic Auth (username + password in the
    Authorization header). When neither env var is set (dev), we accept
    unauthenticated calls so local `curl` replays work. In prod, missing
    or wrong credentials → 401.
    """
    expected_user = settings.postmark_webhook_user or ""
    expected_pass = settings.postmark_webhook_password or ""
    if not expected_user and not expected_pass:
        return True  # dev / unconfigured — no auth enforced
    if not header or not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header[6:]).decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return False
    if ":" not in decoded:
        return False
    user, _, password = decoded.partition(":")
    return (
        hmac.compare_digest(user, expected_user)
        and hmac.compare_digest(password, expected_pass)
    )


@router.post("/webhooks/postmark")
async def postmark_webhook(
    request: Request,
    db: AsyncSession = Depends(get_db),
    authorization: Optional[str] = Header(default=None),
):
    """Ingest Postmark Bounce + SpamComplaint events into `suppressed_emails`.

    Postmark posts one event per webhook call (not batched). RecordType
    dispatches:
      - "Bounce" with Type == "HardBounce" → suppress (reason=hard_bounce)
      - "SpamComplaint" → suppress (reason=spam_complaint)
      - Everything else (SoftBounce, Transient, Delivery, Open, Click,
        SubscriptionChange) → 200 OK, no-op. Soft/Transient bounces
        retry themselves; we only care about permanent failures.

    Idempotent via the Postmark MessageID (unique per event).
    """
    if not _verify_postmark_basic_auth(authorization):
        raise HTTPException(status_code=401, detail="Postmark auth failed")

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    record_type = (payload.get("RecordType") or "").strip()
    email = (payload.get("Email") or "").strip().lower()
    message_id = payload.get("MessageID") or ""

    if not email:
        logger.info("postmark_webhook_no_email", record_type=record_type)
        return {"ok": True, "ignored": "no_email"}

    reason: str | None = None
    if record_type == "Bounce":
        # HardBounce = TypeCode 1 or Type == "HardBounce".
        bounce_type = (payload.get("Type") or "").strip()
        if bounce_type == "HardBounce" or payload.get("TypeCode") == 1:
            reason = "hard_bounce"
    elif record_type == "SpamComplaint":
        reason = "spam_complaint"

    if reason is None:
        logger.info(
            "postmark_webhook_ignored",
            record_type=record_type, email=email, bounce_type=payload.get("Type"),
        )
        return {"ok": True, "ignored": record_type or "unknown"}

    existing = (await db.execute(
        select(SuppressedEmail).where(SuppressedEmail.email == email)
    )).scalar_one_or_none()
    if existing:
        logger.info(
            "postmark_webhook_duplicate",
            email=email, reason=reason, existing_reason=existing.reason,
        )
        return {"ok": True, "duplicate": True}

    db.add(SuppressedEmail(
        id=uuid.uuid4(),
        email=email,
        reason=reason,
        postmark_message_id=str(message_id) if message_id else None,
        source=record_type,
    ))
    await db.commit()
    logger.info(
        "postmark_webhook_suppressed",
        email=email, reason=reason, message_id=str(message_id),
    )
    return {"ok": True, "suppressed": True, "reason": reason}


@router.post("/webhooks/gdpr/shop/redact")
async def gdpr_shop_redact(
    request: Request,
    db: AsyncSession = Depends(get_db),
    x_shopify_hmac_sha256: str = Header(...),
):
    """Purge all shop data on Shopify's GDPR deletion request (~48h after uninstall)."""
    from app.purge import purge_shop_data
    body = await request.body()
    _require_hmac(body, x_shopify_hmac_sha256)
    try:
        payload = json.loads(body) if body else {}
        shop_domain = payload.get("shop_domain", "")
        if shop_domain:
            result = await db.execute(select(Shop).where(Shop.shop_domain == shop_domain))
            shop = result.scalar_one_or_none()
            # Only purge if still uninstalled — skip if merchant reinstalled
            if shop and shop.plan_status == "uninstalled":
                await purge_shop_data(shop.id, db)
                logger.info("gdpr_shop_redact_complete", shop=shop_domain)
    except Exception as e:
        logger.error("gdpr_shop_redact_error", error=str(e))
    return {"ok": True}


# ── Internal handlers ─────────────────────────────────────────────────────────

async def _handle_uninstalled(
    shop_domain: str,
    db: AsyncSession,
    triggered_at: Optional[str] = None,
    authoritative: bool = False,
) -> None:
    """Mark a shop uninstalled.

    `authoritative=True` skips the stale check — used by `/auth` and the
    reconciliation cron, which both confirm the token is dead via a live
    401 response before calling. The stale check only guards the webhook
    path, where Shopify may deliver events from a previous install session.
    """
    result = await db.execute(select(Shop).where(Shop.shop_domain == shop_domain))
    shop = result.scalar_one_or_none()
    if not shop:
        return
    if shop.plan_status in ("uninstalled", "purged"):
        return
    now = datetime.now(timezone.utc)
    installed = shop.installed_at
    if installed and installed.tzinfo is None:
        installed = installed.replace(tzinfo=timezone.utc)
    # Stale check applies only to webhook-delivered events, not to
    # 401-confirmed callers. Compare triggered_at against installed_at —
    # a webhook from a previous install session has triggered_at earlier
    # than the current install. Fall back to a 60s window when the header
    # is missing (older Shopify API versions).
    if not authoritative and installed:
        if triggered_at:
            try:
                webhook_time = datetime.fromisoformat(triggered_at.replace("Z", "+00:00"))
                if webhook_time < installed:
                    logger.info("ignoring_stale_uninstall_webhook", shop=shop_domain,
                                webhook_triggered_at=triggered_at,
                                installed_at=installed.isoformat())
                    return
            except ValueError:
                pass
        elif (now - installed).total_seconds() < 60:
            logger.info("ignoring_stale_uninstall_webhook", shop=shop_domain,
                        installed_seconds_ago=int((now - installed).total_seconds()))
            return
    shop.access_token_encrypted = ""
    shop.refresh_token_encrypted = None
    shop.access_token_expires_at = None
    shop.refresh_token_expires_at = None
    shop.shopify_charge_id = None
    shop.billing_cycle_start = None
    shop.trial_started_at = None
    shop.trial_ends_at = None
    shop.grace_period_ends_at = None
    shop.plan_status = "uninstalled"
    shop.plan_tier = "none"
    shop.uninstalled_at = now
    shop.data_purge_at = now + timedelta(days=DATA_RETENTION_DAYS)
    await db.commit()
    logger.info("shop_uninstalled", shop=shop_domain)


def _extract_numeric_id(raw: str) -> str:
    """Normalize a Shopify charge ID to its numeric part.

    The billing callback receives a numeric ID from the return URL (?charge_id=123).
    Webhook payloads use the full GID (gid://shopify/AppSubscription/123).
    Normalizing both to numeric ensures stale-detection comparisons work correctly.
    """
    return raw.rsplit("/", 1)[-1] if "/" in raw else raw


async def _handle_subscription_update(shop_domain: str, payload: dict, db: AsyncSession) -> None:
    result = await db.execute(select(Shop).where(Shop.shop_domain == shop_domain))
    shop = result.scalar_one_or_none()
    if not shop:
        return

    charge = payload.get("app_subscription", {})
    status = charge.get("status", "").lower()  # Shopify sends uppercase enum values (ACTIVE, CANCELLED…)
    charge_id = _extract_numeric_id(str(charge.get("admin_graphql_api_id", charge.get("id", ""))))

    now = datetime.now(timezone.utc)

    if status == "active":
        shop.shopify_charge_id = charge_id

        # Derive plan tier from subscription name so upgrades/downgrades are picked up.
        sub_name = charge.get("name", "").lower()
        derived_tier = next(
            (tier for tier in PLANS if tier in sub_name),
            shop.plan_tier if shop.plan_tier in PLANS else "starter",
        )
        shop.plan_tier = derived_tier

        # A subscription activation (a monthly renewal, or a deferred
        # downgrade/switch finally taking effect) clears any pending scheduled
        # change — the change has now happened (or been superseded).
        shop.scheduled_plan_tier = None
        shop.scheduled_change_at = None

        force_active_write = False
        if shop.plan_status == "trial_active":
            trial_end = shop.trial_ends_at
            if trial_end and trial_end.tzinfo is None:
                trial_end = trial_end.replace(tzinfo=timezone.utc)
            if trial_end and trial_end > now:
                # Trial still running — this is the initial "subscription active" confirmation.
                # Preserve trial_active; billing cycle resets when the trial actually converts.
                event_type = "trial_confirmed"
            else:
                # Trial period has ended — merchant is now paying.
                shop.plan_status = "active"
                shop.grace_period_ends_at = None
                shop.billing_cycle_start = now
                event_type = "trial_converted"
                force_active_write = True
        elif shop.plan_status in ("pending", "none", None, ""):
            # First activation arriving before (or instead of) the billing
            # callback: the callback refuses to activate until Shopify reports
            # the charge ACTIVE, so this path applies the trial itself, using the
            # same rule the charge was created with (trial only if never
            # trialled). If the callback runs afterwards, its replay guard sees
            # the same charge_id already live and does nothing.
            plan_cfg = PLANS.get(derived_tier, {})
            if derived_tier in PLAN_DEFAULT_AI_TIER:
                shop.selected_model = PLAN_DEFAULT_AI_TIER[derived_tier]  # AI tier, as the callback sets
            if not shop.trial_used and plan_cfg.get("trial_days", 0) > 0:
                shop.plan_status = "trial_active"
                shop.trial_used = True
                shop.trial_started_at = now
                shop.trial_ends_at = now + timedelta(days=plan_cfg["trial_days"])
                shop.grace_period_ends_at = None
                shop.billing_cycle_start = now
                event_type = "trial_started"
            else:
                shop.plan_status = "active"
                shop.grace_period_ends_at = None
                shop.billing_cycle_start = now
                event_type = "activated"
                force_active_write = True
        else:
            # Already active. Shopify sometimes fires multiple webhooks for the same
            # subscription event with different IDs (bypassing idempotency). Distinguish
            # a genuine monthly renewal from a duplicate initial-activation webhook by
            # checking how recently billing_cycle_start was set.
            cycle = shop.billing_cycle_start
            if cycle and cycle.tzinfo is None:
                cycle = cycle.replace(tzinfo=timezone.utc)
            is_duplicate_activation = cycle and (now - cycle).total_seconds() < 60
            shop.plan_status = "active"
            shop.grace_period_ends_at = None
            shop.billing_cycle_start = now
            event_type = "activation_confirmed" if is_duplicate_activation else "renewed"
            force_active_write = True

        if force_active_write:
            # Race-safe write: a concurrent cancellation webhook (from Shopify
            # auto-cancelling the OLD subscription during a plan switch) may
            # load the shop with plan_status='active' from its stale session
            # view, then write 'cancelled' AFTER we've assigned 'active' here
            # but BEFORE our commit — leaving the shop stuck at 'cancelled'
            # on a valid new subscription. Explicit UPDATE bypasses the
            # session cache and guarantees the 'active' transition wins.
            await db.execute(
                update(Shop)
                .where(Shop.id == shop.id)
                .values(plan_status="active", grace_period_ends_at=None)
            )
    elif status == "cancelled":
        # Ignore stale cancellations from plan switches. When a merchant upgrades,
        # Shopify cancels the old subscription after activating the new one.
        # Two stale-detection signals — either is sufficient to ignore:
        # 1. charge_id mismatch: shopify_charge_id already points to the new plan's charge.
        # 2. tier mismatch: the cancelled subscription name belongs to a lower tier than
        #    the current plan (belt-and-suspenders for race conditions where the charge_id
        #    hasn't been updated yet but the plan_tier was already upgraded).
        sub_name = charge.get("name", "").lower()
        cancelled_tier = next((tier for tier in PLANS if tier in sub_name), None)
        different_charge = shop.shopify_charge_id and shop.shopify_charge_id != charge_id
        stale_tier = (
            cancelled_tier
            and shop.plan_tier in PLANS
            and cancelled_tier != shop.plan_tier
            and shop.plan_status == "active"
        )
        if different_charge or stale_tier:
            event_type = "cancelled_stale_ignored"
        else:
            # Grace period starts after the paid billing period ends, not immediately.
            # Merchant keeps full access for the rest of the period they paid for,
            # then gets 7 read-only days before being fully locked out.
            cycle_start = shop.billing_cycle_start or now
            if cycle_start.tzinfo is None:
                cycle_start = cycle_start.replace(tzinfo=timezone.utc)
            paid_period_end = cycle_start + timedelta(days=CYCLE_DAYS)
            new_grace = paid_period_end + timedelta(days=GRACE_PERIOD_DAYS)
            # Race-safe conditional UPDATE: only write cancellation if the
            # shop is STILL on the cancelled charge_id. If a concurrent
            # renewal webhook (from a plan switch) already updated the shop
            # to a new charge_id, this UPDATE affects zero rows — the
            # cancellation is a stale echo of the OLD subscription and
            # must not overwrite the fresh active state. See the downgrade
            # race in the audit note above.
            result = await db.execute(
                update(Shop)
                .where(Shop.id == shop.id)
                .where(Shop.shopify_charge_id == charge_id)
                .values(plan_status="cancelled", grace_period_ends_at=new_grace)
            )
            if result.rowcount == 0:
                event_type = "cancelled_stale_ignored"
                logger.info("cancellation_ignored_charge_moved_on",
                            shop=shop_domain, cancelled_charge=charge_id)
            else:
                # Keep the in-memory shop object in sync so downstream
                # BillingEvent + logging reflect the actual write.
                shop.plan_status = "cancelled"
                shop.grace_period_ends_at = new_grace
                event_type = "cancelled"
    elif status == "declined":
        # Same guard: a DECLINED for an old charge (e.g., rejected upgrade attempt) should
        # not affect the shop's current active subscription.
        if shop.shopify_charge_id and shop.shopify_charge_id != charge_id:
            event_type = "declined_stale_ignored"
        else:
            shop.plan_status = "declined"
            event_type = "declined"
    elif status == "expired":
        if shop.shopify_charge_id and shop.shopify_charge_id != charge_id:
            event_type = "expired_stale_ignored"
        else:
            shop.plan_status = "expired"
            shop.grace_period_ends_at = now + timedelta(days=GRACE_PERIOD_DAYS)
            event_type = "expired"  # expired = trial expired, no paid period to honor
    else:
        event_type = status

    db.add(BillingEvent(
        id=uuid.uuid4(),
        shop_id=shop.id,
        event_type=event_type,
        plan_tier=shop.plan_tier,
        shopify_charge_id=charge_id,
    ))
    await db.commit()
    logger.info("subscription_updated", shop=shop_domain, status=status)

    # Catalog: a paid plan lifts the trial product cap; a webhook-first
    # activation starts the first sync (kick_catalog_syncs is the fallback).
    if event_type == "trial_converted":
        await enqueue("catalog_start_sync", str(shop.id), "trial_converted")
    elif event_type in ("trial_started", "activated"):
        await enqueue("catalog_start_sync", str(shop.id), "initial")


# ── Catalog (docs/TECHNICAL_PLAN.md §4.1) ────────────────────────────────────
# Webhooks only enqueue: the worker fetches the product over GraphQL, so one
# parser handles bulk and single-product syncs. Missed or failed enqueues are
# caught by the nightly reconcile_catalogs cron.

async def _catalog_shop(shop_domain: str, db: AsyncSession) -> Optional[Shop]:
    return (await db.execute(
        select(Shop).where(Shop.shop_domain == shop_domain, Shop.plan_status.in_(("active", "trial_active")))
    )).scalar_one_or_none()


async def _handle_product_changed(shop_domain: str, payload: dict, db: AsyncSession) -> None:
    product_id = str(payload.get("id") or "")
    if not product_id or await _catalog_shop(shop_domain, db) is None:
        return
    # Same job id + short delay: a burst of updates to one product runs once.
    await enqueue("catalog_sync_product", shop_domain, product_id,
                  _job_id=f"catalog-product:{shop_domain}:{product_id}", _defer_by=10)


async def _handle_product_deleted(shop_domain: str, payload: dict, db: AsyncSession) -> None:
    product_id = str(payload.get("id") or "")
    shop = (await db.execute(select(Shop).where(Shop.shop_domain == shop_domain))).scalar_one_or_none()
    if not product_id or shop is None:
        return
    await db.execute(delete(CatalogProductRow).where(
        CatalogProductRow.shop_id == shop.id, CatalogProductRow.product_id == product_id))
    await db.commit()


async def _handle_bulk_finished(shop_domain: str, payload: dict, db: AsyncSession) -> None:
    op_id = payload.get("admin_graphql_api_id")
    if not op_id or await _catalog_shop(shop_domain, db) is None:
        return
    await enqueue("catalog_finish_bulk", shop_domain, op_id, _job_id=f"catalog-bulk:{op_id}")


# ── Gift orders (docs/TECHNICAL_PLAN.md §5.3, §6.3) ──────────────────────────
# Parse here (cheap) and store the gift_orders row; tagging + the order
# metafield (Shopify calls) run in the annotate_gift_order job.

async def _count_order(db: AsyncSession, shop: Shop, payload: dict) -> None:
    """Every order counts toward the "gift orders vs all orders" denominator."""
    try:
        total = round(float(payload.get("total_price") or 0), 2)
    except (TypeError, ValueError):
        total = 0.0
    day = datetime.now(timezone.utc).date()
    for _ in range(2):
        row = (await db.execute(select(OrderCountDaily).where(
            OrderCountDaily.shop_id == shop.id, OrderCountDaily.day == day))).scalar_one_or_none()
        if row is None:
            db.add(OrderCountDaily(shop_id=shop.id, day=day, orders=1, revenue=total))
        else:
            row.orders = (row.orders or 0) + 1
            row.revenue = float(row.revenue or 0) + total
        try:
            await db.commit()
            return
        except IntegrityError:      # another order created today's row first
            await db.rollback()


async def _handle_order_created(shop_domain: str, payload: dict, db: AsyncSession) -> None:
    shop = (await db.execute(select(Shop).where(Shop.shop_domain == shop_domain))).scalar_one_or_none()
    if shop is None or shop.plan_status in ("uninstalled", "purged"):
        return
    await _count_order(db, shop, payload)
    parsed = parse_gift_order(payload)
    if parsed is None:
        return
    exists_ = (await db.execute(select(GiftOrder.id).where(
        GiftOrder.shop_id == shop.id, GiftOrder.order_id == parsed.order_id))).first()
    if exists_:
        return

    source = None
    if parsed.note or any(g.get("note") for g in parsed.groups):
        draft = None
        if parsed.sid:
            session = (await db.execute(select(GiftSession).where(
                GiftSession.shop_id == shop.id, GiftSession.sid == parsed.sid))).scalar_one_or_none()
            drafts = (session.note_drafts if session else None) or {}
            draft = (drafts.get("order") or next(iter(drafts.values()), None) or {}).get("last") if drafts else None
        final = parsed.note or next(g["note"] for g in parsed.groups if g.get("note"))
        source = note_source(final, draft)

    db.add(GiftOrder(
        shop_id=shop.id, order_id=parsed.order_id, order_name=parsed.order_name, sid=parsed.sid,
        delivery_mode=parsed.delivery_mode, gift_lines=parsed.gift_lines, gift_revenue=parsed.gift_revenue,
        order_total=parsed.order_total, currency=parsed.currency, note_source=source,
        groups=[{k: g.get(k) for k in ("id", "label", "wrap", "message")} for g in parsed.groups],
    ))
    try:
        await db.commit()
    except IntegrityError:          # the same order redelivered concurrently
        await db.rollback()
        return
    await enqueue("annotate_gift_order", shop_domain, parsed.order_gid, metafield_value(parsed),
                  _job_id=f"gift-order:{shop.id}:{parsed.order_id}")
