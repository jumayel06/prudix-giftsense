"""GET /api/theme/status: is the gift finder's app embed switched on in the
live theme? Drives the Storefront setup page (app/services/theme_status.py)."""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.theme_status import fetch_embed_status
from core.db.models import Shop
from core.db.session import get_db
from core.shopify_auth import get_valid_access_token
from core.shopify_deps import get_current_shop

router = APIRouter()


@router.get("/api/theme/status")
async def theme_status(shop: Shop = Depends(get_current_shop), db: AsyncSession = Depends(get_db)):
    token = await get_valid_access_token(shop, db)
    return await fetch_embed_status(shop.shop_domain, token)
