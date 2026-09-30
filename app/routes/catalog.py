"""Catalog page API (docs/TECHNICAL_PLAN.md §4.1, §7).

    GET   /api/catalog/status              counts, plan limit, latest sync (progress bar)
    GET   /api/catalog/products            paged list with search
    POST  /api/catalog/resync              queue a fresh export
    GET   /api/catalog/playground/options  intake vocabulary for the test form
    POST  /api/catalog/playground          run the gift finder on this shop's catalog
    PATCH /api/catalog/products/{id}       exclude / include; edit the gift profile

Literal routes are declared before the parameterized one.
"""
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from app.ai_models import AI_TIERS, ai_tier_for, model_for_shop, model_label
from app.jobs import enqueue
from app.llm import chat
from app.plan_guard import catalog_budget_for, generation_limit_for, get_cycle_cost_usd, get_generations_used
from app.services import metering
from app.services.catalog_sync import (
    ACTIVE_STATUSES, IN_PROGRESS, count_held, count_pending, product_limit, rereads_limit, rereads_remaining,
    row_to_product,
)
from app.services.gifting import vocab
from app.services.gifting.brief import GiftBrief, intake_options
from app.services.gifting.embeddings import OpenAIEmbedder
from app.services.gifting.profile import MAX_PITCH_CHARS, GiftProfile, apply_overrides, embedding_text
from app.services.gifting.retrieval import Intake
from core.db.models import CatalogProductRow, CatalogSync, Shop, UsageLog
from core.db.session import get_db
from core.shopify_deps import get_current_shop

router = APIRouter()

PAGE_SIZE = 25
# Sync now at most this often (webhooks + the nightly re-sync cover the rest).
MANUAL_SYNC_COOLDOWN = timedelta(minutes=15)
# Test searches don't use generations (yet) but cost us an LLM call each.
PLAYGROUND_DAILY_LIMIT = 30


SLOW_START = timedelta(minutes=2)


def _sync_json(s: CatalogSync | None) -> dict | None:
    if s is None:
        return None
    started = s.started_at.replace(tzinfo=timezone.utc) if s.started_at and s.started_at.tzinfo is None else s.started_at
    return {
        # Queued for a while = the background worker hasn't picked it up.
        "slow_start": s.status == "queued" and started is not None
                      and datetime.now(timezone.utc) - started > SLOW_START,
        "status": s.status, "kind": s.kind, "total": s.total, "enriched": s.enriched, "failed": s.failed,
        "error": s.error, "started_at": s.started_at.isoformat() if s.started_at else None,
        "finished_at": s.finished_at.isoformat() if s.finished_at else None,
    }


_PROFILE_KEYS = ("giftable", "recipients", "occasions", "vibes", "age_band", "gift_pitch")


def _product_json(r: CatalogProductRow) -> dict:
    prof = r.gift_profile or {}
    effective = asdict(apply_overrides(GiftProfile(**prof), r.merchant_overrides)) if prof else {}
    return {
        "product_id": r.product_id, "title": r.title, "image_url": r.image_url, "url": r.url,
        "product_type": r.product_type, "price_min": float(r.price_min), "price_max": float(r.price_max),
        "available": r.available, "excluded": r.excluded,
        "analyzed": r.gift_profile is not None and r.profile_hash == r.content_hash,
        # Edited since its last AI read but still searchable with the old profile.
        "update_pending": r.gift_profile is not None and r.profile_hash != r.content_hash,
        "profile_fallback": r.profile_fallback,
        # What search uses (merchant edits applied) and what the AI wrote (for "reset").
        "profile": {k: effective.get(k) for k in _PROFILE_KEYS} if prof else None,
        "ai_profile": {k: prof.get(k) for k in _PROFILE_KEYS} if prof else None,
        "overridden": bool(r.merchant_overrides),
    }


@router.get("/api/catalog/status")
async def catalog_status(shop: Shop = Depends(get_current_shop), db: AsyncSession = Depends(get_db)):
    mine = CatalogProductRow.shop_id == shop.id
    total = (await db.execute(select(func.count()).select_from(CatalogProductRow).where(mine))).scalar_one()
    excluded = (await db.execute(
        select(func.count()).select_from(CatalogProductRow).where(mine, CatalogProductRow.excluded.is_(True))
    )).scalar_one()
    held = await count_held(db, shop)          # edited, waiting for next cycle's re-reads
    pending = await count_pending(db, shop.id) - held
    latest = (await db.execute(
        select(CatalogSync).where(CatalogSync.shop_id == shop.id).order_by(CatalogSync.started_at.desc()).limit(1)
    )).scalar_one_or_none()
    remaining = rereads_remaining(shop)
    await db.commit()  # persists a cycle reset of the re-read counter
    next_manual = await _next_manual_sync_at(db, shop)
    return {
        "products": total, "analyzed": total - pending, "pending": pending, "excluded": excluded,
        "limit": product_limit(shop), "is_trial": shop.plan_status == "trial_active",
        "rereads_used": rereads_limit(shop) - remaining, "rereads_limit": rereads_limit(shop), "held": held,
        # This cycle's catalog analysis budget is used up (margin guarantee):
        # new/edited products wait for the next cycle. A flag only: budgets and
        # costs are never shown to merchants.
        "analysis_paused": await get_cycle_cost_usd(shop, db, catalog=True) >= catalog_budget_for(shop),
        "next_manual_sync_at": next_manual.isoformat() if next_manual else None,
        "sync": _sync_json(latest),
    }


async def _next_manual_sync_at(db: AsyncSession, shop: Shop) -> datetime | None:
    """When Sync now can be used again (None = now)."""
    last = (await db.execute(
        select(func.max(CatalogSync.started_at)).where(CatalogSync.shop_id == shop.id, CatalogSync.kind == "manual")
    )).scalar()
    if last is None:
        return None
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    ready = last + MANUAL_SYNC_COOLDOWN
    return ready if ready > datetime.now(timezone.utc) else None


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
    ready_at = await _next_manual_sync_at(db, shop)
    if ready_at:
        minutes = max(1, int((ready_at - datetime.now(timezone.utc)).total_seconds() // 60) + 1)
        raise HTTPException(429, f"You can sync again in {minutes} minute{'s' if minutes != 1 else ''}. "
                                 "Product changes also sync automatically.")

    # Recorded before enqueueing so the page shows "Waiting to start" at once.
    sync = CatalogSync(id=uuid.uuid4(), shop_id=shop.id, kind="manual", status="queued")
    db.add(sync)
    await db.commit()
    if not await enqueue("catalog_start_sync", str(shop.id), "manual"):
        sync.status, sync.error = "failed", "Background queue unavailable."
        sync.finished_at = datetime.now(timezone.utc)
        await db.commit()
        raise HTTPException(503, "Couldn't start the sync right now. Please try again in a few minutes.")
    return {"queued": True}


async def _playground_used_today(db: AsyncSession, shop: Shop) -> int:
    since = datetime.now(timezone.utc) - timedelta(days=1)
    return (await db.execute(             # reservations only (settle/refund rows don't count)
        select(func.count()).select_from(UsageLog).where(
            UsageLog.shop_id == shop.id, UsageLog.action_type == "playground",
            UsageLog.generations_consumed > 0, UsageLog.created_at >= since)
    )).scalar_one()


async def _playground_usage(db: AsyncSession, shop: Shop) -> dict:
    """Counters for the Try it page: test searches left today, and this
    month's generations (shared with the storefront). No costs, ever."""
    limit = generation_limit_for(shop)
    return {
        "tries_left_today": max(0, PLAYGROUND_DAILY_LIMIT - await _playground_used_today(db, shop)),
        "daily_limit": PLAYGROUND_DAILY_LIMIT,
        "generations_left": max(0, limit - await get_generations_used(shop, db)),
        "generation_limit": limit,
        "generations_per_search": AI_TIERS[ai_tier_for(shop.plan_tier, shop.selected_model)]["weight"],
    }


@router.get("/api/catalog/playground/options")
async def playground_options(shop: Shop = Depends(get_current_shop), db: AsyncSession = Depends(get_db)):
    return {**intake_options(), "usage": await _playground_usage(db, shop)}


@router.post("/api/catalog/playground")
async def playground_search(
    brief: GiftBrief,
    shop: Shop = Depends(get_current_shop),
    db: AsyncSession = Depends(get_db),
):
    if shop.plan_status not in ACTIVE_STATUSES:
        raise HTTPException(403, "Choose a plan to try the gift finder.")
    used_today = await _playground_used_today(db, shop)
    if used_today >= PLAYGROUND_DAILY_LIMIT:
        raise HTTPException(429, f"You've run {PLAYGROUND_DAILY_LIMIT} test searches today. Try again tomorrow.")

    # Metered like a shopper search (same generations, limits and AI budget),
    # so Try it can't push the store past its margin guarantee (app/config.py).
    model = model_for_shop(shop)
    intake = Intake(**brief.model_dump())
    result = await metering.run_gift_search(db, shop, intake, OpenAIEmbedder(), chat_fn=chat,
                                            action_type="playground")
    rec, latency_ms = result.recommendation, result.latency_ms

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
        "charged": result.charged,
        # Any monthly limit (generations or the internal cost budget) looks the
        # same to merchants: never expose which, or any cost figure.
        "ai_limit_reached": result.limited not in (None, "inactive"),
        "usage": await _playground_usage(db, shop),
    }


class ProfileOverrides(BaseModel):
    """Merchant edits; omitted fields keep the AI's value."""
    model_config = ConfigDict(extra="forbid")
    recipients: Optional[list[str]] = None
    occasions: Optional[list[str]] = None
    vibes: Optional[list[str]] = Field(default=None, max_length=5)
    age_band: Optional[str] = None
    gift_pitch: Optional[str] = Field(default=None, max_length=MAX_PITCH_CHARS)

    @model_validator(mode="after")
    def _in_vocab(self):
        for key, allowed in (("recipients", vocab.RECIPIENTS), ("occasions", vocab.OCCASIONS), ("vibes", vocab.VIBES)):
            values = getattr(self, key)
            if values is not None and any(v not in allowed for v in values):
                raise ValueError(f"unknown {key}")
        if self.age_band is not None and self.age_band not in vocab.AGE_BANDS:
            raise ValueError("unknown age band")
        return self


class ProductPatch(BaseModel):
    excluded: Optional[bool] = None
    # Present = replace the merchant's edits; null = reset to the AI's profile.
    overrides: Optional[ProfileOverrides] = None


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
    if body.excluded is not None:
        row.excluded = body.excluded
    if "overrides" in body.model_fields_set:
        row.merchant_overrides = body.overrides.model_dump(exclude_none=True) if body.overrides else None
        if row.gift_profile:
            # Search matches on the embedding, so re-embed now (no AI call, not a re-read).
            prof = apply_overrides(GiftProfile(**row.gift_profile), row.merchant_overrides)
            [row.embedding] = await OpenAIEmbedder().embed([embedding_text(row_to_product(row), prof)])
    await db.commit()
    return _product_json(row)
