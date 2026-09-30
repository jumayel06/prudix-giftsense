"""Home onboarding checklist.

    GET  /api/onboarding          steps done from real activity (no stored flags)
    POST /api/onboarding/dismiss  hide it (Shop.onboarding_dismissed_at)

Steps: plan → catalog analyzed → tried in Try it → storefront embed on →
first real shopper search. Shown until all are done or dismissed.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.theme_status import fetch_embed_status
from core.db.models import CatalogProductRow, GiftSession, Shop, UsageLog
from core.db.session import get_db
from core.shopify_auth import get_valid_access_token
from core.shopify_deps import get_current_shop

router = APIRouter()


async def _any(db: AsyncSession, *where) -> bool:
    return bool((await db.execute(select(exists().where(*where)))).scalar())


@router.get("/api/onboarding")
async def onboarding(shop: Shop = Depends(get_current_shop), db: AsyncSession = Depends(get_db)):
    catalog = await _any(db, CatalogProductRow.shop_id == shop.id, CatalogProductRow.embedding.is_not(None))
    try_it = await _any(db, UsageLog.shop_id == shop.id, UsageLog.action_type == "playground")
    first_search = await _any(db, GiftSession.shop_id == shop.id)
    storefront = False
    if not shop.onboarding_dismissed_at:
        # Only ask Shopify while the checklist can still be shown.
        token = await get_valid_access_token(shop, db)
        storefront = (await fetch_embed_status(shop.shop_domain, token))["embed"] == "on"

    steps = [
        {"id": "plan", "done": shop.plan_status in ("active", "trial_active")},
        {"id": "catalog", "done": catalog},
        {"id": "try_it", "done": try_it},
        {"id": "storefront", "done": storefront or first_search},
        {"id": "first_search", "done": first_search},
    ]
    done = sum(s["done"] for s in steps)
    return {"steps": steps, "done_count": done, "total": len(steps),
            "show": shop.onboarding_dismissed_at is None and done < len(steps)}


@router.post("/api/onboarding/dismiss")
async def dismiss(shop: Shop = Depends(get_current_shop), db: AsyncSession = Depends(get_db)):
    shop.onboarding_dismissed_at = datetime.now(timezone.utc)
    await db.commit()
    return {"ok": True}
