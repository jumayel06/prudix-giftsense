"""Gift orders from the orders/create webhook (docs/TECHNICAL_PLAN.md §5.3, §6.3).

An order is a gift order when it carries what the widget/gift panel wrote to
the cart: `_giftsense_gift` / `Gift for` line properties, the `_giftsense_sid`
attribution, a `Gift note`, or `_giftsense_gifts` groups. We store only order
facts (ids, amounts, groups summary, note source) in gift_orders: never the
customer, and never the note text (it stays on the Shopify order).

Then a job tags the order "GiftSense" and writes the `$app:giftsense.gifts`
order metafield (groups + notes) that print actions and the Thank-you page read.
"""
import difflib
import json
import uuid
from dataclasses import dataclass, field

import structlog

from core.shopify_graphql import shopify_graphql_post

logger = structlog.get_logger()

METAFIELD_NAMESPACE = "$app:giftsense"
METAFIELD_KEY = "gifts"
PACKING_SLIP_NAMESPACE = "giftsense"
ORDER_TAG = "GiftSense"
MAX_GROUPS = 20
MAX_LABEL = 40
MAX_NOTE = 1000
ACCEPTED, EDITED_BELOW = 0.02, 0.30   # normalized edit distance thresholds


@dataclass
class ParsedGiftOrder:
    order_id: str
    order_gid: str
    order_name: str
    sid: uuid.UUID | None
    delivery_mode: str | None
    gift_lines: int
    gift_revenue: float
    order_total: float
    currency: str
    note: str | None                       # direct-mode "Gift note" (not stored in our DB)
    groups: list[dict] = field(default_factory=list)
    arrive_by: str | None = None
    gift_receipt: bool = False
    wrap: str | None = None                # direct-mode wrap style
    card: str | None = None                # direct-mode greeting card
    message: str | None = None             # direct-mode voice/video token (gift_media)
    registry_lines: list = field(default_factory=list)   # [(_giftsense_registry value, qty, revenue)]


def _pairs(items) -> dict:
    out = {}
    for it in items or []:
        if isinstance(it, dict) and it.get("name") is not None:
            out[str(it["name"])] = "" if it.get("value") is None else str(it["value"])
    return out


def _uuid(value) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


def _money(value) -> float:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return 0.0


def _groups(raw: str | None) -> list[dict]:
    try:
        data = json.loads(raw) if raw else []
    except ValueError:
        return []
    out = []
    for g in data if isinstance(data, list) else []:
        if not isinstance(g, dict):
            continue
        out.append({
            "id": str(g.get("id", ""))[:40],
            "label": str(g.get("label", ""))[:MAX_LABEL],
            "note": str(g.get("note", ""))[:MAX_NOTE] or None,
            "wrap": str(g.get("wrap", ""))[:40] or None,
            "card": str(g.get("card", ""))[:40] or None,
            "message": str(g.get("message", ""))[:80] or None,
        })
        if len(out) >= MAX_GROUPS:
            break
    return out


def parse_gift_order(payload: dict) -> ParsedGiftOrder | None:
    attrs = _pairs(payload.get("note_attributes"))
    gift_lines, revenue, sid = 0, 0.0, _uuid(attrs.get("_giftsense_sid"))
    registry_lines = []
    for line in payload.get("line_items") or []:
        props = _pairs(line.get("properties"))
        sid = sid or _uuid(props.get("_giftsense_sid"))
        qty = int(line.get("quantity") or 1)
        if "_giftsense_gift" in props or "Gift for" in props or "_giftsense_registry" in props:
            gift_lines += 1
            revenue += _money(line.get("price")) * qty
        if props.get("_giftsense_registry"):
            registry_lines.append((props["_giftsense_registry"][:80], qty, _money(line.get("price")) * qty))
    groups = _groups(attrs.get("_giftsense_gifts"))
    note = (attrs.get("Gift note") or "").strip()[:MAX_NOTE] or None
    if not (gift_lines or sid or note or groups or attrs.get("_giftsense_message")):
        return None
    # Gift groups mean "I'll give it to them"; otherwise one gift shipped
    # straight to the recipient. (_giftsense_mode: carts from before 2026-10-01.)
    mode = attrs.get("_giftsense_mode") or ("self" if groups else "direct")
    return ParsedGiftOrder(
        order_id=str(payload.get("id")),
        order_gid=str(payload.get("admin_graphql_api_id") or f"gid://shopify/Order/{payload.get('id')}"),
        order_name=str(payload.get("name") or "")[:40],
        sid=sid,
        delivery_mode=mode if mode in ("direct", "self") else None,
        gift_lines=gift_lines,
        gift_revenue=round(revenue, 2),
        order_total=_money(payload.get("total_price")),
        currency=str(payload.get("currency") or "")[:3],
        note=note,
        groups=groups,
        arrive_by=(attrs.get("Arrive by") or None),
        gift_receipt=(attrs.get("Gift receipt") or "").lower() in ("yes", "true", "1"),
        wrap=(attrs.get("Gift wrap") or "").strip()[:40] or None,
        card=(attrs.get("Greeting card") or "").strip()[:40] or None,
        message=(attrs.get("_giftsense_message") or "").strip()[:80] or None,
        registry_lines=registry_lines,
    )


def _normalize(text: str) -> str:
    return " ".join(str(text).lower().split())


def message_tokens(p: ParsedGiftOrder) -> list[str]:
    return [t for t in [p.message, *(g.get("message") for g in p.groups)] if t]


def note_source(final: str | None, draft: str | None) -> str | None:
    """How the shopper's final note relates to our last draft (spec metric)."""
    if not final:
        return None
    if not draft:
        return "manual"
    a, b = _normalize(final), _normalize(draft)
    distance = 1 - difflib.SequenceMatcher(None, a, b).ratio()
    if distance <= ACCEPTED:
        return "ai_accepted"
    return "ai_edited" if distance < EDITED_BELOW else "manual"


def metafield_value(p: ParsedGiftOrder) -> dict:
    return {"version": 1, "mode": p.delivery_mode, "note": p.note, "groups": p.groups,
            "arrive_by": p.arrive_by, "gift_receipt": p.gift_receipt, "wrap": p.wrap, "card": p.card,
            "message": p.message}


# ── Shopify writes (run in the worker) ───────────────────────────────────────

DEFINITION_MUTATION = """
mutation($definition: MetafieldDefinitionInput!) {
  metafieldDefinitionCreate(definition: $definition) { userErrors { code message } }
}
"""
TAG_MUTATION = """
mutation($id: ID!, $tags: [String!]!) { tagsAdd(id: $id, tags: $tags) { userErrors { message } } }
"""
METAFIELD_MUTATION = """
mutation($metafields: [MetafieldsSetInput!]!) {
  metafieldsSet(metafields: $metafields) { userErrors { field message } }
}
"""


async def ensure_definition(shop_domain: str, token: str, gql=None) -> bool:
    """Create the order metafield definition (admin-readable). "TAKEN" = exists."""
    gql = gql or shopify_graphql_post
    resp = await gql(shop_domain, token, DEFINITION_MUTATION, {"definition": {
        "name": "GiftSense gifts", "namespace": METAFIELD_NAMESPACE, "key": METAFIELD_KEY, "type": "json",
        "ownerType": "ORDER", "description": "Gift groups, notes and options from GiftSense.",
        "access": {"admin": "MERCHANT_READ"},
    }})
    if resp.status_code != 200:
        return False
    errors = ((resp.json().get("data") or {}).get("metafieldDefinitionCreate") or {}).get("userErrors") or []
    return all(e.get("code") == "TAKEN" for e in errors)


async def annotate_order(shop_domain: str, token: str, order_gid: str, value: dict, gql=None) -> bool:
    """Tag the order and write the gifts metafield. True if both succeeded."""
    gql = gql or shopify_graphql_post
    ok = True
    r1 = await gql(shop_domain, token, TAG_MUTATION, {"id": order_gid, "tags": [ORDER_TAG]})
    if r1.status_code != 200 or ((r1.json().get("data") or {}).get("tagsAdd") or {}).get("userErrors"):
        ok = False
    fields = [{"ownerId": order_gid, "namespace": METAFIELD_NAMESPACE, "key": METAFIELD_KEY, "type": "json",
               "value": json.dumps(value)}]
    if value.get("messages"):
        # Plain (not app-reserved) namespace, so the merchant's packing-slip
        # template can read it: {{ order.metafields.giftsense.messages.value }}.
        fields.append({"ownerId": order_gid, "namespace": PACKING_SLIP_NAMESPACE, "key": "messages", "type": "json",
                       "value": json.dumps(value["messages"])})
    r2 = await gql(shop_domain, token, METAFIELD_MUTATION, {"metafields": fields})
    if r2.status_code != 200 or ((r2.json().get("data") or {}).get("metafieldsSet") or {}).get("userErrors"):
        ok = False
    if not ok:
        logger.warning("gift_order_annotate_failed", shop=shop_domain, order=order_gid)
    return ok
