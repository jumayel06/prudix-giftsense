"""GET /print/gifts?orderIds=gid://shopify/Order/N[,…]&docs=cards,receipt

The printable page behind the admin Print actions (extensions/giftsense-print,
order page and orders-list bulk print):
  cards   one card per gift group ("For Mom", her note, her items) in "I'll give
          it to them" orders, or one card with the order's gift note otherwise
  receipt a price-free gift receipt per order (plan §6.7: replaces editing the
          packing-slip template); gift items grouped by recipient, no prices

Auth: the admin sends a session token (Authorization: Bearer, or `id_token`
for document loads). Gift data is read from the order itself (cart
attributes and line properties, parsed like the orders/create webhook), so
it prints even before our webhook job ran. No customer fields are queried.
"""
import html
import re

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

# No customer fields and no prices: receipts must never show them.
ORDERS_QUERY = """
query($ids: [ID!]!) {
  shop { name }
  nodes(ids: $ids) {
    ... on Order {
      id
      name
      customAttributes { key value }
      lineItems(first: 100) { nodes { name quantity customAttributes { key value } } }
    }
  }
}
"""
MAX_ORDERS = 50
DOCS = {"cards", "receipt"}

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
@page card { size: 5in 7in; margin: 0.45in; }
@page receipt { size: letter; margin: 0.7in; }
* { box-sizing: border-box; }
body { margin: 0; font-family: Georgia, "Times New Roman", serif; color: #1f2937; }
.card { page: card; page-break-after: always; break-after: page; min-height: 6in; display: flex; flex-direction: column;
        justify-content: center; text-align: center; padding: 0.3in; }
.card:last-child { page-break-after: auto; break-after: auto; }
.for { font-size: 14pt; letter-spacing: .08em; text-transform: uppercase; color: #6b7280; margin: 0 0 .3in; }
.note { font-size: 18pt; line-height: 1.5; font-style: italic; margin: 0 0 .3in; white-space: pre-wrap; }
.items { list-style: none; padding: 0; margin: 0 0 .3in; font-size: 10pt; color: #6b7280; }
.from { font-size: 10pt; color: #9ca3af; margin: 0; }
.receipt { page: receipt; page-break-after: always; break-after: page;
           font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; font-size: 11pt; }
.receipt:last-child { page-break-after: auto; break-after: auto; }
.receipt .shop { font-size: 14pt; font-weight: 700; margin: 0; }
.receipt h1 { font-size: 20pt; margin: .1in 0; }
.receipt .meta { color: #6b7280; margin: 0 0 .25in; }
.receipt h2 { font-size: 12pt; margin: .2in 0 .05in; border-bottom: 1px solid #e5e7eb; padding-bottom: .04in; }
.receipt ul { margin: 0; padding-left: .2in; }
.receipt .footer { margin-top: .4in; color: #6b7280; font-size: 9.5pt; }
.empty-page { font-family: -apple-system, sans-serif; padding: 1in; text-align: center; color: #6b7280; }
"""


def _cards(order: dict, parsed, shop_name: str) -> list[str]:
    items = _items_by_group(order)
    if parsed.groups:
        return [_card(f"For {g['label']}" if g["label"] else "A gift for you", g.get("note"),
                      items.get(g["id"], []), shop_name) for g in parsed.groups]
    return [_card("A gift for you", parsed.note, [], shop_name)]


def _all_items(order: dict) -> list[str]:
    out = []
    for li in (order.get("lineItems") or {}).get("nodes") or []:
        qty = int(li.get("quantity") or 1)
        out.append(f"{li.get('name', '')}{f' × {qty}' if qty > 1 else ''}")
    return out


def _receipt(order: dict, parsed, shop_name: str) -> str:
    esc = html.escape
    by_group = _items_by_group(order)
    sections: list[tuple[str, list[str]]] = []
    if parsed.groups:
        sections = [(f"For {g['label']}" if g["label"] else "Gift", by_group.get(g["id"], [])) for g in parsed.groups]
    else:
        gift_items = [i for items in by_group.values() for i in items]
        sections = [("Items", gift_items or _all_items(order))]
    body = "".join(
        f'<h2>{esc(title)}</h2><ul>{"".join(f"<li>{esc(i)}</li>" for i in items)}</ul>'
        for title, items in sections if items
    )
    name = esc(order.get("name") or "")
    return (f'<section class="receipt"><p class="shop">{esc(shop_name)}</p><h1>Gift receipt</h1>'
            f'<p class="meta">Order {name}</p>{body}'
            f'<p class="footer">Prices are not shown on this gift receipt. To exchange an item, contact '
            f'{esc(shop_name)} with order {name}.</p></section>')


def render_documents(orders: list[dict], shop_name: str, docs: set[str]) -> str:
    esc = html.escape
    parts: list[str] = []
    names = []
    for order in orders:
        names.append(order.get("name") or "")
        parsed = parse_gift_order(_as_webhook_shape(order))
        if parsed is None:
            continue
        if "cards" in docs:
            parts.extend(_cards(order, parsed, shop_name))
        if "receipt" in docs:
            parts.append(_receipt(order, parsed, shop_name))
    body = "".join(parts) or (f'<p class="empty-page">{esc(", ".join(n for n in names if n) or "This order")} has no '
                              "GiftSense gift details to print.</p>")
    title = "Gift cards" if docs == {"cards"} else "Gift receipt" if docs == {"receipt"} else "Gift documents"
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{title} · '
            f'{esc(", ".join(names[:3]))}</title><style>{PAGE_CSS}</style></head><body>{body}</body></html>')


@router.get("/print/gifts")
async def print_gifts(orderIds: str = Query(..., max_length=MAX_ORDERS * 40),  # noqa: N803
                      docs: str = Query("cards"),
                      shop: Shop = Depends(print_shop), db: AsyncSession = Depends(get_db)):
    ids = [i for i in orderIds.split(",") if i]
    wanted = {d for d in docs.split(",") if d}
    if not ids or len(ids) > MAX_ORDERS or not all(re.fullmatch(r"gid://shopify/Order/\d+", i) for i in ids):
        raise HTTPException(422, f"orderIds: 1–{MAX_ORDERS} order ids")
    if not wanted or not wanted <= DOCS:
        raise HTTPException(422, "docs: cards and/or receipt")
    token = await get_valid_access_token(shop, db)
    resp = await shopify_graphql_post(shop.shop_domain, token, ORDERS_QUERY, {"ids": ids})
    data = (resp.json().get("data") or {}) if resp.status_code == 200 else {}
    orders = [o for o in data.get("nodes") or [] if o and o.get("id")]
    if not orders:
        raise HTTPException(404, "Order not found")
    page = render_documents(orders, (data.get("shop") or {}).get("name") or shop.shop_domain, wanted)
    return HTMLResponse(page, headers={"Access-Control-Allow-Origin": ADMIN_ORIGIN, "Vary": "Origin",
                                       "Cache-Control": "no-store"})
