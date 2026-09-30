"""Gift-order jobs (enqueued by the orders/create webhook).

annotate_gift_order: tag the order "GiftSense" and write the
$app:giftsense.gifts metafield; creates the metafield definition once per shop.
"""
import structlog
from sqlalchemy import select

from app.services import gift_orders
from core.db.models import GiftOrder, Shop
from core.db.session import AsyncSessionLocal
from core.shopify_auth import get_valid_access_token

logger = structlog.get_logger()


async def annotate_gift_order(ctx: dict, shop_domain: str, order_gid: str, value: dict) -> None:
    async with AsyncSessionLocal() as db:
        shop = (await db.execute(select(Shop).where(Shop.shop_domain == shop_domain))).scalar_one_or_none()
        if shop is None or shop.plan_status in ("uninstalled", "purged"):
            return
        token = await get_valid_access_token(shop, db)
        settings = dict(shop.gift_settings or {})
        if not settings.get("_order_metafield_defined"):
            if await gift_orders.ensure_definition(shop_domain, token):
                shop.gift_settings = {**settings, "_order_metafield_defined": True}
                await db.commit()
        ok = await gift_orders.annotate_order(shop_domain, token, order_gid, value)
        if ok:
            order_id = order_gid.rsplit("/", 1)[-1]
            row = (await db.execute(select(GiftOrder).where(
                GiftOrder.shop_id == shop.id, GiftOrder.order_id == order_id))).scalar_one_or_none()
            if row is not None:
                row.annotated = True
                await db.commit()
