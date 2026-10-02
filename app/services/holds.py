"""Fulfillment holds for arrive-by gift orders (docs/TECHNICAL_PLAN.md §6.5).

Scope: write_merchant_managed_fulfillment_orders, so only fulfillment orders
the merchant ships themselves can be held. Shopify lists what we may do on
each one (supportedActions); 3PL-fulfilled ones simply don't offer HOLD and
are left alone (the order is still tagged with its ship-by date).

Our holds carry handle HOLD_HANDLE, so release only ever touches holds we
placed, never one the merchant added themselves.
"""
from datetime import date

import structlog

from core.shopify_graphql import shopify_graphql_post

logger = structlog.get_logger()

HOLD_HANDLE = "giftsense-arrive-by"

FULFILLMENT_ORDERS = """
query($id: ID!) {
  order(id: $id) {
    fulfillmentOrders(first: 20) {
      nodes { id status supportedActions { action } fulfillmentHolds { id handle } }
    }
  }
}
"""
HOLD = """
mutation($id: ID!, $hold: FulfillmentOrderHoldInput!) {
  fulfillmentOrderHold(id: $id, fulfillmentHold: $hold) {
    fulfillmentHold { id }
    userErrors { field message }
  }
}
"""
RELEASE = """
mutation($id: ID!, $holdIds: [ID!]) {
  fulfillmentOrderReleaseHold(id: $id, holdIds: $holdIds) {
    fulfillmentOrder { id status }
    userErrors { field message }
  }
}
"""


def _pretty(d: str) -> str:
    x = date.fromisoformat(d)
    return f"{x:%b} {x.day}"


async def _fulfillment_orders(shop_domain: str, token: str, order_gid: str, gql) -> list[dict] | None:
    r = await gql(shop_domain, token, FULFILLMENT_ORDERS, {"id": order_gid})
    if r.status_code != 200:
        return None
    return ((((r.json().get("data") or {}).get("order") or {}).get("fulfillmentOrders") or {}).get("nodes")) or []


async def hold_order(shop_domain: str, token: str, order_gid: str, ship_by: str, arrive_by: str,
                     gql=shopify_graphql_post) -> str:
    """Hold every fulfillment order we're allowed to. "held" | "none" (nothing
    holdable, e.g. all 3PL) | "failed"."""
    nodes = await _fulfillment_orders(shop_domain, token, order_gid, gql)
    if nodes is None:
        return "failed"
    holdable = [n for n in nodes if any(a.get("action") == "HOLD" for a in n.get("supportedActions") or [])]
    if not holdable:
        return "none"
    note = f"GiftSense: ship on {_pretty(ship_by)} to arrive by {_pretty(arrive_by)} (estimated)."
    held = 0
    for n in holdable:
        r = await gql(shop_domain, token, HOLD, {"id": n["id"], "hold": {
            "reason": "OTHER", "reasonNotes": note, "handle": HOLD_HANDLE, "notifyMerchant": False}})
        errors = (((r.json().get("data") or {}).get("fulfillmentOrderHold") or {}).get("userErrors")
                  if r.status_code == 200 else [{"message": f"HTTP {r.status_code}"}])
        if errors:
            logger.warning("gift_hold_failed", shop=shop_domain, fulfillment_order=n["id"], errors=errors)
        else:
            held += 1
    return "held" if held else "failed"


async def release_order(shop_domain: str, token: str, order_gid: str, gql=shopify_graphql_post) -> str:
    """Release our holds on the order. "released" (also when none are left,
    e.g. the merchant released or fulfilled it already) | "failed"."""
    nodes = await _fulfillment_orders(shop_domain, token, order_gid, gql)
    if nodes is None:
        return "failed"
    ok = True
    for n in nodes:
        ours = [h["id"] for h in n.get("fulfillmentHolds") or [] if h.get("handle") == HOLD_HANDLE]
        if not ours:
            continue
        r = await gql(shop_domain, token, RELEASE, {"id": n["id"], "holdIds": ours})
        errors = (((r.json().get("data") or {}).get("fulfillmentOrderReleaseHold") or {}).get("userErrors")
                  if r.status_code == 200 else [{"message": f"HTTP {r.status_code}"}])
        if errors:
            ok = False
            logger.warning("gift_release_failed", shop=shop_domain, fulfillment_order=n["id"], errors=errors)
    return "released" if ok else "failed"
