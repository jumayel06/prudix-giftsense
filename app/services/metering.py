"""Generation metering for shopper-facing AI (docs/TECHNICAL_PLAN.md §8.3).

Refund pattern (Commerce): reserve writes UsageLog(+weight) before the LLM
call; settle writes (0 gens, tokens, cost) on success; refund writes (-weight)
on failure. Usage = SUM(generations_consumed), never COUNT.

The gate is atomic per shop: reserve() locks the shop row (SELECT … FOR
UPDATE), sums the cycle's usage and inserts the reservation in one
transaction, so concurrent shoppers can't both take the last generations.
(The plan doc suggested a counter column with a conditional UPDATE; a row lock
keeps usage_logs as the single source of truth instead.)

Storefront rule: limits never break the widget. When the shop can't use AI
(inactive plan, generation limit, trial cap, daily cost cap), reserve()
returns None and the caller serves template reasons with no LLM call.
"""
import uuid
from dataclasses import dataclass

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai_models import AI_TIERS, ai_tier_for, model_for_shop
from app.llm import calc_cost, chat
from app.plan_guard import (
    daily_cost_cap_for, generation_limit_for, get_daily_cost_usd, get_generations_used, may_generate,
)
from app.services import catalog_index
from app.services.gifting.embeddings import Embedder
from app.services.gifting.pipeline import Recommendation
from app.services.gifting.rerank import PROMPT_VERSION as RERANK_PROMPT_VERSION
from app.services.gifting.retrieval import Intake
from core.db.models import Shop, UsageLog

logger = structlog.get_logger()


@dataclass
class Reservation:
    shop_id: uuid.UUID
    action_type: str
    weight: int
    model: str


async def _lock_shop(db: AsyncSession, shop_id) -> None:
    # Serializes reservations per shop on Postgres; a no-op on SQLite (tests).
    await db.execute(select(Shop.id).where(Shop.id == shop_id).with_for_update())


async def reserve(db: AsyncSession, shop: Shop, action_type: str) -> tuple[Reservation | None, str | None]:
    """Reserve the shop's AI-tier weight in generations for one AI use.
    Returns (reservation, None), or (None, reason) when AI isn't allowed:
    "inactive" | "generation_limit" | "daily_cost_cap"."""
    if not may_generate(shop):
        return None, "inactive"
    tier = ai_tier_for(shop.plan_tier, shop.selected_model)
    weight = AI_TIERS[tier]["weight"]
    model = model_for_shop(shop)

    await _lock_shop(db, shop.id)
    cap = daily_cost_cap_for(shop)
    if cap > 0 and await get_daily_cost_usd(shop, db) >= cap:
        await db.commit()  # nothing written; releases the lock (a rollback would expire `shop`)
        logger.warning("metering_daily_cost_cap", shop=shop.shop_domain, cap=cap)
        return None, "daily_cost_cap"
    if generation_limit_for(shop) - await get_generations_used(shop, db) < weight:
        await db.commit()
        return None, "generation_limit"
    db.add(UsageLog(id=uuid.uuid4(), shop_id=shop.id, action_type=action_type, generations_consumed=weight,
                    model_used=model, cost_usd=0, prompt_version=RERANK_PROMPT_VERSION))
    await db.commit()  # releases the lock
    return Reservation(shop.id, action_type, weight, model), None


async def settle(db: AsyncSession, r: Reservation, input_tokens: int, output_tokens: int, model: str | None = None,
                 duration_ms: int = 0) -> None:
    """Success: the reservation stands; record tokens and cost (0 generations)."""
    used = model or r.model
    db.add(UsageLog(id=uuid.uuid4(), shop_id=r.shop_id, action_type=r.action_type, generations_consumed=0,
                    tokens_input=input_tokens, tokens_output=output_tokens, model_used=used,
                    cost_usd=calc_cost(used, input_tokens, output_tokens), prompt_version=RERANK_PROMPT_VERSION,
                    duration_ms=duration_ms))
    await db.commit()


async def refund(db: AsyncSession, r: Reservation, input_tokens: int = 0, output_tokens: int = 0,
                 model: str | None = None) -> None:
    """Failure: give the generations back. Tokens a failed call still burned
    are recorded as cost on the same row, so spend stays visible."""
    used = model or r.model
    db.add(UsageLog(id=uuid.uuid4(), shop_id=r.shop_id, action_type=r.action_type, generations_consumed=-r.weight,
                    tokens_input=input_tokens, tokens_output=output_tokens, model_used=used,
                    cost_usd=calc_cost(used, input_tokens, output_tokens), prompt_version=RERANK_PROMPT_VERSION))
    await db.commit()


@dataclass
class GiftSearchResult:
    recommendation: Recommendation
    latency_ms: int
    charged: bool                 # True when generations were spent
    limited: str | None = None    # why AI wasn't used, if it wasn't


async def run_gift_search(
    db: AsyncSession, shop: Shop, intake: Intake, embedder: Embedder, chat_fn=chat,
) -> GiftSearchResult:
    """A metered shopper gift search. Always returns picks (templates when AI
    is unavailable or fails); charges generations only when the AI's picks
    were used."""
    model = model_for_shop(shop)
    reservation, limited = await reserve(db, shop, "gift_search")
    rec, latency_ms = await catalog_index.recommend_for_shop(
        db, shop.id, intake, embedder, model, chat_fn=chat_fn, use_llm=reservation is not None)
    if reservation is None:
        return GiftSearchResult(rec, latency_ms, charged=False, limited=limited)
    if rec.used_fallback:
        await refund(db, reservation, rec.input_tokens, rec.output_tokens, rec.model or None)
        return GiftSearchResult(rec, latency_ms, charged=False)
    await settle(db, reservation, rec.input_tokens, rec.output_tokens, rec.model or None, latency_ms)
    return GiftSearchResult(rec, latency_ms, charged=True)
