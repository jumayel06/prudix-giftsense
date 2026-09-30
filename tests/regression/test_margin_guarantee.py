"""
REGRESSION: worst-case margin guarantee (app/config.py MARGIN_*).

Per billing cycle a shop's recorded AI spend is capped by two budgets:
shopper AI (searches, notes, Try it) and catalog analysis. The config
arithmetic must leave at least MARGIN_MIN of every plan's price, and the
budgets must sit above the measured cost of using 100% of the plan's
generations on its most expensive AI option, so normal heavy use never hits
them.

If a model swap, price change or limit change breaks this, CI fails here:
re-measure (scripts/eval_recs.py) and adjust budgets, limits or prices.
"""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.ai_models import AI_TIERS, SLOTS
from app.config import (
    MARGIN_IN_FLIGHT_SLACK_USD, MARGIN_MIN, PLANS, TRIAL_AI_BUDGET_MIN_USD, TRIAL_CATALOG_BUDGET_USD,
)
from app.plan_guard import ai_budget_for, catalog_budget_for
from app.services import catalog_sync, metering
from app.services.gifting.embeddings import FakeEmbedder
from core.db.models import CatalogProductRow, UsageLog
from tests.conftest import make_shop
from tests.regression.test_generation_refund import BRIEF, good_chat, logs, shop_with_catalog

# Measured per call (TECHNICAL_PLAN §8.2, eval 2026-09-28): gift search, and a
# gift note (800 in / 200 out). A model not listed here must be measured first.
MEASURED_USD = {
    "gpt-6-luna":      {"search": 0.0003, "note": 0.0002},
    "gpt-6-sol":       {"search": 0.0055, "note": 0.0036},
    "claude-sonnet-5": {"search": 0.0103, "note": 0.0036},
}
CATALOG_USD_PER_PRODUCT = 0.00014      # Luna, 400 products for $0.057


def _worst_per_generation(tier: str) -> float:
    model = SLOTS[AI_TIERS[tier]["slot"]]["model"]
    assert model in MEASURED_USD, f"measure {model} before routing a tier to it"
    return max(MEASURED_USD[model].values()) / AI_TIERS[tier]["weight"]


@pytest.mark.parametrize("tier", list(PLANS))
def test_every_plan_keeps_the_minimum_margin(tier):
    p = PLANS[tier]
    worst_cost = (p["hosting_usd"] + p["media_cost_usd"] + p["ai_budget_usd"] + p["catalog_budget_usd"]
                  + MARGIN_IN_FLIGHT_SLACK_USD)
    margin = 1 - worst_cost / p["price_usd"]
    assert margin >= MARGIN_MIN, f"{tier}: worst-case margin {margin:.1%}"


@pytest.mark.parametrize("tier", list(PLANS))
def test_budgets_cover_full_use_of_the_plan(tier):
    """100% of generations on the plan's priciest AI option fits the AI budget,
    and a full catalog read plus every re-read fits the catalog budget."""
    p = PLANS[tier]
    worst_gen = max(_worst_per_generation(t) for t in p["ai_tiers"])
    assert p["generation_limit"] * worst_gen <= p["ai_budget_usd"]
    catalog = (p["max_products"] + p["product_rereads_per_month"]) * CATALOG_USD_PER_PRODUCT
    assert catalog <= p["catalog_budget_usd"]


def test_trial_budgets_scale_down_with_a_floor():
    pro = make_shop(plan_tier="pro", plan_status="trial_active")
    assert ai_budget_for(pro) == TRIAL_AI_BUDGET_MIN_USD            # 12.75 × 150/4500 = 0.43 → floor
    assert catalog_budget_for(pro) == TRIAL_CATALOG_BUDGET_USD
    paid = make_shop(plan_tier="pro", plan_status="active")
    assert ai_budget_for(paid) == PLANS["pro"]["ai_budget_usd"]


def _spend(shop, usd, action="gift_search"):
    """Spend earlier in this cycle (yesterday), so the daily cap isn't what trips."""
    return UsageLog(id=uuid.uuid4(), shop_id=shop.id, action_type=action, generations_consumed=0,
                    model_used="gpt-6-sol", cost_usd=usd, created_at=datetime.now(timezone.utc) - timedelta(days=1))


@pytest.mark.asyncio
async def test_search_uses_templates_once_the_ai_budget_is_spent(db_session):
    shop = await shop_with_catalog(db_session, plan_tier="growth", selected_model="advanced",
                                   billing_cycle_start=datetime.now(timezone.utc) - timedelta(days=10))
    db_session.add(_spend(shop, PLANS["growth"]["ai_budget_usd"]))
    await db_session.commit()
    chat = good_chat()
    result = await metering.run_gift_search(db_session, shop, BRIEF, FakeEmbedder(), chat_fn=chat)
    assert result.limited == "cost_budget" and not result.charged and chat.await_count == 0
    assert result.recommendation.picks                          # shoppers still get picks


@pytest.mark.asyncio
async def test_catalog_spend_does_not_count_against_shoppers(db_session):
    shop = await shop_with_catalog(db_session, plan_tier="growth", selected_model="advanced")
    db_session.add(_spend(shop, 50.0, action="catalog_analysis"))
    await db_session.commit()
    result = await metering.run_gift_search(db_session, shop, BRIEF, FakeEmbedder(), chat_fn=good_chat())
    assert result.charged and result.limited is None


@pytest.mark.asyncio
async def test_try_it_counts_toward_generations_and_the_budget(db_session):
    shop = await shop_with_catalog(db_session, plan_tier="growth", selected_model="advanced")
    await metering.run_gift_search(db_session, shop, BRIEF, FakeEmbedder(), chat_fn=good_chat(),
                                   action_type="playground")
    rows = await logs(db_session, shop)
    assert {r.action_type for r in rows} == {"playground"} and sum(r.generations_consumed for r in rows) == 2


@pytest.mark.asyncio
async def test_catalog_analysis_stops_at_the_catalog_budget(db_session):
    shop = make_shop(plan_tier="growth")
    db_session.add(shop)
    await db_session.flush()
    db_session.add(_spend(shop, PLANS["growth"]["catalog_budget_usd"], action="catalog_analysis"))
    db_session.add(CatalogProductRow(shop_id=shop.id, product_id="1", handle="c", title="Candle", description="",
                                     product_type="Candle", vendor="", tags=[], price_min=20, price_max=20,
                                     available=True, content_hash="h1"))
    await db_session.commit()
    chat = good_chat()
    analyzed = await catalog_sync.analyze_pending(db_session, shop, FakeEmbedder(), chat)
    assert analyzed == 0 and chat.await_count == 0
