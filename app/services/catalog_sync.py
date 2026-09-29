"""Catalog sync: Shopify products → catalog_products (docs/TECHNICAL_PLAN.md §4.1).

    start_bulk_sync   → bulkOperationRunQuery over active products
    finish_bulk_sync  → (bulk_operations/finish webhook or the 5-min cron)
                        download JSONL, upsert, drop products no longer listed,
                        then analyze_pending
    sync_product      → products/create|update webhook: one product via GraphQL
    analyze_pending   → AI gift profile + embedding for new or changed products

Only title/description/type/vendor/tags feed `content_hash`, so price and stock
changes never trigger a paid re-read. The number of products analyzed is capped
by the plan (`max_products`) and during the trial by TRIAL_MAX_PRODUCTS.
Every Shopify call is GraphQL Admin API (App Store rule 2.2.4).
"""
import asyncio
import hashlib
import json
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

import httpx
import structlog
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.config import PLANS, TRIAL_MAX_PRODUCTS
from app.plan_guard import effective_cycle_start
from app.llm import calc_cost, chat
from app.services.gifting.catalog import CatalogProduct
from app.services.gifting.embeddings import Embedder
from app.ai_models import catalog_model_for
from app.services.gifting.enrichment import enrich_product, profile_version_for
from app.services.gifting.profile import GiftProfile, apply_overrides, embedding_text
from core.db.models import CatalogProductRow, CatalogSync, Shop, UsageLog
from core.shopify_graphql import numeric_id_from_gid, product_gid, shopify_graphql_post

logger = structlog.get_logger()

ACTIVE_STATUSES = ("active", "trial_active")
# catalog_syncs.status while a sync isn't finished. "queued": Sync now was
# clicked and the worker hasn't picked it up yet (shown as "Waiting to start").
IN_PROGRESS = ("queued", "running", "importing")
QUEUED_TIMEOUT = timedelta(minutes=30)
ANALYZE_CHUNK = 25          # products enriched + embedded per commit (progress bar granularity)
ENRICH_CONCURRENCY = 5
STALE_SYNC_AFTER = timedelta(hours=6)
# Unchanged products re-read per sync after a prompt/model change (nightly
# reconcile → a 5,000-product catalog moves over in a few nights).
UPGRADES_PER_RUN = 1000

PRODUCT_FIELDS = """
  id handle title descriptionHtml productType vendor tags status isGiftCard
  publishedAt totalInventory tracksInventory
  priceRangeV2 { minVariantPrice { amount } maxVariantPrice { amount } }
  featuredMedia { preview { image { url } } }
"""
# No nested connections, so every JSONL line is one product (no __parentId rows).
BULK_PRODUCTS_QUERY = '{ products(query: "status:active") { edges { node { %s } } } }' % PRODUCT_FIELDS

BULK_RUN_MUTATION = """
mutation($query: String!) {
  bulkOperationRunQuery(query: $query) {
    bulkOperation { id status }
    userErrors { field message }
  }
}
"""

BULK_STATUS_QUERY = """
query($id: ID!) {
  node(id: $id) { ... on BulkOperation { id status errorCode url objectCount } }
}
"""

PRODUCT_QUERY = "query($id: ID!) { product(id: $id) { %s } }" % PRODUCT_FIELDS

GraphQLFn = Callable[..., Awaitable[httpx.Response]]
FetchText = Callable[[str], Awaitable[str]]


# ── Pure helpers ──────────────────────────────────────────────────────────────

def parse_product(node: dict | None) -> dict | None:
    """Shopify Product node → catalog row fields, or None if the product can't
    be a gift here (draft/archived, not on the Online Store, gift card, $0).

    "On the Online Store" = `publishedAt` set. Not `onlineStoreUrl`: it is null
    on password-protected (not yet launched) stores even for published
    products, which silently emptied the dev store's catalog (2026-09-29)."""
    if not node or node.get("status") != "ACTIVE" or node.get("isGiftCard") or not node.get("publishedAt"):
        return None
    prices = node.get("priceRangeV2") or {}
    price_min = float(((prices.get("minVariantPrice") or {}).get("amount")) or 0)
    price_max = float(((prices.get("maxVariantPrice") or {}).get("amount")) or price_min)
    if price_max <= 0:
        return None
    image = ((node.get("featuredMedia") or {}).get("preview") or {}).get("image") or {}
    return {
        "product_id": numeric_id_from_gid(node["id"]),
        "handle": node.get("handle"),
        "title": node.get("title") or "",
        "description": node.get("descriptionHtml") or "",
        "product_type": node.get("productType") or "",
        "vendor": node.get("vendor") or "",
        "tags": list(node.get("tags") or []),
        "price_min": price_min,
        "price_max": price_max,
        "available": (not node.get("tracksInventory")) or (node.get("totalInventory") or 0) > 0,
        "image_url": image.get("url"),
        # Storefront-relative: the widget runs on the storefront, so this works
        # on myshopify, custom domains and password-protected stores alike.
        "url": f"/products/{node['handle']}" if node.get("handle") else None,
    }


def content_hash(fields: dict) -> str:
    """Hash of what the product *is*. Price/stock/image changes don't count."""
    key = {k: fields.get(k) for k in ("title", "description", "product_type", "vendor")}
    key["tags"] = sorted(fields.get("tags") or [])
    return hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()[:32]


def product_limit(shop: Shop) -> int:
    """How many products this shop may have analyzed right now."""
    if shop.plan_status not in ACTIVE_STATUSES or shop.plan_tier not in PLANS:
        return 0
    limit = PLANS[shop.plan_tier]["max_products"]
    if shop.plan_status == "trial_active":
        limit = min(limit, TRIAL_MAX_PRODUCTS)
    return limit


def row_to_product(row: CatalogProductRow) -> CatalogProduct:
    return CatalogProduct(
        product_id=row.product_id, title=row.title, description=row.description,
        product_type=row.product_type, vendor=row.vendor, tags=list(row.tags or []),
        price_min=float(row.price_min), price_max=float(row.price_max), available=row.available,
        image_url=row.image_url, url=row.url, excluded=row.excluded,
    )


# ── DB writes ────────────────────────────────────────────────────────────────

async def upsert_products(db: AsyncSession, shop: Shop, products: list[dict]) -> dict:
    """Insert new products (up to the plan limit) and refresh existing ones.
    Returns counts {"added", "updated", "over_limit"}. Caller commits."""
    existing = {
        r.product_id: r for r in (await db.execute(
            select(CatalogProductRow).options(defer(CatalogProductRow.embedding))
            .where(CatalogProductRow.shop_id == shop.id)
        )).scalars()
    }
    limit = product_limit(shop)
    counts = {"added": 0, "updated": 0, "over_limit": 0}
    now = datetime.now(timezone.utc)
    for fields in products:
        row = existing.get(fields["product_id"])
        if row is None:
            if len(existing) >= limit:
                counts["over_limit"] += 1
                continue
            row = CatalogProductRow(id=uuid.uuid4(), shop_id=shop.id, **fields,
                                    content_hash=content_hash(fields), synced_at=now)
            db.add(row)
            existing[row.product_id] = row
            counts["added"] += 1
        else:
            for k, v in fields.items():
                setattr(row, k, v)
            row.content_hash = content_hash(fields)
            row.synced_at = now
            counts["updated"] += 1
    return counts


async def delete_missing(db: AsyncSession, shop_id: uuid.UUID, keep_ids: set[str]) -> int:
    """Drop products that are no longer sellable gifts (deleted, drafted, …)."""
    stale = [pid for pid in (await db.execute(
        select(CatalogProductRow.product_id).where(CatalogProductRow.shop_id == shop_id)
    )).scalars() if pid not in keep_ids]
    if stale:
        await db.execute(delete(CatalogProductRow).where(
            CatalogProductRow.shop_id == shop_id, CatalogProductRow.product_id.in_(stale)))
    return len(stale)


def _content_pending():
    """New or edited products (or a missing embedding): analyzed first, all of them."""
    return or_(
        CatalogProductRow.profile_hash.is_(None),
        CatalogProductRow.profile_hash != CatalogProductRow.content_hash,
        CatalogProductRow.embedding.is_(None),
    )


def _upgrade_pending(version: str):
    """Unchanged products whose profile came from an older prompt or model.
    They keep serving searches with that profile until re-read."""
    return or_(CatalogProductRow.profile_version.is_(None), CatalogProductRow.profile_version != version)


async def count_pending(db: AsyncSession, shop_id: uuid.UUID) -> int:
    """Products not usable by the gift finder yet (new/edited, not analyzed)."""
    return (await db.execute(
        select(func.count()).select_from(CatalogProductRow)
        .where(CatalogProductRow.shop_id == shop_id, _content_pending())
    )).scalar_one()


def rereads_limit(shop: Shop) -> int:
    return PLANS.get(shop.plan_tier, PLANS["starter"])["product_rereads_per_month"]


def rereads_remaining(shop: Shop) -> int:
    """Paid re-reads of edited products left this billing cycle (resets the
    counter in place when a new cycle has started; caller commits)."""
    cycle = effective_cycle_start(shop)
    stored = shop.catalog_rereads_cycle_start
    if stored is not None and stored.tzinfo is None:
        stored = stored.replace(tzinfo=timezone.utc)
    if cycle is not None and stored != cycle:
        shop.catalog_rereads_used = 0
        shop.catalog_rereads_cycle_start = cycle
    return max(0, rereads_limit(shop) - (shop.catalog_rereads_used or 0))


def _is_reread():
    """Edited since its last AI read (had a profile, content changed)."""
    return (CatalogProductRow.profile_hash.is_not(None)) & (CatalogProductRow.profile_hash != CatalogProductRow.content_hash)


async def count_held(db: AsyncSession, shop: Shop) -> int:
    """Edited products waiting for next cycle's re-reads (they keep serving
    their previous profile meanwhile)."""
    if rereads_remaining(shop) > 0:
        return 0
    return (await db.execute(
        select(func.count()).select_from(CatalogProductRow)
        .where(CatalogProductRow.shop_id == shop.id, _is_reread(), CatalogProductRow.embedding.is_not(None))
    )).scalar_one()


async def count_upgrades(db: AsyncSession, shop: Shop) -> int:
    version = profile_version_for(catalog_model_for(shop))
    return (await db.execute(
        select(func.count()).select_from(CatalogProductRow)
        .where(CatalogProductRow.shop_id == shop.id, ~_content_pending(), _upgrade_pending(version))
    )).scalar_one()


async def analyze_pending(
    db: AsyncSession,
    shop: Shop,
    embedder: Embedder,
    chat_fn=chat,
    sync: CatalogSync | None = None,
    upgrade_limit: int = UPGRADES_PER_RUN,
) -> int:
    """Enrich + embed new or edited products, then re-read up to
    `upgrade_limit` products whose profile predates the current prompt or
    catalog_analysis model, so a model switch spreads over several syncs
    while old profiles keep serving. Commits per chunk so the dashboard's
    progress bar moves. Returns how many were analyzed. Enrichment never
    raises (fallback profile); an embedding failure leaves the chunk pending.

    Edited products (re-reads) count against the plan's monthly
    product_rereads_per_month; past it they keep their previous profile and
    stay searchable (price/stock still update free). New products and our own
    prompt/model upgrades never count."""
    model = catalog_model_for(shop)
    version = profile_version_for(model)
    done = upgraded = 0
    budget = rereads_remaining(shop)
    sem = asyncio.Semaphore(ENRICH_CONCURRENCY)
    failed_ids: set[str] = set()   # embedding failed, or held by the re-read cap

    async def _enrich(row: CatalogProductRow):
        # A current profile for unchanged content only needs its embedding redone.
        if row.gift_profile and row.profile_hash == row.content_hash and row.profile_version == version:
            return None
        async with sem:
            return await enrich_product(row_to_product(row), model=model, chat_fn=chat_fn)

    while True:
        base = select(CatalogProductRow).where(CatalogProductRow.shop_id == shop.id)
        if failed_ids:
            base = base.where(CatalogProductRow.product_id.not_in(failed_ids))
        rows = list((await db.execute(
            base.where(_content_pending()).order_by(CatalogProductRow.product_id).limit(ANALYZE_CHUNK)
        )).scalars())
        is_upgrade = False
        if not rows and upgraded < upgrade_limit:
            rows = list((await db.execute(
                base.where(_upgrade_pending(version)).order_by(CatalogProductRow.product_id)
                .limit(min(ANALYZE_CHUNK, upgrade_limit - upgraded))
            )).scalars())
            is_upgrade = True
        if rows and not is_upgrade:
            allowed = []
            for r in rows:
                if r.profile_hash is not None and r.profile_hash != r.content_hash:
                    if budget <= 0:
                        failed_ids.add(r.product_id)   # held until next cycle
                        continue
                    budget -= 1
                    shop.catalog_rereads_used = (shop.catalog_rereads_used or 0) + 1
                allowed.append(r)
            if not allowed:
                continue
            rows = allowed
        if not rows:
            break

        results = await asyncio.gather(*(_enrich(r) for r in rows))
        tokens_in = tokens_out = 0
        for row, res in zip(rows, results):
            if res is None:
                continue
            row.gift_profile = asdict(res.profile)
            row.profile_hash = row.content_hash
            row.profile_version = version
            row.profile_fallback = res.used_fallback
            row.enriched_at = datetime.now(timezone.utc)
            tokens_in += res.input_tokens
            tokens_out += res.output_tokens

        texts = [embedding_text(row_to_product(r), apply_overrides(GiftProfile(**r.gift_profile),
                                                                   r.merchant_overrides)) for r in rows]
        try:
            vectors = await embedder.embed(texts)
        except Exception as e:  # noqa: BLE001 — retried on the next sync
            logger.warning("catalog_embed_failed", shop=shop.shop_domain, error=str(e))
            vectors = None
            failed_ids.update(r.product_id for r in rows)
        if vectors:
            for row, vec in zip(rows, vectors):
                row.embedding = vec
                row.embedding_model = getattr(embedder, "model", type(embedder).__name__)

        if tokens_in or tokens_out:
            # Catalog analysis is our cost, not the merchant's: 0 generations.
            db.add(UsageLog(id=uuid.uuid4(), shop_id=shop.id, action_type="catalog_analysis",
                            generations_consumed=0, tokens_input=tokens_in, tokens_output=tokens_out,
                            model_used=model, cost_usd=calc_cost(model, tokens_in, tokens_out),
                            prompt_version=version))
        analyzed = len(rows) if vectors else 0
        done += analyzed
        if is_upgrade:
            upgraded += len(rows)
        if sync is not None:
            sync.enriched += analyzed
            sync.failed += len(rows) - analyzed
        await db.commit()
    return done


# ── Bulk sync ────────────────────────────────────────────────────────────────

async def _running_sync(db: AsyncSession, shop_id: uuid.UUID) -> CatalogSync | None:
    return (await db.execute(
        select(CatalogSync).where(CatalogSync.shop_id == shop_id, CatalogSync.status.in_(IN_PROGRESS))
        .order_by(CatalogSync.started_at.desc()).limit(1)
    )).scalar_one_or_none()


async def start_bulk_sync(
    db: AsyncSession, shop: Shop, token: str, kind: str, gql: GraphQLFn = shopify_graphql_post,
) -> CatalogSync:
    """Kick off a bulk product export. Reuses a sync that's already running;
    a "queued" one (from Sync now) is started on its own row."""
    running = await _running_sync(db, shop.id)
    if running is not None and running.status != "queued":
        return running
    if running is not None:
        sync = running
        sync.status = "running"
        sync.started_at = datetime.now(timezone.utc)
    else:
        sync = CatalogSync(id=uuid.uuid4(), shop_id=shop.id, kind=kind, status="running")
        db.add(sync)
    try:
        resp = await gql(shop.shop_domain, token, BULK_RUN_MUTATION, {"query": BULK_PRODUCTS_QUERY})
        body = resp.json() if resp.status_code == 200 else {}
        data = (body.get("data") or {}).get("bulkOperationRunQuery") or {}
        errors = data.get("userErrors") or []
        op = data.get("bulkOperation") or {}
        if errors or not op.get("id"):
            raise RuntimeError("; ".join(e.get("message", "") for e in errors) or f"HTTP {resp.status_code}")
        sync.bulk_operation_id = op["id"]
    except Exception as e:  # noqa: BLE001 — recorded on the sync row; the cron retries
        sync.status = "failed"
        sync.error = str(e)[:500]
        sync.finished_at = datetime.now(timezone.utc)
        logger.warning("catalog_bulk_start_failed", shop=shop.shop_domain, error=str(e))
    await db.commit()
    return sync


async def _fetch_text(url: str) -> str:
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.text


def _fail(sync: CatalogSync, error: str) -> None:
    sync.status = "failed"
    sync.error = error[:500]
    sync.finished_at = datetime.now(timezone.utc)


async def finish_bulk_sync(
    db: AsyncSession,
    shop: Shop,
    token: str,
    bulk_operation_id: str,
    embedder: Embedder,
    chat_fn=chat,
    gql: GraphQLFn = shopify_graphql_post,
    fetch_text: FetchText = _fetch_text,
) -> CatalogSync | None:
    """Import a finished bulk export. Safe to call early (still running → no-op)
    or twice (only the caller that moves it running → importing does the work)."""
    sync = (await db.execute(
        select(CatalogSync).where(CatalogSync.shop_id == shop.id,
                                  CatalogSync.bulk_operation_id == bulk_operation_id)
    )).scalar_one_or_none()
    if sync is None or sync.status != "running":
        return sync

    resp = await gql(shop.shop_domain, token, BULK_STATUS_QUERY, {"id": bulk_operation_id})
    op = ((resp.json().get("data") or {}).get("node") or {}) if resp.status_code == 200 else {}
    status = op.get("status")
    if status in ("CREATED", "RUNNING", None):
        started = sync.started_at if sync.started_at.tzinfo else sync.started_at.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - started > STALE_SYNC_AFTER:
            _fail(sync, f"bulk operation still {status or 'unknown'} after {STALE_SYNC_AFTER}")
            await db.commit()
        return sync
    if status != "COMPLETED":
        _fail(sync, f"bulk operation {status}: {op.get('errorCode')}")
        await db.commit()
        return sync

    # Claim the import: the webhook job and the 5-min cron may both get here,
    # and only one of them should pay for the analysis.
    claimed = await db.execute(
        update(CatalogSync).where(CatalogSync.id == sync.id, CatalogSync.status == "running")
        .values(status="importing").execution_options(synchronize_session=False)
    )
    await db.commit()
    if claimed.rowcount != 1:
        return sync
    await db.refresh(sync)

    text = await fetch_text(op["url"]) if op.get("url") else ""   # no url = no products
    products = []
    for line in text.splitlines():
        if line.strip():
            parsed = parse_product(json.loads(line))
            if parsed:
                products.append(parsed)

    counts = await upsert_products(db, shop, products)
    await delete_missing(db, shop.id, {p["product_id"] for p in products})
    sync.total = counts["added"] + counts["updated"]
    sync.processed = sync.total
    await db.commit()

    await analyze_pending(db, shop, embedder, chat_fn, sync=sync)
    sync.status = "done"
    sync.finished_at = datetime.now(timezone.utc)
    await db.commit()
    logger.info("catalog_sync_done", shop=shop.shop_domain, kind=sync.kind, **counts,
                enriched=sync.enriched, failed=sync.failed)
    return sync


# ── Single product (webhooks) ────────────────────────────────────────────────

async def sync_product(
    db: AsyncSession,
    shop: Shop,
    token: str,
    product_id: str,
    embedder: Embedder,
    chat_fn=chat,
    gql: GraphQLFn = shopify_graphql_post,
) -> str:
    """Refresh one product from Shopify. Returns "upserted" | "removed" | "error"."""
    resp = await gql(shop.shop_domain, token, PRODUCT_QUERY, {"id": product_gid(product_id)})
    if resp.status_code != 200:
        return "error"
    parsed = parse_product((resp.json().get("data") or {}).get("product"))
    if parsed is None:
        await delete_missing_one(db, shop.id, str(product_id))
        await db.commit()
        return "removed"
    await upsert_products(db, shop, [parsed])
    await db.commit()
    await analyze_pending(db, shop, embedder, chat_fn)
    return "upserted"


async def delete_missing_one(db: AsyncSession, shop_id: uuid.UUID, product_id: str) -> None:
    await db.execute(delete(CatalogProductRow).where(
        CatalogProductRow.shop_id == shop_id, CatalogProductRow.product_id == str(product_id)))
