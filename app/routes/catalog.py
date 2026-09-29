"""Catalog page API (docs/TECHNICAL_PLAN.md §4.1, §7).

    GET   /api/catalog/status              counts, plan limit, latest sync (progress bar)
    GET   /api/catalog/products            paged list with search
    POST  /api/catalog/resync              queue a fresh export
    GET   /api/catalog/playground/options  intake vocabulary for the test form
    POST  /api/catalog/playground          run the gift finder on this shop's catalog
    PATCH /api/catalog/products/{id}       exclude / include a product

Literal routes are declared before the parameterized one.
"""
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.ai_models import AI_TIERS, ai_tier_for, model_for_shop, model_label
from app.jobs import enqueue
from app.llm import calc_cost, chat
from app.services import catalog_index
from app.services.gifting import vocab
from app.services.gifting.embeddings import OpenAIEmbedder
from app.services.gifting.rerank import PROMPT_VERSION as RERANK_PROMPT_VERSION
from app.services.gifting.rerank import budget_label
from app.services.gifting.retrieval import Intake
from app.services.catalog_sync import ACTIVE_STATUSES, IN_PROGRESS, count_pending, product_limit
from core.db.models import CatalogProductRow, CatalogSync, Shop, UsageLog
from core.db.session import get_db
from core.shopify_deps import get_current_shop

router = APIRouter()

PAGE_SIZE = 25
# Test searches don't use generations (yet) but cost us an LLM call each.
PLAYGROUND_DAILY_LIMIT = 30


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


def _options(values: list[str]) -> list[dict]:
    return [{"value": v, "label": vocab.LABELS.get(v, v)} for v in values]


@router.get("/api/catalog/playground/options")
async def playground_options(shop: Shop = Depends(get_current_shop)):
    return {
        "recipients": _options(vocab.RECIPIENTS),
        "occasions": _options(vocab.OCCASIONS),
        "vibes": _options(vocab.VIBES),
        "age_bands": _options(vocab.AGE_BANDS),
        "budgets": [{"value": b, "label": budget_label(b)} for b in vocab.BUDGET_BANDS],
        "max_vibes": 3,
    }


class PlaygroundBrief(BaseModel):
    recipient: str
    occasion: str
    budget_band: str
    vibes: list[str] = Field(default_factory=list, max_length=3)
    age_band: Optional[str] = None
    free_text: str = Field(default="", max_length=200)

    @field_validator("recipient")
    @classmethod
    def _recipient(cls, v):
        if v not in vocab.RECIPIENTS:
            raise ValueError("unknown recipient")
        return v

    @field_validator("occasion")
    @classmethod
    def _occasion(cls, v):
        if v not in vocab.OCCASIONS:
            raise ValueError("unknown occasion")
        return v

    @field_validator("budget_band")
    @classmethod
    def _budget(cls, v):
        if v not in vocab.BUDGET_BANDS:
            raise ValueError("unknown budget")
        return v

    @field_validator("vibes")
    @classmethod
    def _vibes(cls, v):
        if any(x not in vocab.VIBES for x in v):
            raise ValueError("unknown vibe")
        return v

    @field_validator("age_band")
    @classmethod
    def _age(cls, v):
        if v is not None and v not in vocab.AGE_BANDS:
            raise ValueError("unknown age band")
        return v


@router.post("/api/catalog/playground")
async def playground_search(
    brief: PlaygroundBrief,
    shop: Shop = Depends(get_current_shop),
    db: AsyncSession = Depends(get_db),
):
    if shop.plan_status not in ACTIVE_STATUSES:
        raise HTTPException(403, "Choose a plan to try the gift finder.")
    since = datetime.now(timezone.utc) - timedelta(days=1)
    used_today = (await db.execute(
        select(func.count()).select_from(UsageLog).where(
            UsageLog.shop_id == shop.id, UsageLog.action_type == "playground", UsageLog.created_at >= since)
    )).scalar_one()
    if used_today >= PLAYGROUND_DAILY_LIMIT:
        raise HTTPException(429, f"You've run {PLAYGROUND_DAILY_LIMIT} test searches today. Try again tomorrow.")

    model = model_for_shop(shop)
    intake = Intake(**brief.model_dump())
    rec, latency_ms = await catalog_index.recommend_for_shop(db, shop.id, intake, OpenAIEmbedder(), model, chat_fn=chat)

    if rec.input_tokens or rec.output_tokens:
        db.add(UsageLog(id=uuid.uuid4(), shop_id=shop.id, action_type="playground", generations_consumed=0,
                        tokens_input=rec.input_tokens, tokens_output=rec.output_tokens, model_used=rec.model or model,
                        cost_usd=calc_cost(rec.model or model, rec.input_tokens, rec.output_tokens),
                        prompt_version=RERANK_PROMPT_VERSION, duration_ms=latency_ms))
        await db.commit()

    return {
        "picks": [{
            "product_id": p.product.product_id, "title": p.product.title, "image_url": p.product.image_url,
            "url": p.product.url, "price_min": p.product.price_min, "price_max": p.product.price_max,
            "reason": p.reason, "source": p.source,
        } for p in rec.picks],
        "ai_tier": (tier := ai_tier_for(shop.plan_tier, shop.selected_model)),
        "ai_tier_label": AI_TIERS[tier]["label"],
        "ai_model_label": model_label(rec.model or model),  # the model that actually answered
        "mode": rec.mode, "latency_ms": latency_ms, "used_fallback": rec.used_fallback,
        "candidates_considered": rec.candidates_considered,
    }


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
