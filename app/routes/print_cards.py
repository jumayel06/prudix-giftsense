"""GET /print/gift-cards?orderId=gid://shopify/Order/N

The printable page behind the admin order Print action
(extensions/giftsense-print): one card per gift group ("For Mom", her note,
her items) in "I'll give it to them" orders, or one card with the order's
gift note for "ship straight to them" orders.

Auth: the admin sends a session token (Authorization: Bearer, or `id_token`
for document loads). Gift data is read from the order itself (cart
attributes and line properties, parsed like the orders/create webhook), so
it prints even before our webhook job ran. No customer fields are queried.
"""
import html

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.gift_orders import parse_gift_order
from core.config import settings
from core.db.models import Shop
from core.db.session import get_db
from core.shopify_auth import get_shop_from_session_token, get_valid_access_token
from core.shopify_deps import _SHOP_PARAM_FALLBACK_ENVS
from core.shopify_graphql import shopify_graphql_post

router = APIRouter()

ORDER_QUERY = """
query($id: ID!) {
  shop { name }
  order(id: $id) {
    id
    name
    customAttributes { key value }
    lineItems(first: 100) { nodes { name quantity customAttributes { key value } } }
  }
}
"""

ADMIN_ORIGIN = "https://admin.shopify.com"


async def print_shop(request: Request, db: AsyncSession = Depends(get_db)) -> Shop:
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else request.query_params.get("id_token")
    domain = None
    if token:
        try:
            domain = get_shop_from_session_token(token)
        except Exception:  # noqa: BLE001 — any invalid token is a 401
            raise HTTPException(401, "Invalid session token")
    elif settings.app_env in _SHOP_PARAM_FALLBACK_ENVS:
        domain = request.query_params.get("shop")
    if not domain:
        raise HTTPException(401, "Missing session token")
    shop = (await db.execute(select(Shop).where(Shop.shop_domain == domain))).scalar_one_or_none()
    if shop is None or shop.plan_status in ("uninstalled", "purged"):
        raise HTTPException(404, "Shop not found")
    return shop


def _as_webhook_shape(order: dict) -> dict:
    """GraphQL order → the orders/create payload shape parse_gift_order reads."""
    def pairs(items):
        return [{"name": a.get("key"), "value": a.get("value")} for a in items or []]
    return {
        "id": str(order["id"]).rsplit("/", 1)[-1], "admin_graphql_api_id": order["id"], "name": order.get("name"),
        "note_attributes": pairs(order.get("customAttributes")),
        "line_items": [{"title": li.get("name"), "quantity": li.get("quantity"), "price": "0",
                        "properties": pairs(li.get("customAttributes"))}
                       for li in (order.get("lineItems") or {}).get("nodes") or []],
    }


def _items_by_group(order: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for li in (order.get("lineItems") or {}).get("nodes") or []:
        attrs = {a.get("key"): a.get("value") for a in li.get("customAttributes") or []}
        gid = attrs.get("_giftsense_gift")
        if gid:
            qty = int(li.get("quantity") or 1)
            out.setdefault(gid, []).append(f"{li.get('name', '')}{f' × {qty}' if qty > 1 else ''}")
    return out


def _card(heading: str, note: str | None, items: list[str], shop_name: str) -> str:
    esc = html.escape
    note_html = f'<p class="note">{esc(note)}</p>' if note else '<p class="note empty">&nbsp;</p>'
    items_html = ("<ul class=\"items\">" + "".join(f"<li>{esc(i)}</li>" for i in items) + "</ul>") if items else ""
    return (f'<section class="card"><p class="for">{esc(heading)}</p>{note_html}{items_html}'
            f'<p class="from">{esc(shop_name)}</p></section>')


PAGE_CSS = """
@page { size: 5in 7in; margin: 0.45in; }
* { box-sizing: border-box; }
body { margin: 0; font-family: Georgia, "Times New Roman", serif; color: #1f2937; }
.card { page-break-after: always; break-after: page; min-height: 6in; display: flex; flex-direction: column;
        justify-content: center; text-align: center; padding: 0.3in; }
.card:last-child { page-break-after: auto; break-after: auto; }
.for { font-size: 14pt; letter-spacing: .08em; text-transform: uppercase; color: #6b7280; margin: 0 0 .3in; }
.note { font-size: 18pt; line-height: 1.5; font-style: italic; margin: 0 0 .3in; white-space: pre-wrap; }
.items { list-style: none; padding: 0; margin: 0 0 .3in; font-size: 10pt; color: #6b7280; }
.from { font-size: 10pt; color: #9ca3af; margin: 0; }
.empty-page { font-family: -apple-system, sans-serif; padding: 1in; text-align: center; color: #6b7280; }
"""


def render_cards(order: dict, shop_name: str) -> str:
    parsed = parse_gift_order(_as_webhook_shape(order))
    esc = html.escape
    cards: list[str] = []
    if parsed is not None:
        items = _items_by_group(order)
        if parsed.groups:
            for g in parsed.groups:
                cards.append(_card(f"For {g['label']}" if g["label"] else "A gift for you", g.get("note"),
                                   items.get(g["id"], []), shop_name))
        elif parsed.note or parsed.gift_lines:
            cards.append(_card("A gift for you", parsed.note, [], shop_name))
    body = "".join(cards) or (f'<p class="empty-page">Order {esc(order.get("name") or "")} has no GiftSense gift '
                              "details to print.</p>")
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Gift cards · '
            f'{esc(order.get("name") or "")}</title><style>{PAGE_CSS}</style></head><body>{body}</body></html>')


@router.get("/print/gift-cards")
async def print_gift_cards(orderId: str = Query(..., pattern=r"^gid://shopify/Order/\d+$"),  # noqa: N803
                           shop: Shop = Depends(print_shop), db: AsyncSession = Depends(get_db)):
    token = await get_valid_access_token(shop, db)
    resp = await shopify_graphql_post(shop.shop_domain, token, ORDER_QUERY, {"id": orderId})
    data = (resp.json().get("data") or {}) if resp.status_code == 200 else {}
    order = data.get("order")
    if not order:
        raise HTTPException(404, "Order not found")
    page = render_cards(order, (data.get("shop") or {}).get("name") or shop.shop_domain)
    return HTMLResponse(page, headers={"Access-Control-Allow-Origin": ADMIN_ORIGIN, "Vary": "Origin",
                                       "Cache-Control": "no-store"})
