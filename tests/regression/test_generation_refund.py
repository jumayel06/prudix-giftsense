"""
REGRESSION (ported from Prudix Commerce): generation refund pattern.

Every AI use writes UsageLog(+weight) up front, then (0 gens, tokens, cost) on
success or (-weight) on failure, so SUM(generations_consumed) is net usage.
GiftSense adds the storefront rules: over any limit the shopper still gets
picks (template reasons, no LLM call, nothing charged), and the reservation
is taken under a per-shop row lock so concurrent shoppers can't overspend.
"""
import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.ai_models import AI_TIER_WEIGHTS
from app.config import PLANS
from app.llm import LLMResponse
from app.services import metering
from app.services.gifting.embeddings import FakeEmbedder
from app.services.gifting.retrieval import Intake
from core.db.models import UsageLog
from tests.conftest import add_usage, make_shop
from tests.integration.test_playground import row

BRIEF = Intake(recipient="friend", occasion="birthday", budget_band="25_50", vibes=["cozy"])


async def net(db, shop) -> int:
    return int((await db.execute(select(func.coalesce(func.sum(UsageLog.generations_consumed), 0))
                                 .where(UsageLog.shop_id == shop.id))).scalar())


async def logs(db, shop):
    return (await db.execute(select(UsageLog).where(UsageLog.shop_id == shop.id)
                             .order_by(UsageLog.created_at))).scalars().all()


async def shop_with_catalog(db, **kw):
    shop = make_shop(**kw)
    db.add(shop)
    await db.flush()
    db.add_all([row(shop, str(i)) for i in range(4)])
    await db.commit()
    return shop


def good_chat():
    async def _chat(**kw):
        ids = [l.split("product_id: ")[1].split(" |")[0] for l in kw["prompt"].splitlines() if "product_id: " in l]
        return LLMResponse(json.dumps({"picks": [{"product_id": i, "fact": "soy wax", "reason": "Soy wax glow."}
                                                 for i in ids[:3]]}), 1500, 150, model=kw["model"])
    return AsyncMock(side_effect=_chat)


# ── reserve / settle / refund ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_success_nets_the_weight_and_records_cost(db_session):
    shop = await shop_with_catalog(db_session, plan_tier="pro", selected_model="premium")
    result = await metering.run_gift_search(db_session, shop, BRIEF, FakeEmbedder(), chat_fn=good_chat())
    assert result.charged and not result.recommendation.used_fallback
    assert await net(db_session, shop) == AI_TIER_WEIGHTS["premium"]
    rows = await logs(db_session, shop)
    assert [r.generations_consumed for r in rows] == [4, 0]
    assert rows[1].model_used == "claude-sonnet-5" and rows[1].tokens_input == 1500 and float(rows[1].cost_usd) > 0


@pytest.mark.asyncio
async def test_llm_failure_is_refunded_to_net_zero(db_session):
    shop = await shop_with_catalog(db_session, plan_tier="growth", selected_model="advanced")
    boom = AsyncMock(side_effect=RuntimeError("provider down"))
    result = await metering.run_gift_search(db_session, shop, BRIEF, FakeEmbedder(), chat_fn=boom)
    assert result.recommendation.picks and result.recommendation.used_fallback   # shopper still gets picks
    assert not result.charged and await net(db_session, shop) == 0
    assert [r.generations_consumed for r in await logs(db_session, shop)] == [2, -2]


@pytest.mark.asyncio
async def test_unparseable_output_is_refunded(db_session):
    shop = await shop_with_catalog(db_session)
    bad = AsyncMock(return_value=LLMResponse("not json", 900, 40, model="gpt-6-luna"))
    result = await metering.run_gift_search(db_session, shop, BRIEF, FakeEmbedder(), chat_fn=bad)
    assert not result.charged and await net(db_session, shop) == 0
    # The failed call still cost us tokens: recorded at 0 generations.
    assert sum(float(r.cost_usd) for r in await logs(db_session, shop)) > 0


@pytest.mark.asyncio
async def test_over_the_limit_serves_templates_without_calling_the_llm(db_session):
    shop = await shop_with_catalog(db_session, plan_tier="starter", selected_model="standard")
    await add_usage(db_session, shop, PLANS["starter"]["generation_limit"])
    chat = good_chat()
    result = await metering.run_gift_search(db_session, shop, BRIEF, FakeEmbedder(), chat_fn=chat)
    assert result.recommendation.picks and all(p.source == "template" for p in result.recommendation.picks)
    assert result.limited == "generation_limit" and not result.charged
    chat.assert_not_awaited()
    assert await net(db_session, shop) == PLANS["starter"]["generation_limit"]


@pytest.mark.asyncio
async def test_weight_must_fit_what_is_left(db_session):
    # 3 left but Premium needs 4 → templates, nothing reserved.
    shop = await shop_with_catalog(db_session, plan_tier="pro", selected_model="premium")
    await add_usage(db_session, shop, PLANS["pro"]["generation_limit"] - 3)
    result = await metering.run_gift_search(db_session, shop, BRIEF, FakeEmbedder(), chat_fn=good_chat())
    assert result.limited == "generation_limit"


@pytest.mark.asyncio
async def test_trial_cap_applies(db_session):
    shop = await shop_with_catalog(db_session, plan_tier="growth", plan_status="trial_active",
                                   selected_model="standard",
                                   trial_ends_at=datetime.now(timezone.utc) + timedelta(days=3))
    await add_usage(db_session, shop, PLANS["growth"]["trial_generations"])
    result = await metering.run_gift_search(db_session, shop, BRIEF, FakeEmbedder(), chat_fn=good_chat())
    assert result.limited == "generation_limit"


@pytest.mark.asyncio
async def test_daily_cost_cap_falls_back(db_session):
    shop = await shop_with_catalog(db_session, plan_tier="starter", selected_model="standard")
    db_session.add(UsageLog(id=uuid.uuid4(), shop_id=shop.id, action_type="gift_search", generations_consumed=0,
                            model_used="gpt-6-luna", cost_usd=PLANS["starter"]["daily_cost_cap_usd"]))
    await db_session.commit()
    result = await metering.run_gift_search(db_session, shop, BRIEF, FakeEmbedder(), chat_fn=good_chat())
    assert result.limited == "daily_cost_cap"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["cancelled", "expired", "uninstalled", "pending"])
async def test_no_ai_without_an_active_plan(db_session, status):
    # Cycle started 40 days ago: a cancelled shop's paid month is over.
    shop = await shop_with_catalog(db_session, plan_status=status,
                                   billing_cycle_start=datetime.now(timezone.utc) - timedelta(days=40))
    chat = good_chat()
    result = await metering.run_gift_search(db_session, shop, BRIEF, FakeEmbedder(), chat_fn=chat)
    assert result.limited == "inactive" and not chat.await_count


@pytest.mark.asyncio
async def test_cancelled_shop_in_its_paid_period_can_still_use_ai(db_session):
    shop = await shop_with_catalog(db_session, plan_status="cancelled",
                                   billing_cycle_start=datetime.now(timezone.utc) - timedelta(days=5))
    result = await metering.run_gift_search(db_session, shop, BRIEF, FakeEmbedder(), chat_fn=good_chat())
    assert result.charged


@pytest.mark.asyncio
async def test_sum_not_count_after_refunds(db_session):
    shop = await shop_with_catalog(db_session, plan_tier="growth", selected_model="advanced")
    await metering.run_gift_search(db_session, shop, BRIEF, FakeEmbedder(), chat_fn=AsyncMock(side_effect=RuntimeError))
    await metering.run_gift_search(db_session, shop, BRIEF, FakeEmbedder(), chat_fn=good_chat())
    assert len(await logs(db_session, shop)) == 4 and await net(db_session, shop) == 2


@pytest.mark.asyncio
async def test_hourly_store_cap_serves_templates(db_session, monkeypatch):
    # A bot rotating sessions/IPs still can't drain the month in an hour.
    shop = await shop_with_catalog(db_session, plan_tier="starter", selected_model="standard")
    await add_usage(db_session, shop, metering.hourly_generation_cap(shop))
    chat = good_chat()
    result = await metering.run_gift_search(db_session, shop, BRIEF, FakeEmbedder(), chat_fn=chat)
    assert result.limited == "hourly_cap" and not chat.await_count and result.recommendation.picks


def test_hourly_cap_is_a_tenth_of_the_month_with_a_floor():
    from tests.conftest import make_shop as _mk
    assert metering.hourly_generation_cap(_mk(plan_tier="starter")) == 60
    assert metering.hourly_generation_cap(_mk(plan_tier="pro")) == 450
    assert metering.hourly_generation_cap(_mk(plan_tier="growth", plan_status="trial_active")) == 175
