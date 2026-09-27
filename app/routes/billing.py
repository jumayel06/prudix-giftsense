"""Shopify Billing API (appSubscriptionCreate), copied from Prudix Commerce.

GiftSense is monthly-only. All of Commerce's hardening is kept: see
docs/SHOPIFY_PLAYBOOK.md §4 for why each guard exists.
"""
import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import (
    CYCLE_DAYS,
    PLAN_DEFAULT_MODELS,
    PLANS,
    derive_tier_from_subscription_name,
    subscription_name,
)
from core.config import settings
from core.db.models import BillingEvent, Shop
from core.db.session import AsyncSessionLocal, get_db
from core.shopify_auth import get_valid_access_token
from core.shopify_deps import get_current_shop

logger = structlog.get_logger()
router = APIRouter()

# Billing callback subscription lookup: Shopify can take a moment to make a
# just-approved charge queryable. ~3s total before leaving it to the webhook.
_SUBSCRIPTION_LOOKUP_ATTEMPTS = 4
_SUBSCRIPTION_LOOKUP_DELAY_SECS = 1.0

_APP_SUBSCRIPTION_CREATE = """
mutation appSubscriptionCreate(
  $name: String!
  $returnUrl: URL!
  $trialDays: Int
  $test: Boolean
  $replacementBehavior: AppSubscriptionReplacementBehavior
  $lineItems: [AppSubscriptionLineItemInput!]!
) {
  appSubscriptionCreate(
    name: $name
    returnUrl: $returnUrl
    trialDays: $trialDays
    test: $test
    replacementBehavior: $replacementBehavior
    lineItems: $lineItems
  ) {
    appSubscription { id }
    confirmationUrl
    userErrors { field message }
  }
}
"""

_APP_SUBSCRIPTION_QUERY = """
query getSubscription($id: ID!) {
  node(id: $id) {
    ... on AppSubscription {
      id
      status
      name
    }
  }
}
"""


def _should_defer_change(current_plan_tier, current_plan_status, new_plan_tier) -> bool:
    """Whether a plan change should be DEFERRED to the next billing cycle
    (Shopify `APPLY_ON_NEXT_BILLING_CYCLE`) instead of applied immediately.

    Rule: only a STRICT UPGRADE (more monthly generations) applies immediately.
    On an active paid plan, a downgrade OR a same-tier switch waits until the
    cycle ends — this stops merchants from resetting their generation quota
    mid-cycle by downgrading and back.
    First-time / trial / non-active subscriptions are never deferred.
    """
    if current_plan_status != "active":
        return False
    if current_plan_tier not in PLANS or new_plan_tier not in PLANS:
        return False
    return PLANS[new_plan_tier]["generation_limit"] <= PLANS[current_plan_tier]["generation_limit"]


@router.post("/api/billing/create-charge")
async def create_charge_for_plan(
    plan: str = Query(default="growth"),
    shop_record: Shop = Depends(get_current_shop),
    db: AsyncSession = Depends(get_db),
):
    if plan not in PLANS:
        raise HTTPException(status_code=400, detail=f"Unknown plan: {plan}")

    access_token = await get_valid_access_token(shop_record, db)
    with_trial = not shop_record.trial_used
    confirmation_url = await create_recurring_charge(
        shop_record.shop_domain, access_token,
        plan_tier=plan, with_trial=with_trial,
        current_plan_tier=shop_record.plan_tier,
        current_plan_status=shop_record.plan_status,
    )
    return {"confirmation_url": confirmation_url}


@router.get("/api/plans")
async def list_plans():
    return {
        "plans": [
            {
                "tier": tier,
                "name": p["name"],
                "price_usd": p["price_usd"],
                "generation_limit": p["generation_limit"],
                "trial_days": p["trial_days"],
                "trial_generations": p["trial_generations"],
                "models_available": p["models_available"],
                "features": p.get("features", []),
                "max_products": p["max_products"],
                "media_messages_per_month": p["media_messages_per_month"],
            }
            for tier, p in PLANS.items()
        ]
    }


async def create_recurring_charge(
    shop_domain: str,
    access_token: str,
    plan_tier: str = "growth",
    with_trial: bool = True,
    current_plan_tier: str | None = None,
    current_plan_status: str | None = None,
) -> str:
    """Create a Shopify app subscription via GraphQL and return the confirmation URL."""
    plan = PLANS.get(plan_tier, PLANS["growth"])

    # Downgrades / same-tier switches on an active plan defer to the next
    # billing cycle; strict upgrades (and first-time subs) apply immediately.
    defer = _should_defer_change(current_plan_tier, current_plan_status, plan_tier)
    replacement_behavior = "APPLY_ON_NEXT_BILLING_CYCLE" if defer else "APPLY_IMMEDIATELY"

    return_url = (
        f"https://{settings.get_app_host()}/billing/callback"
        f"?shop={shop_domain}&plan={plan_tier}&deferred={1 if defer else 0}"
    )

    trial_days = plan.get("trial_days", 0) if with_trial else 0

    variables = {
        "name": subscription_name(plan_tier),
        "returnUrl": return_url,
        "trialDays": trial_days if trial_days > 0 else None,
        # TEST charge when: prod override flag set (BILLING_TEST_MODE) OR non-prod.
        # `false` on prod = real charge (required for launch + submission).
        "test": settings.billing_test_mode or (not settings.is_production),
        "replacementBehavior": replacement_behavior,
        "lineItems": [
            {
                "plan": {
                    "appRecurringPricingDetails": {
                        "price": {"amount": str(plan["price_usd"]), "currencyCode": "USD"},
                        "interval": "EVERY_30_DAYS",
                    }
                }
            }
        ],
    }

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"https://{shop_domain}/admin/api/{settings.shopify_api_version}/graphql.json",
            headers={
                "X-Shopify-Access-Token": access_token,
                "Content-Type": "application/json",
            },
            json={"query": _APP_SUBSCRIPTION_CREATE, "variables": variables},
        )

    if resp.status_code != 200:
        logger.error("billing_graphql_failed", status=resp.status_code, body=resp.text[:300])
        raise HTTPException(status_code=502, detail="Billing API request failed")

    data = resp.json()
    result = data.get("data", {}).get("appSubscriptionCreate", {})
    user_errors = result.get("userErrors", [])
    if user_errors:
        logger.error("billing_user_errors", errors=user_errors)
        raise HTTPException(status_code=502, detail=f"Subscription error: {user_errors}")

    confirmation_url = result.get("confirmationUrl")
    if not confirmation_url:
        logger.error("billing_no_confirmation_url", data=data)
        raise HTTPException(status_code=502, detail="No confirmation URL returned")

    sub_id = result.get("appSubscription", {}).get("id", "")
    logger.info("billing_charge_created", shop=shop_domain, sub_id=sub_id)
    return confirmation_url


@router.get("/billing/callback")
async def billing_callback(
    shop: str = Query(...),
    charge_id: str = Query(...),
    plan: str = Query(default="growth"),
    deferred: int = Query(default=0),
    db: AsyncSession = Depends(get_db),
):
    """Shopify redirects here after merchant approves or declines the charge."""
    result = await db.execute(select(Shop).where(Shop.shop_domain == shop))
    shop_record = result.scalar_one_or_none()
    if not shop_record:
        raise HTTPException(status_code=404, detail="Shop not found")

    access_token = await get_valid_access_token(shop_record, db)
    gid = f"gid://shopify/AppSubscription/{charge_id}"

    # SECURITY: only a subscription Shopify actually returns may activate a
    # plan. Right after approval the node can briefly be null (Shopify writes
    # the charge asynchronously), so retry for a few seconds. Commerce instead
    # activated optimistically on a null node, taking the tier from the plan=
    # param — but a made-up charge_id ALSO returns null, so anyone could get
    # Pro for free. A genuine charge that is still null after the retries is
    # activated by the app_subscriptions/update webhook instead.
    status = ""
    sub: dict = {}
    for attempt in range(_SUBSCRIPTION_LOOKUP_ATTEMPTS):
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"https://{shop}/admin/api/{settings.shopify_api_version}/graphql.json",
                headers={
                    "X-Shopify-Access-Token": access_token,
                    "Content-Type": "application/json",
                },
                json={"query": _APP_SUBSCRIPTION_QUERY, "variables": {"id": gid}},
            )
        if resp.status_code != 200:
            # A genuine API error — don't activate. The merchant lands on the
            # plan picker and can retry, or the webhook activates the charge.
            logger.error("billing_status_fetch_failed", status=resp.status_code)
            break
        sub = (resp.json().get("data") or {}).get("node") or {}
        if sub:
            status = (sub.get("status") or "").lower()
            logger.info("billing_callback_status", shop=shop, charge_id=charge_id, status=status)
            break
        if attempt < _SUBSCRIPTION_LOOKUP_ATTEMPTS - 1:
            await asyncio.sleep(_SUBSCRIPTION_LOOKUP_DELAY_SECS)

    if not sub:
        logger.warning("billing_callback_subscription_not_found", shop=shop, charge_id=charge_id)
        return RedirectResponse(f"https://{shop}/admin/apps/{settings.shopify_api_key}")

    # SECURITY: `plan` and `deferred` are query params on an UNAUTHENTICATED
    # redirect URL the merchant can see and replay/edit. Never trust them for
    # what the merchant actually bought. Derive the tier from the subscription
    # Shopify returned (its name is authoritative — we control the naming).
    # Fall back to the param only when Shopify gave us no name (the null-node
    # "still processing" case), where the webhook heals any mismatch.
    sub_name = sub.get("name") if isinstance(sub, dict) else None
    if sub_name:
        plan = derive_tier_from_subscription_name(sub_name, fallback=plan if plan in PLANS else "growth")
    elif plan not in PLANS:
        plan = "growth"

    plan_cfg = PLANS.get(plan, PLANS["growth"])
    now = datetime.now(timezone.utc)

    # Replay guard (Issue 1): the callback stamps billing_cycle_start = now, and
    # our monthly quota counts generations SINCE that stamp. Because the URL is
    # replayable, a merchant could re-open it to reset their usage to zero for
    # free. Re-opening always references a charge_id the shop is ALREADY active
    # on; a genuine upgrade/first-charge always carries a NEW charge_id. So if
    # this charge is the one we already recorded and the shop is already live on
    # it, treat the hit as a replay and do nothing.
    if (
        status in ("active", "pending")
        and shop_record.shopify_charge_id == str(charge_id)
        and shop_record.plan_status in ("active", "trial_active")
    ):
        logger.info("billing_callback_replay_ignored", shop=shop, charge_id=charge_id)
        return RedirectResponse(f"https://{shop}/admin/apps/{settings.shopify_api_key}")

    # Whether this change defers to the next billing cycle is recomputed
    # server-side from the shop's CURRENT state + the tier we derived from
    # Shopify — never from the `deferred` query param (which the merchant could
    # flip to 0 to force an immediate downgrade + quota reset). This matches the
    # replacementBehavior we sent Shopify at create-charge time.
    defer_change = _should_defer_change(
        shop_record.plan_tier, shop_record.plan_status, plan,
    )

    # Deferred plan change (downgrade / same-tier switch): Shopify keeps the
    # merchant on their CURRENT plan until the cycle ends, then activates this
    # subscription — which the app_subscriptions/update webhook applies. Do NOT
    # touch plan_tier / plan_status / billing_cycle_start here, or the merchant
    # would drop to the lower plan (and reset their generation quota) early.
    # Just record the schedule so the UI can show a "plan changes on X" banner.
    if defer_change and status in ("active", "pending"):
        # Race guard: on accelerated (development) stores the "next billing
        # cycle" is minutes away, so Shopify's activation webhook can apply this
        # change before this callback commits. Re-read the shop; if the change
        # already took effect, don't write a now-stale schedule/banner.
        await db.refresh(shop_record)
        if shop_record.plan_tier == plan:
            shop_record.scheduled_plan_tier = None
            shop_record.scheduled_change_at = None
            await db.commit()
            logger.info("billing_deferred_already_applied", shop=shop, plan=plan)
            return RedirectResponse(f"https://{shop}/admin/apps/{settings.shopify_api_key}")

        cycle_start = shop_record.billing_cycle_start or now
        if cycle_start.tzinfo is None:
            cycle_start = cycle_start.replace(tzinfo=timezone.utc)
        shop_record.scheduled_plan_tier = plan
        shop_record.scheduled_change_at = cycle_start + timedelta(days=CYCLE_DAYS)
        db.add(BillingEvent(
            id=uuid.uuid4(),
            shop_id=shop_record.id,
            event_type="change_scheduled",
            plan_tier=plan,
            shopify_charge_id=str(charge_id),
        ))
        await db.commit()
        logger.info(
            "billing_change_scheduled", shop=shop, plan=plan,
            effective_at=shop_record.scheduled_change_at.isoformat(),
        )
        return RedirectResponse(f"https://{shop}/admin/apps/{settings.shopify_api_key}")

    # "active" = charge approved (with or without trial).
    # "pending" = trial period active (subscription accepted but not yet billing).
    if status in ("active", "pending"):
        previous_tier = shop_record.plan_tier
        shop_record.shopify_charge_id = str(charge_id)
        shop_record.billing_cycle_start = now
        # An immediate change (upgrade / first-time) supersedes any pending
        # scheduled downgrade — clear it.
        shop_record.scheduled_plan_tier = None
        shop_record.scheduled_change_at = None

        if not shop_record.trial_used and plan_cfg.get("trial_days", 0) > 0:
            shop_record.plan_status = "trial_active"
            shop_record.plan_tier = plan
            shop_record.trial_used = True
            shop_record.trial_started_at = now
            shop_record.trial_ends_at = now + timedelta(days=plan_cfg["trial_days"])
            event_type = "trial_started"
        else:
            shop_record.plan_status = "active"
            shop_record.plan_tier = plan
            event_type = "activated"

        # Model: on a first activation (or a model the new plan doesn't allow)
        # use the plan default. On a plan change, keep the merchant's choice.
        if previous_tier not in PLANS or shop_record.selected_model not in plan_cfg["models_available"]:
            shop_record.selected_model = PLAN_DEFAULT_MODELS[plan]

        db.add(BillingEvent(
            id=uuid.uuid4(),
            shop_id=shop_record.id,
            event_type=event_type,
            plan_tier=plan,
            shopify_charge_id=str(charge_id),
        ))
        # Race-safe write: a concurrent app_subscriptions/update cancellation
        # webhook (Shopify auto-cancels the OLD subscription during downgrade
        # or upgrade) may have loaded the shop with plan_status='active' from
        # its stale session view and be about to write 'cancelled' after our
        # commit — leaving the merchant stuck at 'cancelled' on the fresh
        # subscription. Explicit UPDATE bypasses the session cache and forces
        # the active-transition to win. Skip for trial_active (still on trial).
        if event_type != "trial_started":
            await db.execute(
                update(Shop)
                .where(Shop.id == shop_record.id)
                .values(plan_status="active", grace_period_ends_at=None)
            )
        await db.commit()
        logger.info("billing_activated", shop=shop, plan=plan, status=event_type)

    elif status == "declined":
        # Don't overwrite an already-active subscription. A merchant on Pro who declines
        # a Growth upgrade is still on Pro — only set declined if there's no active plan.
        if shop_record.plan_status not in ("active", "trial_active"):
            shop_record.plan_status = "declined"
            await db.commit()
        logger.info("billing_declined", shop=shop, current_status=shop_record.plan_status)

    return RedirectResponse(f"https://{shop}/admin/apps/{settings.shopify_api_key}")
