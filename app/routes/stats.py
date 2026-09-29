"""GET /api/stats — dashboard boot + usage card (ported from Prudix Commerce's
analytics.get_stats, trimmed to GiftSense and monthly-only billing).

The dashboard calls this on load: `plan_status == "pending"` routes the
merchant to the plan picker. Usage is SUM(generations_consumed) since the
current cycle start (refund rows are negative), never COUNT().
"""
import math
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai_models import AI_TIERS, ai_tier_for, model_for_shop, model_label
from app.config import CYCLE_DAYS, PLANS
from app.plan_guard import effective_cycle_start
from core.config import settings
from core.db.models import Shop, UsageLog
from core.db.session import get_db
from core.shopify_deps import get_current_shop

router = APIRouter()


def _utc(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


@router.get("/api/stats")
async def get_stats(
    shop_record: Shop = Depends(get_current_shop),
    db: AsyncSession = Depends(get_db),
):
    plan_tier = shop_record.plan_tier if shop_record.plan_tier in PLANS else "starter"
    plan = PLANS[plan_tier]
    now = datetime.now(timezone.utc)

    cycle_start = effective_cycle_start(shop_record)
    used_q = select(func.coalesce(func.sum(UsageLog.generations_consumed), 0)).where(
        UsageLog.shop_id == shop_record.id
    )
    if cycle_start:
        used_q = used_q.where(UsageLog.created_at >= cycle_start)
    generations_used = int((await db.execute(used_q)).scalar() or 0)

    total_used = int((await db.execute(
        select(func.coalesce(func.sum(UsageLog.generations_consumed), 0))
        .where(UsageLog.shop_id == shop_record.id)
    )).scalar() or 0)

    days_elapsed = max(0, (now - cycle_start).days) if cycle_start else 0
    days_remaining = max(0, CYCLE_DAYS - days_elapsed)
    generation_limit = plan["generation_limit"]
    usage_pct = round(generations_used / generation_limit * 100, 1) if generation_limit else 0

    trial_info = {}
    trial_ends = _utc(shop_record.trial_ends_at)
    if shop_record.plan_status == "trial_active" and trial_ends:
        trial_info = {
            "trial_ends_at": trial_ends.isoformat(),
            "trial_days_remaining": max(0, math.ceil((trial_ends - now).total_seconds() / 86400)),
            "trial_generations_cap": plan["trial_generations"],
            "trial_generations_used": generations_used,
            "trial_price_after": plan["price_usd"],
            "trial_plan_name": plan["name"],
        }

    # One-time App Store review prompt after the first AI use. Gated on the
    # listing slug so dev/staging never burn the per-shop flag on a broken link.
    # Atomic UPDATE … WHERE review_prompt_shown = false so two concurrent loads
    # can't both show it.
    show_review_prompt = False
    review_prompt_url = None
    listing_slug = settings.app_store_listing_slug.strip()
    if listing_slug and total_used >= 1 and not shop_record.review_prompt_shown:
        flip = await db.execute(
            update(Shop)
            .where(Shop.id == shop_record.id, Shop.review_prompt_shown.is_(False))
            .values(review_prompt_shown=True)
        )
        await db.commit()
        if flip.rowcount == 1:
            shop_record.review_prompt_shown = True
            show_review_prompt = True
            review_prompt_url = f"https://apps.shopify.com/{listing_slug}#modal-show=ReviewListingModal"

    # Cancelled shops: the date access actually ends (end of the paid month,
    # then the 7-day read-only grace).
    access_until = None
    if shop_record.plan_status == "cancelled" and shop_record.billing_cycle_start:
        paid_end = _utc(shop_record.billing_cycle_start) + timedelta(days=CYCLE_DAYS)
        grace = _utc(shop_record.grace_period_ends_at)
        if now < paid_end:
            access_until = paid_end.isoformat()
        elif grace and now < grace:
            access_until = grace.isoformat()

    return {
        "generations_used": generations_used,
        "generation_limit": generation_limit,
        "usage_pct": usage_pct,
        "days_elapsed": days_elapsed,
        "days_remaining": days_remaining,
        "days_in_cycle": CYCLE_DAYS,
        "plan_status": shop_record.plan_status,
        "plan_tier": plan_tier,
        "plan_name": plan["name"],
        "trial_used": bool(shop_record.trial_used),
        "ai_tier": ai_tier_for(plan_tier, shop_record.selected_model),
        "ai_tier_label": AI_TIERS[ai_tier_for(plan_tier, shop_record.selected_model)]["label"],
        "ai_tier_weight": AI_TIERS[ai_tier_for(plan_tier, shop_record.selected_model)]["weight"],
        "ai_model_label": model_label(model_for_shop(shop_record)),
        "scheduled_plan_tier": shop_record.scheduled_plan_tier,
        "scheduled_plan_name": (
            PLANS.get(shop_record.scheduled_plan_tier, {}).get("name")
            if shop_record.scheduled_plan_tier else None
        ),
        "scheduled_change_at": (
            _utc(shop_record.scheduled_change_at).isoformat()
            if shop_record.scheduled_change_at else None
        ),
        "show_review_prompt": show_review_prompt,
        "review_prompt_url": review_prompt_url,
        "access_until": access_until,
        "shop_domain": shop_record.shop_domain,
        **trial_info,
    }
