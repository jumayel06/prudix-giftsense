"""Gift-order jobs (enqueued by the orders/create webhook and Settings).

annotate_gift_order: tag the order "GiftSense" and write the
$app:giftsense.gifts metafield; creates the metafield definition once per shop.
sync_wrap: create/update the hidden Gift wrap product after a wrap settings save.
"""
import structlog
from sqlalchemy import select

from app.services import gift_orders, wrap
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


async def sync_wrap(ctx: dict, shop_domain: str) -> None:
    """Create/update the hidden Gift wrap product (app/services/wrap.py).

    The Shopify calls happen outside the DB transaction; if the merchant edited
    the styles meanwhile, the newer save queued its own sync, so drop ours."""
    async with AsyncSessionLocal() as db:
        shop = (await db.execute(select(Shop).where(Shop.shop_domain == shop_domain))).scalar_one_or_none()
        if shop is None or shop.plan_status in ("uninstalled", "purged"):
            return
        token = await get_valid_access_token(shop, db)
        before = wrap.wrap_settings(shop)
        await db.commit()
    result = await wrap.sync_wrap_product(shop_domain, token, before)
    async with AsyncSessionLocal() as db:
        shop = (await db.execute(select(Shop).where(Shop.shop_domain == shop_domain))).scalar_one_or_none()
        if shop is None:
            return
        now = wrap.wrap_settings(shop)
        if wrap.style_key(now["styles"]) != wrap.style_key(before["styles"]):
            logger.info("wrap_sync_superseded", shop=shop_domain)
            return
        shop.gift_settings = {**(shop.gift_settings or {}), "wrap": {**result, "enabled": now["enabled"]}}
        await db.commit()
