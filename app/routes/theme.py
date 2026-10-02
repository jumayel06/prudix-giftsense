"""GET /api/theme/status: is the gift finder's app embed switched on in the
live theme, and where are its blocks? Drives the Storefront setup page and
refreshes the dashboard-wide warning (app/services/theme_status.py)."""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.theme_status import fetch_embed_status, remember
from core.db.models import Shop
from core.db.session import get_db
from core.shopify_auth import get_valid_access_token
from core.shopify_deps import get_current_shop

router = APIRouter()


@router.get("/api/theme/status")
async def theme_status(shop: Shop = Depends(get_current_shop), db: AsyncSession = Depends(get_db)):
    token = await get_valid_access_token(shop, db)
    result = await fetch_embed_status(shop.shop_domain, token)
    remember(shop, result)
    await db.commit()
    return result
