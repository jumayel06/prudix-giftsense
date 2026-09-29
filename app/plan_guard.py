from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import CYCLE_DAYS, PLANS
from core.config import settings
from core.db.models import Shop, UsageLog

# Statuses that can generate new content
GENERATE_STATUSES = {"active", "trial_active"}

# Statuses that can view existing data (cancelled/expired get grace-period read access)
VIEW_STATUSES = {"active", "trial_active", "cancelled", "expired"}


def _paid_period_ends_at(shop: Shop) -> datetime | None:
    """Return the end of the billing period the merchant already paid for, or None."""
    if not shop.billing_cycle_start:
        return None
    start = shop.billing_cycle_start
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    return start + timedelta(days=CYCLE_DAYS)


def effective_cycle_start(shop: Shop) -> datetime | None:
    """Return the start of the shop's CURRENT monthly generation cycle.

    GiftSense is monthly-only, so `billing_cycle_start` advances every cycle via
    the app_subscriptions/update renewal webhook and is used verbatim. (Commerce
    also rolls a 30-day anchor forward for annual plans; not needed here.)
    """
    if not shop.billing_cycle_start:
        return None
    start = shop.billing_cycle_start
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    return start


def _in_paid_period(shop: Shop) -> bool:
    """True if a cancelled shop is still within the period they paid for."""
    ends_at = _paid_period_ends_at(shop)
    if not ends_at:
        return False
    return datetime.now(timezone.utc) < ends_at


def _in_grace_period(shop: Shop) -> bool:
    if not shop.grace_period_ends_at:
        return False
    ends_at = shop.grace_period_ends_at
    if ends_at.tzinfo is None:
        ends_at = ends_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) <= ends_at


async def get_shop_plan(shop_domain: str, db: AsyncSession) -> tuple[Shop, dict]:
    result = await db.execute(select(Shop).where(Shop.shop_domain == shop_domain))
    shop = result.scalar_one_or_none()
    if not shop:
        raise HTTPException(status_code=404, detail="Shop not found")
    plan = PLANS.get(shop.plan_tier or "starter", PLANS["starter"])
    return shop, plan


async def require_feature(feature: str, shop_domain: str, db: AsyncSession) -> tuple[Shop, dict]:
    """Raise 403 if the shop cannot access the feature.

    Active and trial shops: full access.
    Cancelled/expired shops within grace period: read-only access (view existing data).
    All others (pending, uninstalled, purged, past grace period): blocked.
    """
    shop, plan = await get_shop_plan(shop_domain, db)

    if shop.plan_status not in VIEW_STATUSES:
        raise HTTPException(
            status_code=403,
            detail=f"Your plan is not active (status: {shop.plan_status}). Please choose a plan.",
        )

    # Cancelled shops still within their paid billing period have full access —
    # they already paid for that time. After the paid period ends, the 7-day
    # read-only grace kicks in. Expired plans get only the grace period.
    if shop.plan_status == "cancelled" and _in_paid_period(shop):
        pass  # full access — paid period still running
    elif shop.plan_status in ("cancelled", "expired") and not _in_grace_period(shop):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "grace_period_ended",
                "message": "Your grace period has ended. Please renew your plan to continue.",
            },
        )

    if feature not in plan.get("features", []):
        upgrade_to = next(
            (name for name, p in PLANS.items() if feature in p.get("features", [])),
            "growth",
        )
        raise HTTPException(
            status_code=403,
            detail={
                "code": "feature_not_available",
                "feature": feature,
                "current_plan": shop.plan_tier,
                "upgrade_to": upgrade_to,
                "message": f"This feature requires the {PLANS[upgrade_to]['name']} plan or higher.",
            },
        )

    return shop, plan


def may_generate(shop: Shop) -> bool:
    """Can this shop use AI right now? Active or trialling, or cancelled but
    still inside the period it paid for."""
    if shop.plan_status in GENERATE_STATUSES:
        return True
    return shop.plan_status == "cancelled" and _in_paid_period(shop)


def generation_limit_for(shop: Shop) -> int:
    """This cycle's generation budget: the plan's, or its trial cap while trialling."""
    plan = PLANS.get(shop.plan_tier or "starter", PLANS["starter"])
    if shop.plan_status == "trial_active":
        return min(plan["generation_limit"], plan.get("trial_generations", plan["generation_limit"]))
    return plan["generation_limit"]


def daily_cost_cap_for(shop: Shop) -> float:
    """USD/day backstop; 0 disables it."""
    plan = PLANS.get(shop.plan_tier or "starter", PLANS["starter"])
    cap = plan.get("daily_cost_cap_usd")
    return settings.max_daily_cost_usd_per_shop if cap is None else cap


async def get_daily_cost_usd(shop: Shop, db: AsyncSession) -> float:
    """Sum LLM cost (USD) this shop's shopper-facing AI accrued today (UTC).
    Catalog analysis is excluded: it's our background cost with its own cap
    (product re-reads per month) and must never push shoppers onto template
    reasons."""
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    result = await db.execute(
        select(func.coalesce(func.sum(UsageLog.cost_usd), 0)).where(
            UsageLog.shop_id == shop.id,
            UsageLog.created_at >= today_start,
            UsageLog.action_type != "catalog_analysis",
        )
    )
    return float(result.scalar() or 0)


async def check_daily_cost_cap(shop: Shop, db: AsyncSession) -> None:
    """Refuse generation if today's accumulated LLM spend exceeds the daily cap.
    Independent of plan generation limits — margin-protection backstop against
    runaway scripts, abuse, or pathological prompts.

    Per-tier cap from `PLANS[tier]["daily_cost_cap_usd"]` takes precedence;
    falls back to the global `max_daily_cost_usd_per_shop` setting if the plan
    doesn't define one. Disabled when the resolved cap is 0.
    """
    cap = daily_cost_cap_for(shop)
    if cap <= 0:
        return
    spent = await get_daily_cost_usd(shop, db)
    if spent >= cap:
        # Reset window: midnight UTC = next day 00:00:00.
        now = datetime.now(timezone.utc)
        resumes_at = (now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1))
        raise HTTPException(
            status_code=429,
            detail={
                "code": "daily_cost_cap_exceeded",
                "message": (
                    "You've reached today's safety cap on AI generation cost. "
                    "This protects you from unexpected billing. "
                    "Generation resumes at the start of the next UTC day. "
                    "Contact support if you need this raised."
                ),
                "spent_usd_today": round(spent, 2),
                "cap_usd": cap,
                "resumes_at": resumes_at.isoformat(),
            },
        )


async def get_generations_used(shop: Shop, db: AsyncSession) -> int:
    """Return how many generations this shop has consumed in the current
    monthly billing cycle. SUM, never COUNT: refund rows are negative."""
    query = select(func.coalesce(func.sum(UsageLog.generations_consumed), 0)).where(
        UsageLog.shop_id == shop.id
    )
    cycle_floor = effective_cycle_start(shop)
    if cycle_floor:
        query = query.where(UsageLog.created_at >= cycle_floor)
    result = await db.execute(query)
    return int(result.scalar() or 0)


async def check_generation_limit(shop: Shop, db: AsyncSession, count: int = 1) -> int:
    """
    Check that the shop has at least `count` generations remaining.
    During trial, applies the trial_generations cap instead of the full plan limit.
    Raises HTTP 429 if the budget is exhausted.
    Returns the number of generations remaining after this call would be consumed.
    """
    from datetime import timezone

    plan = PLANS.get(shop.plan_tier or "starter", PLANS["starter"])
    full_limit = plan["generation_limit"]
    is_trial = shop.plan_status == "trial_active"
    limit = generation_limit_for(shop)  # trial_generations cap while trialling

    used = await get_generations_used(shop, db)
    remaining = limit - used

    if remaining < count:
        if is_trial:
            # Give the merchant context: when does the full budget kick in?
            trial_ends_at = shop.trial_ends_at
            when = None
            if trial_ends_at is not None:
                if trial_ends_at.tzinfo is None:
                    trial_ends_at = trial_ends_at.replace(tzinfo=timezone.utc)
                from datetime import datetime
                now = datetime.now(timezone.utc)
                days_left = max(0, (trial_ends_at - now).days)
                when = "tomorrow" if days_left == 0 else ("in 1 day" if days_left == 1 else f"in {days_left} days")

            if remaining <= 0:
                # Truly exhausted — no generations left at all.
                if when:
                    full_msg = (
                        f"You've used all {limit} trial generations. "
                        f"Your full {full_limit}-generation {plan['name']} plan starts {when} "
                        f"when your trial converts — or upgrade to Pro now for {PLANS['pro']['generation_limit']} generations/month."
                    )
                else:
                    full_msg = (
                        f"You've used all {limit} trial generations. "
                        f"Your full {full_limit}-generation plan starts when your trial converts. "
                        "You can also upgrade to Pro now for more generations."
                    )
            else:
                # Some remain, but not enough for this specific action.
                gen_word = "generation" if remaining == 1 else "generations"
                if when:
                    full_msg = (
                        f"You have {remaining} trial {gen_word} left, but this action needs {count}. "
                        f"Your full {full_limit}-generation {plan['name']} plan starts {when} "
                        f"when your trial converts — or upgrade to Pro now."
                    )
                else:
                    full_msg = (
                        f"You have {remaining} trial {gen_word} left, but this action needs {count}. "
                        "Upgrade to a paid plan for more generations."
                    )
        else:
            full_msg = (
                f"You've used {used} of {limit} generations this billing cycle. "
                "Upgrade your plan for a higher monthly limit."
            )

        raise HTTPException(
            status_code=429,
            detail={
                "code": "generation_limit_exceeded",
                "is_trial": is_trial,
                "used": used,
                "limit": limit,
                "full_plan_limit": full_limit,
                "requested": count,
                "remaining": max(0, remaining),
                "trial_ends_at": shop.trial_ends_at.isoformat() if shop.trial_ends_at else None,
                "message": full_msg,
            },
        )

    return remaining - count


async def require_generation(
    feature: str,
    shop_domain: str,
    db: AsyncSession,
    count: int = 1,
) -> tuple[Shop, dict]:
    """
    Combined guard for all LLM generation endpoints:
    1. Plan must include the requested feature (view check — allows grace period)
    2. Plan must be strictly active for generation (no grace-period generation)
    3. Shop must have enough generation budget remaining
    Returns (shop, plan) on success.
    """
    shop, plan = await require_feature(feature, shop_domain, db)

    # Cancelled shops within their paid period can still generate — they already paid.
    # Past the paid period, generation is blocked (grace period is read-only).
    if not may_generate(shop):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "subscription_ended",
                "message": "Your subscription has ended. Please renew your plan to generate new content.",
            },
        )

    await check_generation_limit(shop, db, count)
    await check_daily_cost_cap(shop, db)

    # Activation tracking: stamp the first time a shop passes every gate.
    # Staged in the same session as the caller's writes so they commit together —
    # if the caller rolls back, this rolls back too. The `is None` guard means
    # we touch the column at most once per shop's lifetime.
    if shop.first_generation_at is None:
        shop.first_generation_at = datetime.now(timezone.utc)

    return shop, plan
