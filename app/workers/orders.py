"""Gift-order jobs (enqueued by the orders/create webhook and Settings).

annotate_gift_order: tag the order "GiftSense" and write the
$app:giftsense.gifts metafield; creates the metafield definition once per shop.
sync_wrap: create/update the hidden Gift wrap product after a wrap settings save.
release_due_holds (hourly cron): release arrive-by fulfillment holds on their ship-by day.
reconcile_orders (hourly cron): orders from the last 48 h that no orders/create
webhook brought in (delivery isn't guaranteed) go through the webhook's handler.
"""
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select

from app.services import delivery, gift_orders, holds, wrap
from core.db.models import CountedOrder, GiftOrder, Shop
from core.db.session import AsyncSessionLocal
from core.shopify_auth import get_valid_access_token
from core.shopify_graphql import shopify_graphql_post

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
        order_id = order_gid.rsplit("/", 1)[-1]
        row = (await db.execute(select(GiftOrder).where(
            GiftOrder.shop_id == shop.id, GiftOrder.order_id == order_id))).scalar_one_or_none()
        if row is None:
            return
        if ok:
            row.annotated = True
        if row.hold_status in ("pending", "late") and row.ship_by:
            await _schedule_shipment(shop, token, order_gid, row)
        await db.commit()


async def _schedule_shipment(shop, token: str, order_gid: str, row: GiftOrder) -> None:
    """Arrive-by: tag the order with its ship-by date and hold what the merchant
    ships until then (app/services/holds.py). A date that can't be met anymore
    is tagged to ship as soon as possible and never held."""
    late = row.hold_status == "late"
    tags = [f"giftsense-ship-by-{row.ship_by.isoformat()}", "giftsense-ship-asap" if late else "giftsense-scheduled"]
    await shopify_graphql_post(shop.shop_domain, token, gift_orders.TAG_MUTATION, {"id": order_gid, "tags": tags})
    if late:
        return
    if row.ship_by <= delivery.store_today(shop.store_timezone).date():
        row.hold_status = "none"            # due today: nothing to hold
        return
    row.hold_status = await holds.hold_order(shop.shop_domain, token, order_gid,
                                             row.ship_by.isoformat(), row.arrive_by.isoformat())


async def release_due_holds(ctx: dict) -> None:
    """Hourly: release arrive-by holds whose ship-by day has come (store
    timezone). A failed release stays "held" and is retried next hour."""
    cutoff = datetime.now(timezone.utc).date() + timedelta(days=1)    # widest timezone; filtered per shop below
    async with AsyncSessionLocal() as db:
        due = [tuple(r) for r in (await db.execute(
            select(GiftOrder.id, Shop.shop_domain).join(Shop, Shop.id == GiftOrder.shop_id).where(
                GiftOrder.hold_status == "held", GiftOrder.ship_by <= cutoff,
                Shop.plan_status.in_(("active", "trial_active", "cancelled"))))).all()]
    released = 0
    for row_id, domain in due:
        try:
            async with AsyncSessionLocal() as db:
                row = await db.get(GiftOrder, row_id)
                shop = (await db.execute(select(Shop).where(Shop.shop_domain == domain))).scalar_one()
                if row.ship_by > delivery.store_today(shop.store_timezone).date():
                    continue
                token = await get_valid_access_token(shop, db)
                result = await holds.release_order(domain, token, f"gid://shopify/Order/{row.order_id}")
                if result == "released":
                    row.hold_status = "released"
                    released += 1
                await db.commit()
        except Exception as e:  # noqa: BLE001 — one shop's failure mustn't stop the rest
            logger.warning("release_due_hold_failed", shop=domain, error=str(e)[:200])
    if due:
        logger.info("release_due_holds", candidates=len(due), released=released)


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


# ── Missed orders/create webhooks ─────────────────────────────────────────────
RECONCILE_WINDOW = timedelta(hours=48)
ORDER_IDS_QUERY = """
query($q: String!, $after: String) {
  orders(first: 250, after: $after, query: $q, sortKey: CREATED_AT) {
    pageInfo { hasNextPage endCursor }
    nodes { id legacyResourceId }
  }
}
"""
# Only the fields parse_gift_order and the counter read, renamed to the
# orders/create webhook's shape below. 10 orders × 50 lines keeps the query
# cost well under Shopify's 1,000-point limit.
ORDERS_DETAIL_QUERY = """
query($ids: [ID!]!) {
  nodes(ids: $ids) {
    ... on Order {
      id legacyResourceId name createdAt
      totalPriceSet { shopMoney { amount currencyCode } }
      customAttributes { key value }
      lineItems(first: 50) { nodes { quantity originalUnitPriceSet { shopMoney { amount } } customAttributes { key value } } }
    }
  }
}
"""


def webhook_payload(order: dict) -> dict:
    """A GraphQL Order → the orders/create webhook fields our handler reads."""
    money = (order.get("totalPriceSet") or {}).get("shopMoney") or {}
    pairs = lambda attrs: [{"name": a["key"], "value": a.get("value")} for a in attrs or []]
    return {
        "id": order["legacyResourceId"],
        "admin_graphql_api_id": order["id"],
        "name": order.get("name"),
        "created_at": order.get("createdAt"),
        "total_price": money.get("amount"),
        "currency": money.get("currencyCode"),
        "note_attributes": pairs(order.get("customAttributes")),
        "line_items": [{"quantity": li.get("quantity"),
                        "price": ((li.get("originalUnitPriceSet") or {}).get("shopMoney") or {}).get("amount"),
                        "properties": pairs(li.get("customAttributes"))}
                       for li in ((order.get("lineItems") or {}).get("nodes") or [])],
    }


async def _recent_order_ids(shop_domain: str, token: str, since: datetime) -> dict[str, str]:
    """{numeric id: gid} of orders created since `since`."""
    out, after = {}, None
    q = f"created_at:>='{since.strftime('%Y-%m-%dT%H:%M:%SZ')}'"
    while True:
        resp = await shopify_graphql_post(shop_domain, token, ORDER_IDS_QUERY, {"q": q, "after": after})
        data = ((resp.json() or {}).get("data") or {}).get("orders") if resp.status_code == 200 else None
        if data is None:
            raise RuntimeError(f"orders query failed: HTTP {resp.status_code}")
        out.update({str(n["legacyResourceId"]): n["id"] for n in data["nodes"]})
        if not data["pageInfo"]["hasNextPage"]:
            return out
        after = data["pageInfo"]["endCursor"]


async def reconcile_orders(ctx: dict) -> None:
    """Hourly: count (and record as gift orders) any order from the last 48 h
    that hasn't been counted — its webhook was missed. Same handler as the
    webhook, and counted_orders + gift_orders' unique order id make a second
    pass a no-op, so nothing is ever counted twice."""
    from app.routes.webhooks import _handle_order_created   # the webhook's own handler

    async with AsyncSessionLocal() as db:
        domains = [d for (d,) in (await db.execute(
            select(Shop.shop_domain).where(Shop.plan_status.in_(("active", "trial_active"))))).all()]
    since = datetime.now(timezone.utc) - RECONCILE_WINDOW
    recovered = 0
    for domain in domains:
        try:
            async with AsyncSessionLocal() as db:
                shop = (await db.execute(select(Shop).where(Shop.shop_domain == domain))).scalar_one()
                token = await get_valid_access_token(shop, db)
                recent = await _recent_order_ids(domain, token, since)
                if not recent:
                    continue
                counted = {o for (o,) in (await db.execute(select(CountedOrder.order_id).where(
                    CountedOrder.shop_id == shop.id, CountedOrder.order_id.in_(list(recent))))).all()}
                missing = [gid for oid, gid in recent.items() if oid not in counted]
                for i in range(0, len(missing), 10):
                    resp = await shopify_graphql_post(domain, token, ORDERS_DETAIL_QUERY, {"ids": missing[i:i + 10]})
                    nodes = ((resp.json() or {}).get("data") or {}).get("nodes") if resp.status_code == 200 else None
                    if nodes is None:
                        raise RuntimeError(f"order details query failed: HTTP {resp.status_code}")
                    for order in nodes:
                        if order and order.get("legacyResourceId"):
                            await _handle_order_created(domain, webhook_payload(order), db)
                            recovered += 1
        except Exception as e:  # noqa: BLE001 — one shop's failure mustn't stop the rest
            logger.warning("reconcile_orders_failed", shop=domain, error=str(e)[:200])
    if recovered:
        logger.info("reconcile_orders_recovered", orders=recovered)
