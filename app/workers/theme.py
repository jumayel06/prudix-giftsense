"""Theme jobs (enqueued by the themes/publish webhook).

check_theme: a new live theme starts with GiftSense's app embed off and none
of its sections placed (they're saved per theme). Re-check it so the dashboard
can warn the merchant (app/services/theme_status.py).
"""
from sqlalchemy import select

from app.services import theme_status
from core.db.models import Shop
from core.db.session import AsyncSessionLocal
from core.shopify_auth import get_valid_access_token


async def check_theme(ctx: dict, shop_domain: str) -> None:
    async with AsyncSessionLocal() as db:
        shop = (await db.execute(select(Shop).where(Shop.shop_domain == shop_domain))).scalar_one_or_none()
        if shop is None or shop.plan_status in ("uninstalled", "purged"):
            return
        token = await get_valid_access_token(shop, db)
        theme_status.remember(shop, await theme_status.fetch_embed_status(shop_domain, token))
        await db.commit()
