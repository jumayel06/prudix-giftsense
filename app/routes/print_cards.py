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
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

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

# No customer fields and no prices: receipts must never show them. The shop's
# contactEmail is the store's public support address, not a customer's.
ORDERS_QUERY = """
query($ids: [ID!]!) {
  shop { name contactEmail primaryDomain { host } }
  nodes(ids: $ids) {
    ... on Order {
      id
      name
      createdAt
      customAttributes { key value }
      lineItems(first: 100) { nodes { name title variantTitle quantity customAttributes { key value } } }
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


@dataclass
class StoreInfo:
    name: str
    host: str = ""          # e.g. "snowandco.com", for the receipt's exchange box
    email: str = ""         # the store's public contact email
    timezone: str = "UTC"


def _order_date(order: dict, tz: str) -> str:
    try:
        when = datetime.fromisoformat(str(order.get("createdAt")).replace("Z", "+00:00"))
        when = when.astimezone(ZoneInfo(tz or "UTC"))
    except (ValueError, TypeError, KeyError):
        return ""
    return f"{when:%B} {when.day}, {when.year}"


def _receipt_lines(order: dict, gift_id: str | None) -> list[tuple[str, str, int]]:
    """(item, option, quantity) for one gift group (gift_id), all gift items
    (gift_id="*") or every line (None)."""
    out = []
    for li in (order.get("lineItems") or {}).get("nodes") or []:
        attrs = {a.get("key"): a.get("value") for a in li.get("customAttributes") or []}
        gid = attrs.get("_giftsense_gift")
        if gift_id == "*" and not gid or gift_id not in (None, "*") and gid != gift_id:
            continue
        option = li.get("variantTitle") or ""
        out.append((li.get("title") or li.get("name") or "", "" if option == "Default Title" else option,
                    int(li.get("quantity") or 1)))
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
.receipt { page: receipt; page-break-after: always; break-after: page; color: #1f2937;
           font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; font-size: 11pt; }
.receipt:last-child { page-break-after: auto; break-after: auto; }
.r-head { display: flex; justify-content: space-between; align-items: flex-end; gap: .3in;
          border-bottom: 2px solid #1f2937; padding-bottom: .15in; margin-bottom: .3in; }
.r-shop { font-size: 16pt; font-weight: 700; margin: 0; }
.r-host { color: #6b7280; margin: .03in 0 0; font-size: 10pt; }
.r-title { text-align: right; }
.r-title h1 { font-size: 13pt; letter-spacing: .12em; text-transform: uppercase; margin: 0; }
.r-title p { color: #6b7280; margin: .04in 0 0; font-size: 10pt; }
.r-for { font-family: Georgia, "Times New Roman", serif; font-style: italic; font-size: 13pt; margin: .25in 0 .08in; }
.r-items { width: 100%; border-collapse: collapse; }
.r-items th { text-align: left; font-size: 8.5pt; letter-spacing: .08em; text-transform: uppercase; color: #6b7280;
              border-bottom: 1px solid #d1d5db; padding: .06in 0; }
.r-items td { border-bottom: 1px solid #eef0f3; padding: .1in 0; vertical-align: top; }
.r-items .qty { width: .7in; text-align: right; }
.r-items .opt { width: 2.2in; color: #4b5563; }
.r-exchange { margin-top: .45in; border: 1px solid #d1d5db; border-radius: 6px; padding: .16in .2in; }
.r-exchange h2 { font-size: 10pt; letter-spacing: .08em; text-transform: uppercase; margin: 0 0 .06in; }
.r-exchange p { margin: .03in 0; }
.r-foot { margin-top: .3in; color: #9ca3af; font-size: 9pt; text-align: center; }
.empty-page { font-family: -apple-system, sans-serif; padding: 1in; text-align: center; color: #6b7280; }
"""


def _cards(order: dict, parsed, shop_name: str) -> list[str]:
    items = _items_by_group(order)
    if parsed.groups:
        return [_card(f"For {g['label']}" if g["label"] else "A gift for you", g.get("note"),
                      items.get(g["id"], []), shop_name) for g in parsed.groups]
    return [_card("A gift for you", parsed.note, [], shop_name)]


def _items_table(rows: list[tuple[str, str, int]]) -> str:
    esc = html.escape
    has_opts = any(opt for _, opt, _ in rows)
    head = "<tr><th>Item</th>" + ("<th>Option</th>" if has_opts else "") + '<th class="qty">Qty</th></tr>'
    body = "".join(f"<tr><td>{esc(item)}</td>" + (f'<td class="opt">{esc(opt)}</td>' if has_opts else "")
                   + f'<td class="qty">{qty}</td></tr>' for item, opt, qty in rows)
    return f'<table class="r-items">{head}{body}</table>'


def _receipt(order: dict, parsed, store: StoreInfo) -> str:
    """A price-free gift receipt: store, order number and date, the gift items
    (per recipient in "I'll give it to them" orders) with size/colour and
    quantity, and how to exchange. Never prices or customer details."""
    esc = html.escape
    if parsed.groups:
        sections = [(f"A gift for {g['label']}" if g["label"] else "", _receipt_lines(order, g["id"]))
                    for g in parsed.groups]
    else:
        sections = [("", _receipt_lines(order, "*") or _receipt_lines(order, None))]
    body = "".join((f'<p class="r-for">{esc(title)}</p>' if title else "") + _items_table(rows)
                   for title, rows in sections if rows)
    name = esc(order.get("name") or "")
    date = _order_date(order, store.timezone)
    contact = " or ".join(x for x in (f"<b>{esc(store.email)}</b>" if store.email else "",
                                      f"<b>{esc(store.host)}</b>" if store.host else "") if x)
    how = (f"Contact {esc(store.name)} at {contact}" if contact else f"Contact {esc(store.name)}")
    return (f'<section class="receipt">'
            f'<header class="r-head"><div><p class="r-shop">{esc(store.name)}</p>'
            + (f'<p class="r-host">{esc(store.host)}</p>' if store.host else "")
            + f'</div><div class="r-title"><h1>Gift receipt</h1><p>Order {name}{f" · {esc(date)}" if date else ""}</p>'
            f'</div></header>{body}'
            f'<div class="r-exchange"><h2>Exchanges</h2><p>{how} and mention order <b>{name}</b>.</p>'
            f'<p>Exchanges follow {esc(store.name)}&#8217;s return policy.</p></div>'
            f'<p class="r-foot">This gift receipt does not show prices.</p></section>')


def render_documents(orders: list[dict], store: StoreInfo | str, docs: set[str]) -> str:
    store = store if isinstance(store, StoreInfo) else StoreInfo(name=store)
    shop_name = store.name
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
            parts.append(_receipt(order, parsed, store))
    body = "".join(parts) or (f'<p class="empty-page">{esc(", ".join(n for n in names if n) or "This order")} has no '
                              "GiftSense gift details to print.</p>")
    title = "Gift note cards" if docs == {"cards"} else "Gift receipt" if docs == {"receipt"} else "Gift documents"
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
    s = data.get("shop") or {}
    store = StoreInfo(name=s.get("name") or shop.shop_domain, host=(s.get("primaryDomain") or {}).get("host") or "",
                      email=s.get("contactEmail") or "", timezone=shop.store_timezone or "UTC")
    page = render_documents(orders, store, wanted)
    return HTMLResponse(page, headers={"Access-Control-Allow-Origin": ADMIN_ORIGIN, "Vary": "Origin",
                                       "Cache-Control": "no-store"})
