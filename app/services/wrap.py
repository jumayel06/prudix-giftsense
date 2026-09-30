"""Gift wrap (docs/TECHNICAL_PLAN.md §6.4).

The merchant sets up to 3 styles (name + price). The app keeps ONE hidden
"Gift wrap" product in the store: status UNLISTED (addable to carts by id,
hidden from search, collections and channels), one variant per style,
inventory untracked, no shipping of its own, published to the Online Store
(write_publications) so carts accept it. Settings live in
shops.gift_settings["wrap"]; `sync_wrap_product` runs in the worker.

Shoppers pick wrap in the gift panel: never pre-selected, price always shown
(App Store rule 1.1.9). A wrap cart line carries `_giftsense_wrap_for`.
"""
from decimal import Decimal
from typing import Optional

import structlog
from pydantic import BaseModel, Field, field_validator

from core.shopify_graphql import numeric_id_from_gid, shopify_graphql_post

logger = structlog.get_logger()

MAX_STYLES = 3
WRAP_TAG = "giftsense-wrap"
DEFAULTS = {"enabled": False, "styles": [], "product_id": None, "ready": False, "error": None}


class WrapStyle(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    price: Decimal = Field(ge=0, le=1000)

    @field_validator("name", mode="before")
    @classmethod
    def _trim(cls, v):
        return " ".join(str(v).split())


class WrapUpdate(BaseModel):
    enabled: Optional[bool] = None
    styles: Optional[list[WrapStyle]] = Field(default=None, max_length=MAX_STYLES)

    @field_validator("styles")
    @classmethod
    def _unique(cls, styles):
        if styles is not None:
            names = [s.name.lower() for s in styles]
            if len(names) != len(set(names)):
                raise ValueError("wrap style names must be different")
        return styles


def style_key(styles: list[dict]) -> list:
    """What the Shopify product is built from: names + prices, in order."""
    return [(s.get("name"), float(s.get("price") or 0)) for s in styles or []]


def wrap_settings(shop) -> dict:
    stored = (getattr(shop, "gift_settings", None) or {}).get("wrap") or {}
    return {**DEFAULTS, **{k: v for k, v in stored.items() if k in DEFAULTS}}


def update_wrap_settings(shop, update: WrapUpdate) -> None:
    """Apply the merchant's edits; any style change needs a product re-sync."""
    current = wrap_settings(shop)
    if update.enabled is not None:
        current["enabled"] = update.enabled
    if update.styles is not None:
        old = {s["name"].lower(): s for s in current["styles"]}
        styles = [{"name": s.name, "price": float(s.price),
                   **({"variant_id": old[s.name.lower()]["variant_id"]}
                      if old.get(s.name.lower(), {}).get("variant_id") else {})}
                  for s in update.styles]
        if style_key(styles) != style_key(current["styles"]):
            current["styles"], current["ready"], current["error"] = styles, False, None
    shop.gift_settings = {**(shop.gift_settings or {}), "wrap": current}


def storefront_styles(shop) -> list[dict]:
    """What the widget offers: only when enabled and the product is synced."""
    s = wrap_settings(shop)
    if not (s["enabled"] and s["ready"] and s["product_id"]):
        return []
    return [{"variant_id": int(numeric_id_from_gid(x["variant_id"])), "name": x["name"], "price": x["price"]}
            for x in s["styles"] if x.get("variant_id")]


PRODUCT_SET = """
mutation($input: ProductSetInput!, $identifier: ProductSetIdentifiers) {
  productSet(synchronous: true, input: $input, identifier: $identifier) {
    product { id variants(first: 10) { nodes { id title } } }
    userErrors { field message }
  }
}
"""
PUBLICATIONS = "{ publications(first: 25) { nodes { id name } } }"
PUBLISH = """
mutation($id: ID!, $input: [PublicationInput!]!) {
  publishablePublish(id: $id, input: $input) { userErrors { field message } }
}
"""


def _product_input(styles: list[dict]) -> dict:
    return {
        "title": "Gift wrap",
        "status": "UNLISTED",
        "productType": "Gift wrap",
        "tags": [WRAP_TAG],
        "descriptionHtml": "<p>Gift wrapping, added by GiftSense.</p>",
        "productOptions": [{"name": "Style", "values": [{"name": s["name"]} for s in styles]}],
        "variants": [{
            "optionValues": [{"optionName": "Style", "name": s["name"]}],
            "price": f"{Decimal(str(s['price'])):.2f}",
            "inventoryPolicy": "CONTINUE",
            "inventoryItem": {"tracked": False, "requiresShipping": False},
            "taxable": True,
        } for s in styles],
        "metafields": [{"namespace": "seo", "key": "hidden", "type": "number_integer", "value": "1"}],
    }


async def sync_wrap_product(shop_domain: str, token: str, settings: dict, gql=None) -> dict:
    """Create/update the hidden wrap product and publish it to the Online
    Store. Returns updated settings (variant ids, ready/error); never raises."""
    gql = gql or shopify_graphql_post
    out = {**DEFAULTS, **settings}
    styles = out["styles"]
    if not styles:
        return {**out, "ready": False, "error": None}
    try:
        variables = {"input": _product_input(styles)}
        if out.get("product_id"):
            variables["identifier"] = {"id": out["product_id"]}
        r = await gql(shop_domain, token, PRODUCT_SET, variables)
        payload = ((r.json().get("data") or {}).get("productSet") or {}) if r.status_code == 200 else {}
        errors = payload.get("userErrors") or []
        product = payload.get("product")
        if errors or not product:
            raise RuntimeError("; ".join(e.get("message", "") for e in errors) or f"HTTP {r.status_code}")
        by_title = {v["title"].lower(): v["id"] for v in product["variants"]["nodes"]}
        out["product_id"] = product["id"]
        out["styles"] = [{**s, "variant_id": by_title.get(s["name"].lower())} for s in styles]

        pubs = await gql(shop_domain, token, PUBLICATIONS)
        nodes = ((pubs.json().get("data") or {}).get("publications") or {}).get("nodes") or []
        online = next((p["id"] for p in nodes if p.get("name") == "Online Store"), None)
        if not online:
            raise RuntimeError("Online Store sales channel not found")
        p = await gql(shop_domain, token, PUBLISH, {"id": product["id"], "input": [{"publicationId": online}]})
        perr = ((p.json().get("data") or {}).get("publishablePublish") or {}).get("userErrors") or []
        if perr:
            raise RuntimeError("; ".join(e.get("message", "") for e in perr))
        return {**out, "ready": True, "error": None}
    except Exception as e:  # noqa: BLE001 — shown on the Settings page
        logger.warning("wrap_sync_failed", shop=shop_domain, error=str(e)[:200])
        return {**out, "ready": False, "error": str(e)[:200]}
