"""Merchant settings: AI tier choice, plan summary, weekly email opt-in.

GET/PUT /api/settings copied from Prudix Commerce, trimmed to GiftSense
fields. Merchants pick an AI tier (Standard / Advanced / Premium, app/ai_models.py),
never a model. The choice is validated server-side against the plan's
ai_tiers: the dashboard grays out locked tiers, but the API must refuse them
too (tests/regression/test_model_plan_gating.py).
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai_models import AI_TIERS, ai_tier_for, tier_models_for_shop
from app.config import PLANS
from app.jobs import enqueue
from app.services.wrap import WrapUpdate, create_image_upload, update_wrap_settings, wrap_settings
from app.services.delivery import DeliveryUpdate, date_window, delivery_settings, update_delivery_settings
from app.services.gift_settings import NOTE_TONES, GiftNotesUpdate, note_settings, update_note_settings
from core.db.models import Shop
from core.db.session import get_db
from core.shopify_auth import get_valid_access_token
from core.shopify_deps import get_current_shop

router = APIRouter()


@router.get("/api/settings")
async def get_settings(
    shop_record: Shop = Depends(get_current_shop),
):
    plan_tier = shop_record.plan_tier if shop_record.plan_tier in PLANS else "starter"
    plan = PLANS[plan_tier]
    ai_tier = ai_tier_for(plan_tier, shop_record.selected_model)
    return {
        "ai_tier": ai_tier,
        "ai_tier_weight": AI_TIERS[ai_tier]["weight"],
        "ai_tiers_available": plan["ai_tiers"],
        "ai_tiers": _ai_tier_catalog(),
        # "Currently runs on …" per tier, resolved for this store (rollout-aware).
        "ai_tier_models": tier_models_for_shop(shop_record),
        "gift_notes": note_settings(shop_record),
        "gift_wrap": wrap_settings(shop_record),
        "delivery": delivery_settings(shop_record),
        # Preview for the Arrive-by page: what a shopper ordering now could pick.
        "delivery_window": date_window(delivery_settings(shop_record), shop_record.store_timezone),
        "store_timezone": shop_record.store_timezone or "UTC",
        "note_tones": [{"value": k, "label": v} for k, v in NOTE_TONES.items()],
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


def _ai_tier_catalog() -> dict:
    # `plan`: the lowest plan that includes the tier, so Settings can show locked
    # tiers ("Available on Pro") instead of hiding them.
    return {t: {"label": s["label"], "description": s["description"], "weight": s["weight"],
                "plan": next((p["name"] for p in PLANS.values() if t in p["ai_tiers"]), None)}
            for t, s in AI_TIERS.items()}


class SaveSettingsRequest(BaseModel):
    ai_tier: str | None = None
    digest_email_opt_in: bool | None = None
    gift_notes: GiftNotesUpdate | None = None
    gift_wrap: WrapUpdate | None = None
    delivery: DeliveryUpdate | None = None


@router.put("/api/settings")
async def save_settings(
    payload: SaveSettingsRequest,
    shop_record: Shop = Depends(get_current_shop),
    db: AsyncSession = Depends(get_db),
):
    if payload.ai_tier:
        plan_tier = shop_record.plan_tier if shop_record.plan_tier in PLANS else "starter"
        plan = PLANS[plan_tier]
        if payload.ai_tier not in plan["ai_tiers"]:
            raise HTTPException(status_code=403, detail={
                "code": "ai_tier_not_available",
                "message": f"This AI option is not available on your {plan['name']} plan. Upgrade to access it.",
            })
        shop_record.selected_model = payload.ai_tier  # column stores the AI tier

    if payload.digest_email_opt_in is not None:
        shop_record.digest_email_opt_in = payload.digest_email_opt_in
    if payload.gift_notes is not None:
        update_note_settings(shop_record, payload.gift_notes)
    if payload.delivery is not None:
        plan = PLANS.get(shop_record.plan_tier, PLANS["starter"])
        if payload.delivery.enabled and "arrive_by" not in plan["features"]:
            raise HTTPException(status_code=403, detail={
                "code": "feature_not_available",
                "message": f"Arrive-by dates aren't available on the {plan['name']} plan. Upgrade to Growth to use them.",
            })
        update_delivery_settings(shop_record, payload.delivery)
    resync_wrap = False
    if payload.gift_wrap is not None:
        try:
            update_wrap_settings(shop_record, payload.gift_wrap)
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"code": "invalid_wrap_photo", "message": str(e)})
        w = wrap_settings(shop_record)
        resync_wrap = w["enabled"] and bool(w["styles"]) and not w["ready"]

    await db.commit()
    if resync_wrap:
        await enqueue("sync_wrap", shop_record.shop_domain)
    return {"ok": True}


class WrapImageUpload(BaseModel):
    filename: str
    mime_type: str
    size: int


@router.post("/api/settings/gift-wrap/image-upload")
async def wrap_image_upload(
    payload: WrapImageUpload,
    shop_record: Shop = Depends(get_current_shop),
    db: AsyncSession = Depends(get_db),
):
    """Staged upload target for a wrap style photo (app/services/wrap.py)."""
    token = await get_valid_access_token(shop_record, db)
    try:
        return await create_image_upload(shop_record.shop_domain, token, payload.filename,
                                         payload.mime_type, payload.size)
    except ValueError as e:
        raise HTTPException(status_code=422, detail={"code": "invalid_wrap_photo", "message": str(e)})
    except RuntimeError:
        raise HTTPException(status_code=502, detail={"code": "upload_unavailable",
                                                     "message": "Shopify didn't accept the upload. Please try again."})
