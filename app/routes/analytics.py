"""GET /api/analytics?days=7|30|90: the dashboard's Analytics page.

Basic analytics (all plans): the last 30 days (app/services/analytics.py).
Full analytics (Growth+): 7, 30 or 90 days, the breakdowns, and the same
numbers for the period before, so the page shows what changed.
"""
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import PLANS
from app.services import analytics as svc
from core.db.models import Shop
from core.db.session import get_db
from core.shopify_deps import get_current_shop

router = APIRouter()

DAYS = 30
RANGES = (7, 30, 90)
COMPARED = ("sessions", "gift_orders", "attributed_revenue", "conversion_rate", "gift_revenue", "all_orders")


@router.get("/api/analytics")
async def analytics(days: int = Query(DAYS), shop: Shop = Depends(get_current_shop),
                    db: AsyncSession = Depends(get_db)):
    if days not in RANGES:
        raise HTTPException(422, f"days must be one of {RANGES}")
    full = "full_analytics" in PLANS.get(shop.plan_tier, PLANS["starter"])["features"]
    days = days if full else DAYS
    now = datetime.now(timezone.utc)
    data = await svc.summary(db, shop, days, now, full=full)
    data["full"] = full
    if full:
        before = await svc.summary(db, shop, days, now - timedelta(days=days), daily=False)
        data["previous"] = {k: before[k] for k in COMPARED}
        data["changes"] = {k: svc.change(data[k], before[k]) for k in COMPARED}
    return data
