"""Catalog sync jobs + crons (docs/TECHNICAL_PLAN.md §4.1).

Jobs (enqueued by webhooks / the billing callback):
  catalog_start_sync(shop_id, kind)        bulk export of active products
  catalog_finish_bulk(shop_domain, op_id)  import a finished export + analyze
  catalog_sync_product(shop_domain, pid)   one product after products/create|update

Crons:
  kick_catalog_syncs (every 5 min) — first sync for newly active shops, and
      finishes exports whose bulk_operations/finish webhook never arrived.
  reconcile_catalogs (02:30 UTC) — full re-export: catches missed webhooks,
      deletions, and products unlocked by a plan upgrade.
Each shop is isolated: one failure never aborts the others.
"""
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select

from app.llm import chat
from app.services import catalog_sync
from app.services.gifting.embeddings import OpenAIEmbedder
from core.db.models import CatalogSync, Shop
from core.db.session import AsyncSessionLocal
from core.shopify_auth import get_valid_access_token

logger = structlog.get_logger()


def _age(dt: datetime) -> timedelta:
    return datetime.now(timezone.utc) - (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc))


def _embedder():
    return OpenAIEmbedder()


async def _active_shop(db, *, shop_id=None, shop_domain=None) -> Shop | None:
    q = select(Shop).where(Shop.plan_status.in_(catalog_sync.ACTIVE_STATUSES))
    q = q.where(Shop.id == shop_id) if shop_id else q.where(Shop.shop_domain == shop_domain)
    return (await db.execute(q)).scalar_one_or_none()


async def catalog_start_sync(ctx: dict, shop_id: str, kind: str = "initial") -> None:
    async with AsyncSessionLocal() as db:
        shop = await _active_shop(db, shop_id=shop_id)
        if shop is None:
            return
        token = await get_valid_access_token(shop, db)
        await catalog_sync.start_bulk_sync(db, shop, token, kind)


async def catalog_finish_bulk(ctx: dict, shop_domain: str, bulk_operation_id: str) -> None:
    async with AsyncSessionLocal() as db:
        shop = await _active_shop(db, shop_domain=shop_domain)
        if shop is None:
            return
        token = await get_valid_access_token(shop, db)
        await catalog_sync.finish_bulk_sync(db, shop, token, bulk_operation_id, _embedder(), chat)


async def catalog_sync_product(ctx: dict, shop_domain: str, product_id: str) -> None:
    async with AsyncSessionLocal() as db:
        shop = await _active_shop(db, shop_domain=shop_domain)
        if shop is None:
            return
        token = await get_valid_access_token(shop, db)
        await catalog_sync.sync_product(db, shop, token, product_id, _embedder(), chat)


async def _active_shop_ids(db) -> list[tuple]:
    # Plain (id, domain) tuples: a rollback after one shop's failure expires
    # ORM objects, so each shop is re-loaded inside its own try block.
    return [tuple(r) for r in (await db.execute(
        select(Shop.id, Shop.shop_domain).where(Shop.plan_status.in_(catalog_sync.ACTIVE_STATUSES))
    )).all()]


async def kick_catalog_syncs(ctx: dict) -> None:
    started = checked = 0
    async with AsyncSessionLocal() as db:
        for shop_id, domain in await _active_shop_ids(db):
            try:
                shop = await db.get(Shop, shop_id)
                syncs = (await db.execute(
                    select(CatalogSync).where(CatalogSync.shop_id == shop.id)
                )).scalars().all()
                running = [s for s in syncs if s.status == "running" and s.bulk_operation_id]
                # An import whose worker died (deploy, crash) would block new syncs.
                for s in syncs:
                    if s.status == "importing" and _age(s.started_at) > catalog_sync.STALE_SYNC_AFTER:
                        s.status, s.error = "failed", "import interrupted"
                        s.finished_at = datetime.now(timezone.utc)
                await db.commit()
                if not syncs:
                    token = await get_valid_access_token(shop, db)
                    await catalog_sync.start_bulk_sync(db, shop, token, "initial")
                    started += 1
                for s in running:
                    token = await get_valid_access_token(shop, db)
                    await catalog_sync.finish_bulk_sync(db, shop, token, s.bulk_operation_id, _embedder(), chat)
                    checked += 1
            except Exception as e:  # noqa: BLE001
                await db.rollback()
                logger.warning("kick_catalog_sync_failed", shop=domain, error=str(e))
    logger.info("kick_catalog_syncs_complete", started=started, checked=checked)


async def reconcile_catalogs(ctx: dict) -> None:
    started = 0
    async with AsyncSessionLocal() as db:
        for shop_id, domain in await _active_shop_ids(db):
            try:
                shop = await db.get(Shop, shop_id)
                token = await get_valid_access_token(shop, db)
                await catalog_sync.start_bulk_sync(db, shop, token, "reconcile")
                started += 1
            except Exception as e:  # noqa: BLE001
                await db.rollback()
                logger.warning("reconcile_catalog_failed", shop=domain, error=str(e))
    logger.info("reconcile_catalogs_complete", started=started)
