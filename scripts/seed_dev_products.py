#!/usr/bin/env python3
"""Seed the DEV store with the Commerce demo apparel catalog (dev only).

Products: scripts/dev_catalog/apparel.json (snapshot of Prudix Commerce's
scripts/seed_demo_store.py specs). Images: a local folder of files named
"<n>. <Title>.jpg" (one per product).

Uses GiftSense's own offline token for the shop (write_products +
write_publications) and the GraphQL Admin API only:
  stagedUploadsCreate → upload the photo → productSet (ACTIVE, one variant per
  size/colour, inventory not tracked: no write_inventory scope) → publish to
  the Online Store. Products whose title already exists are skipped, so it is
  safe to re-run.

Usage:
  .venv/bin/python scripts/seed_dev_products.py --images ~/Downloads/Untitled --dry-run
  .venv/bin/python scripts/seed_dev_products.py --images ~/Downloads/Untitled
"""
import argparse
import asyncio
import json
import logging
import mimetypes
import sys
from pathlib import Path

import httpx
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.wrap import PUBLICATIONS, PUBLISH  # noqa: E402
from core.db.models import Shop  # noqa: E402
from core.db.session import AsyncSessionLocal  # noqa: E402
from core.shopify_auth import get_valid_access_token  # noqa: E402
from core.shopify_graphql import shopify_graphql_post  # noqa: E402

DEV_SHOP = "prudix-commerce-dev.myshopify.com"
CATALOG = Path(__file__).parent / "dev_catalog" / "apparel.json"

EXISTING = """query($q: String!) { products(first: 1, query: $q) { nodes { id title } } }"""
STAGED = """
mutation($input: [StagedUploadInput!]!) {
  stagedUploadsCreate(input: $input) {
    stagedTargets { url resourceUrl parameters { name value } }
    userErrors { field message }
  }
}
"""
PRODUCT_SET = """
mutation($input: ProductSetInput!) {
  productSet(synchronous: true, input: $input) {
    product { id title }
    userErrors { field message }
  }
}
"""


def data(resp, key):
    body = resp.json()
    if body.get("errors"):
        raise RuntimeError(body["errors"])
    return (body.get("data") or {}).get(key) or {}


def image_for(images: Path, n: int) -> Path | None:
    matches = sorted(images.glob(f"{n}. *"))
    return matches[0] if matches else None


async def upload_image(gql, shop, token, path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    r = await gql(shop, token, STAGED, {"input": [{
        "resource": "IMAGE", "filename": path.name, "mimeType": mime, "httpMethod": "POST",
        "fileSize": str(path.stat().st_size),
    }]})
    payload = data(r, "stagedUploadsCreate")
    if payload.get("userErrors"):
        raise RuntimeError(payload["userErrors"])
    target = payload["stagedTargets"][0]
    form = {p["name"]: p["value"] for p in target["parameters"]}
    async with httpx.AsyncClient(timeout=120) as client:
        up = await client.post(target["url"], data=form, files={"file": (path.name, path.read_bytes(), mime)})
    if up.status_code >= 300:
        raise RuntimeError(f"upload failed: HTTP {up.status_code} {up.text[:200]}")
    return target["resourceUrl"]


def product_input(spec: dict, image_url: str | None) -> dict:
    option = spec["options"][0]["name"]
    inp = {
        "title": spec["title"],
        "descriptionHtml": spec["description_html"],
        "vendor": spec["vendor"],
        "productType": spec["product_type"],
        "tags": spec["tags"],
        "status": "ACTIVE",
        "productOptions": [{"name": option, "values": [{"name": v["option1"]} for v in spec["variants"]]}],
        "variants": [{
            "optionValues": [{"optionName": option, "name": v["option1"]}],
            "price": v["price"],
            **({"compareAtPrice": v["compare_at_price"]} if v.get("compare_at_price") else {}),
            "inventoryPolicy": "DENY",
            "inventoryItem": {"sku": v.get("sku"), "tracked": False},
        } for v in spec["variants"]],
    }
    if image_url:
        inp["files"] = [{"originalSource": image_url, "alt": spec["image_alt"], "contentType": "IMAGE"}]
    return inp


async def main(images: Path, dry_run: bool) -> None:
    logging.disable(logging.INFO)
    specs = json.loads(CATALOG.read_text())
    missing = [s["title"] for s in specs if not image_for(images, s["n"])]
    if missing:
        print(f"No image for: {', '.join(missing)} (they'll be created without one)")

    async with AsyncSessionLocal() as db:
        shop = (await db.execute(select(Shop).where(Shop.shop_domain == DEV_SHOP))).scalar_one()
        token = await get_valid_access_token(shop, db)
        await db.commit()
    gql = shopify_graphql_post

    pubs = data(await gql(DEV_SHOP, token, PUBLICATIONS), "publications").get("nodes") or []
    online = next((p["id"] for p in pubs if p["name"] == "Online Store"), None)
    if not online:
        sys.exit("Online Store sales channel not found")

    created = skipped = failed = 0
    for spec in specs:
        title = spec["title"]
        found = data(await gql(DEV_SHOP, token, EXISTING, {"q": f'title:"{title}"'}), "products").get("nodes") or []
        if any(n["title"] == title for n in found):
            print(f"  = {title} (already in the store)")
            skipped += 1
            continue
        img = image_for(images, spec["n"])
        if dry_run:
            print(f"  + {title} — {len(spec['variants'])} variants, image: {img.name if img else 'none'}")
            continue
        try:
            image_url = await upload_image(gql, DEV_SHOP, token, img) if img else None
            result = data(await gql(DEV_SHOP, token, PRODUCT_SET, {"input": product_input(spec, image_url)}),
                          "productSet")
            if result.get("userErrors") or not result.get("product"):
                raise RuntimeError(result.get("userErrors"))
            pid = result["product"]["id"]
            pub = data(await gql(DEV_SHOP, token, PUBLISH, {"id": pid, "input": [{"publicationId": online}]}),
                       "publishablePublish")
            if pub.get("userErrors"):
                raise RuntimeError(pub["userErrors"])
            print(f"  + {title} ({pid.rsplit('/', 1)[-1]})")
            created += 1
        except Exception as e:  # noqa: BLE001 — report and carry on
            print(f"  ! {title}: {e}")
            failed += 1
    print(f"\n{'Would create' if dry_run else 'Created'} {len(specs) - skipped if dry_run else created}, "
          f"skipped {skipped}, failed {failed}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--images", type=Path, required=True)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    asyncio.run(main(a.images.expanduser(), a.dry_run))
