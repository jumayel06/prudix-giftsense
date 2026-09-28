"""Catalog page API (docs/TECHNICAL_PLAN.md §4.1, §7).

    GET   /api/catalog/status              counts, plan limit, latest sync (progress bar)
    GET   /api/catalog/products            paged list with search
    POST  /api/catalog/resync              queue a fresh export
    PATCH /api/catalog/products/{id}       exclude / include a product

Literal routes are declared before the parameterized one.
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.jobs import enqueue
from app.services.catalog_sync import ACTIVE_STATUSES, IN_PROGRESS, count_pending, product_limit
from core.db.models import CatalogProductRow, CatalogSync, Shop
from core.db.session import get_db
from core.shopify_deps import get_current_shop

router = APIRouter()

PAGE_SIZE = 25


def _sync_json(s: CatalogSync | None) -> dict | None:
    if s is None:
        return None
    return {
        "status": s.status, "kind": s.kind, "total": s.total, "enriched": s.enriched, "failed": s.failed,
        "error": s.error, "started_at": s.started_at.isoformat() if s.started_at else None,
        "finished_at": s.finished_at.isoformat() if s.finished_at else None,
    }


def _product_json(r: CatalogProductRow) -> dict:
    prof = r.gift_profile or {}
    return {
        "product_id": r.product_id, "title": r.title, "image_url": r.image_url, "url": r.url,
        "product_type": r.product_type, "price_min": float(r.price_min), "price_max": float(r.price_max),
        "available": r.available, "excluded": r.excluded,
        "analyzed": r.gift_profile is not None and r.profile_hash == r.content_hash,
        "profile_fallback": r.profile_fallback,
        "profile": {k: prof.get(k) for k in ("giftable", "recipients", "occasions", "vibes", "age_band", "gift_pitch")}
        if prof else None,
    }


@router.get("/api/catalog/status")
async def catalog_status(shop: Shop = Depends(get_current_shop), db: AsyncSession = Depends(get_db)):
    mine = CatalogProductRow.shop_id == shop.id
    total = (await db.execute(select(func.count()).select_from(CatalogProductRow).where(mine))).scalar_one()
    excluded = (await db.execute(
        select(func.count()).select_from(CatalogProductRow).where(mine, CatalogProductRow.excluded.is_(True))
    )).scalar_one()
    pending = await count_pending(db, shop.id)
    latest = (await db.execute(
        select(CatalogSync).where(CatalogSync.shop_id == shop.id).order_by(CatalogSync.started_at.desc()).limit(1)
    )).scalar_one_or_none()
    return {
        "products": total, "analyzed": total - pending, "pending": pending, "excluded": excluded,
        "limit": product_limit(shop), "is_trial": shop.plan_status == "trial_active",
        "sync": _sync_json(latest),
    }


@router.get("/api/catalog/products")
async def catalog_products(
    q: str = "",
    page: int = 1,
    shop: Shop = Depends(get_current_shop),
    db: AsyncSession = Depends(get_db),
):
    page = max(page, 1)
    where = [CatalogProductRow.shop_id == shop.id]
    if q.strip():
        like = f"%{q.strip().lower()}%"
        where.append(or_(func.lower(CatalogProductRow.title).like(like),
                         func.lower(CatalogProductRow.product_type).like(like)))
    total = (await db.execute(select(func.count()).select_from(CatalogProductRow).where(*where))).scalar_one()
    rows = (await db.execute(
        select(CatalogProductRow).options(defer(CatalogProductRow.embedding)).where(*where)
        .order_by(CatalogProductRow.title, CatalogProductRow.product_id)
        .offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)
    )).scalars().all()
    return {"products": [_product_json(r) for r in rows], "total": total, "page": page, "page_size": PAGE_SIZE}


@router.post("/api/catalog/resync", status_code=202)
async def catalog_resync(shop: Shop = Depends(get_current_shop), db: AsyncSession = Depends(get_db)):
    if shop.plan_status not in ACTIVE_STATUSES:
        raise HTTPException(403, "Choose a plan to analyze your catalog.")
    running = (await db.execute(
        select(CatalogSync.id).where(CatalogSync.shop_id == shop.id, CatalogSync.status.in_(IN_PROGRESS)).limit(1)
    )).first()
    if running:
        raise HTTPException(409, "A catalog sync is already in progress.")
    await enqueue("catalog_start_sync", str(shop.id), "manual")
    return {"queued": True}


class ProductPatch(BaseModel):
    excluded: bool


@router.patch("/api/catalog/products/{product_id}")
async def catalog_update_product(
    product_id: str,
    body: ProductPatch,
    shop: Shop = Depends(get_current_shop),
    db: AsyncSession = Depends(get_db),
):
    row = (await db.execute(
        select(CatalogProductRow).options(defer(CatalogProductRow.embedding))
        .where(CatalogProductRow.shop_id == shop.id, CatalogProductRow.product_id == product_id)
    )).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Product not found.")
    row.excluded = body.excluded
    await db.commit()
    return _product_json(row)
