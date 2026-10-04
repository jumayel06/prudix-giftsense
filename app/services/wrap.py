"""Gift wrap (docs/TECHNICAL_PLAN.md §6.4).

The merchant sets up to 3 styles (name + price + type + optional photo).
A style's type (wrap, bag, box, or card = a greeting card offered next to
the packaging) only changes how the gift panel shows it;
products tagged `no-gift-wrap` never offer it (NO_WRAP_TAG). The app keeps ONE hidden
"Gift wrap" product in the store: status UNLISTED (addable to carts by id,
hidden from search, collections and channels), one variant per style,
inventory untracked, no shipping of its own, published to the Online Store
(write_publications) so carts accept it. Settings live in
shops.gift_settings["wrap"]; `sync_wrap_product` runs in the worker.

Shoppers pick wrap in the gift panel: never pre-selected, price always shown
(App Store rule 1.1.9). A wrap cart line carries `_giftsense_wrap_for`.

Photos: the dashboard uploads straight to a Shopify staged URL
(`create_image_upload`), saves the style with that `image_source`, and the
sync attaches it to the style's variant (productSet files/variant file). The
CDN url (`image_url`) and media id (`file_id`, reused on later syncs) are read
back once Shopify has processed the image. No extra scope: write_products.
"""
import asyncio
from decimal import Decimal
from typing import Literal, Optional

import structlog
from pydantic import BaseModel, Field, field_validator

from core.shopify_graphql import numeric_id_from_gid, shopify_graphql_post

logger = structlog.get_logger()

MAX_PACKAGING = 3           # wrap / bag / box styles
MAX_CARDS = 2               # greeting cards
MAX_STYLES = MAX_PACKAGING + MAX_CARDS
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024
STAGED_HOST = "https://shopify-staged-uploads.storage.googleapis.com/"
IMAGE_FIELDS = ("image_source", "file_id", "image_url")
KINDS = ("wrap", "bag", "box", "card")
NO_WRAP_TAG = "no-gift-wrap"


def wrappable(tags) -> bool:
    """False for products the merchant tagged `no-gift-wrap` (oversized, digital…)."""
    return NO_WRAP_TAG not in {str(t).strip().lower() for t in tags or []}
WRAP_TAG = "giftsense-wrap"
DEFAULTS = {"enabled": False, "styles": [], "product_id": None, "ready": False, "error": None}


class WrapStyle(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    price: Decimal = Field(ge=0, le=1000)
    kind: Literal["wrap", "bag", "box", "card"] = "wrap"
    # None = no photo; the style's current image_url/image_source = keep it;
    # a fresh staged-upload resourceUrl = new photo (checked in update).
    image: Optional[str] = Field(default=None, max_length=2000)

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
            cards = sum(1 for s in styles if s.kind == "card")
            if cards > MAX_CARDS or len(styles) - cards > MAX_PACKAGING:
                raise ValueError(f"up to {MAX_PACKAGING} wrap styles and {MAX_CARDS} greeting cards")
        return styles


def style_key(styles: list[dict]) -> list:
    """What the Shopify product is built from: names, prices, photos, in order."""
    return [(s.get("name"), float(s.get("price") or 0), s.get("image_source") or s.get("file_id"))
            for s in styles or []]


def _image_fields(new_image: Optional[str], old: dict) -> dict:
    if not new_image:
        return {}
    if new_image in (old.get("image_url"), old.get("image_source")):
        return {k: old[k] for k in IMAGE_FIELDS if old.get(k)}
    if not new_image.startswith(STAGED_HOST):
        raise ValueError("wrap photo must be uploaded through GiftSense")
    return {"image_source": new_image}


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
        styles = [{"name": s.name, "price": float(s.price), "kind": s.kind,
                   **({"variant_id": old[s.name.lower()]["variant_id"]}
                      if old.get(s.name.lower(), {}).get("variant_id") else {}),
                   **_image_fields(s.image, old.get(s.name.lower(), {}))}
                  for s in update.styles]
        if style_key(styles) != style_key(current["styles"]):
            current["styles"], current["ready"], current["error"] = styles, False, None
        else:   # a type change is a label only: no product re-sync
            current["styles"] = [{**old_s, "kind": new_s["kind"]} for old_s, new_s in zip(current["styles"], styles)]
    shop.gift_settings = {**(shop.gift_settings or {}), "wrap": current}


def storefront_styles(shop) -> list[dict]:
    """What the widget offers: only when enabled and the product is synced."""
    s = wrap_settings(shop)
    if not (s["enabled"] and s["ready"] and s["product_id"]):
        return []
    return [{"variant_id": int(numeric_id_from_gid(x["variant_id"])), "name": x["name"], "price": x["price"],
             "kind": x.get("kind") or "wrap", **({"image": x["image_url"]} if x.get("image_url") else {})}
            for x in s["styles"] if x.get("variant_id")]


PRODUCT_SET = """
mutation($input: ProductSetInput!, $identifier: ProductSetIdentifiers) {
  productSet(synchronous: true, input: $input, identifier: $identifier) {
    product { id variants(first: 10) { nodes { id title } } }
    userErrors { field message }
  }
}
"""
VARIANT_MEDIA = """
query($id: ID!) {
  product(id: $id) {
    variants(first: 10) { nodes { id media(first: 1) { nodes { id ... on MediaImage { image { url } } } } } }
  }
}
"""
STAGED_UPLOAD = """
mutation($input: [StagedUploadInput!]!) {
  stagedUploadsCreate(input: $input) {
    stagedTargets { url resourceUrl parameters { name value } }
    userErrors { field message }
  }
}
"""
IMAGE_POLLS, IMAGE_POLL_SECS = 5, 2.0
PUBLICATIONS = "{ publications(first: 25) { nodes { id catalog { title } } } }"   # Publication.name is deprecated
PUBLISH = """
mutation($id: ID!, $input: [PublicationInput!]!) {
  publishablePublish(id: $id, input: $input) { userErrors { field message } }
}
"""


def _file_input(style: dict) -> dict | None:
    if style.get("image_source") and not style.get("file_id"):
        return {"originalSource": style["image_source"], "contentType": "IMAGE", "alt": f"{style['name']} gift wrap"}
    if style.get("file_id"):
        return {"id": style["file_id"]}
    return None


def _product_input(styles: list[dict]) -> dict:
    files = [_file_input(s) for s in styles]
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
            **({"file": f} if f else {}),
        } for s, f in zip(styles, files)],
        # Always sent: a removed photo leaves the product's media too.
        "files": [f for f in files if f],
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
        if any(s.get("image_source") or s.get("file_id") for s in styles):
            out["styles"] = await _read_images(gql, shop_domain, token, product["id"], out["styles"])

        pubs = await gql(shop_domain, token, PUBLICATIONS)
        nodes = ((pubs.json().get("data") or {}).get("publications") or {}).get("nodes") or []
        online = next((p["id"] for p in nodes if (p.get("catalog") or {}).get("title") == "Online Store"), None)
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


async def _read_images(gql, shop_domain: str, token: str, product_id: str, styles: list[dict]) -> list[dict]:
    """Copy each variant's media id + CDN url onto its style. Shopify
    processes new images asynchronously, so poll briefly; a style still
    processing keeps its image_source and gets its url on the next sync."""
    styles = [{k: v for k, v in s.items() if k not in ("file_id", "image_url")} if s.get("image_source") else s
              for s in styles]
    for attempt in range(IMAGE_POLLS):
        r = await gql(shop_domain, token, VARIANT_MEDIA, {"id": product_id})
        nodes = ((((r.json().get("data") or {}).get("product") or {}).get("variants") or {}).get("nodes") or [])
        media = {}
        for v in nodes:
            m = ((v.get("media") or {}).get("nodes") or [None])[0]
            if m:
                media[v["id"]] = (m.get("id"), ((m.get("image") or {}).get("url")))
        for s in styles:
            if (s.get("image_source") or s.get("file_id")) and s.get("variant_id") in media:
                file_id, url = media[s["variant_id"]]
                s["file_id"] = file_id
                if url:
                    s["image_url"] = url
        if all(s.get("image_url") for s in styles if s.get("image_source") or s.get("file_id")):
            break
        if attempt < IMAGE_POLLS - 1:
            await asyncio.sleep(IMAGE_POLL_SECS)
    return styles


async def create_image_upload(shop_domain: str, token: str, filename: str, mime_type: str, size: int,
                              gql=None) -> dict:
    """Staged upload target for one wrap photo; the dashboard POSTs the file
    to `url` with `parameters`, then saves the style with `resource_url`."""
    if mime_type not in IMAGE_TYPES:
        raise ValueError("Use a JPG, PNG, WebP or GIF image.")
    if not 0 < size <= MAX_IMAGE_BYTES:
        raise ValueError("Images can be up to 5 MB.")
    gql = gql or shopify_graphql_post
    r = await gql(shop_domain, token, STAGED_UPLOAD, {"input": [{
        "filename": filename[:100] or "wrap.jpg", "mimeType": mime_type, "resource": "IMAGE",
        "httpMethod": "POST", "fileSize": str(size),
    }]})
    payload = ((r.json().get("data") or {}).get("stagedUploadsCreate") or {}) if r.status_code == 200 else {}
    targets = payload.get("stagedTargets") or []
    if payload.get("userErrors") or not targets:
        raise RuntimeError("; ".join(e.get("message", "") for e in payload.get("userErrors") or [])
                           or f"HTTP {r.status_code}")
    t = targets[0]
    return {"url": t["url"], "resource_url": t["resourceUrl"],
            "parameters": [{"name": p["name"], "value": p["value"]} for p in t["parameters"]]}
