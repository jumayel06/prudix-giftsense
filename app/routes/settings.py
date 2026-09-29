"""Merchant settings: AI model choice, plan summary, weekly email opt-in.

GET/PUT /api/settings copied from Prudix Commerce, trimmed to GiftSense
fields. Model selection is validated server-side against the plan's
models_available: the dashboard grays out locked models, but the API must
refuse them too (tests/regression/test_model_plan_gating.py).
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import MODEL_WEIGHTS, PLANS, effective_model
from core.db.models import Shop
from core.db.session import get_db
from core.shopify_deps import get_current_shop

router = APIRouter()


@router.get("/api/settings")
async def get_settings(
    shop_record: Shop = Depends(get_current_shop),
):
    plan_tier = shop_record.plan_tier if shop_record.plan_tier in PLANS else "starter"
    plan = PLANS[plan_tier]
    selected_model = effective_model(plan_tier, shop_record.selected_model)
    return {
        "selected_model": selected_model,
        "model_weight": MODEL_WEIGHTS.get(selected_model, 1),
        "models_available": plan["models_available"],
        "model_weights": {m: MODEL_WEIGHTS[m] for m in plan["models_available"]},
        "features": plan["features"],
        "plan_tier": plan_tier,
        "plan_status": shop_record.plan_status,
        "plan_name": plan["name"],
        "generation_limit": plan["generation_limit"],
        "price_usd": plan["price_usd"],
        # Deferred plan change (downgrade / same-tier switch) scheduled for the
        # end of the current cycle — drives the "plan changes on X" banner.
        "scheduled_plan_tier": shop_record.scheduled_plan_tier,
        "scheduled_plan_name": (
            PLANS.get(shop_record.scheduled_plan_tier, {}).get("name")
            if shop_record.scheduled_plan_tier else None
        ),
        "scheduled_change_at": (
            shop_record.scheduled_change_at.isoformat()
            if shop_record.scheduled_change_at else None
        ),
        "digest_email_opt_in": bool(shop_record.digest_email_opt_in),
        "shop_domain": shop_record.shop_domain,
    }


class SaveSettingsRequest(BaseModel):
    selected_model: str | None = None
    digest_email_opt_in: bool | None = None


@router.put("/api/settings")
async def save_settings(
    payload: SaveSettingsRequest,
    shop_record: Shop = Depends(get_current_shop),
    db: AsyncSession = Depends(get_db),
):
    if payload.selected_model:
        plan_tier = shop_record.plan_tier if shop_record.plan_tier in PLANS else "starter"
        plan = PLANS[plan_tier]
        if payload.selected_model not in plan["models_available"]:
            raise HTTPException(status_code=403, detail={
                "code": "model_not_available",
                "message": f"This model is not available on your {plan['name']} plan. Upgrade to access it.",
            })
        shop_record.selected_model = payload.selected_model

    if payload.digest_email_opt_in is not None:
        shop_record.digest_email_opt_in = payload.digest_email_opt_in

    await db.commit()
    return {"ok": True}
