"""Playground: the merchant tries the gift finder on their own catalog.

Covers the DB-backed search (catalog_index) and the /api/catalog/playground
endpoints. The LLM is faked via app.routes.catalog.chat (never openai/anthropic)."""
import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.llm import LLMResponse
from app.main import app
from app.services import catalog_index
from app.services.gifting.embeddings import FakeEmbedder
from app.services.gifting.profile import embedding_text
from app.services.gifting.retrieval import Intake
from app.services.catalog_sync import row_to_product
from app.services.gifting.profile import GiftProfile
from app.services.gifting.enrichment import PROMPT_VERSION as ENRICH_VERSION
from core.db.models import CatalogProductRow, UsageLog
from core.db.session import get_db
from tests.conftest import TEST_SHOP_DOMAIN, make_shop

EMB = FakeEmbedder()
BRIEF = {"recipient": "friend", "occasion": "birthday", "budget_band": "25_50", "vibes": ["cozy"]}


def row(shop, pid, *, title=None, price=30, analyzed=True, **kw):
    prof = {"giftable": 0.9, "recipients": ["friend"], "occasions": ["birthday"], "vibes": ["cozy"],
            "interests": [], "age_band": "adult", "gift_pitch": f"{title or pid} for cozy nights.",
            "facts": ["soy wax"]}
    r = CatalogProductRow(id=uuid.uuid4(), shop_id=shop.id, product_id=pid, title=title or f"Candle {pid}",
                          price_min=price, price_max=price, content_hash="h", profile_hash="h",
                          profile_version=ENRICH_VERSION, gift_profile=prof if analyzed else None, **kw)
    if analyzed:
        r.embedding = EMB.embed_sync(embedding_text(row_to_product(r), GiftProfile(**prof)))
    return r


def picking_chat(n=3):
    async def _chat(**kw):
        ids = [line.split("product_id: ")[1].split(" |")[0] for line in kw["prompt"].splitlines()
               if "product_id: " in line][:n]
        return LLMResponse(json.dumps({"picks": [{"product_id": i, "fact": "soy wax",
                                                  "reason": "Soy wax for slow evenings."} for i in ids]}), 1500, 150)
    return AsyncMock(side_effect=_chat)


# ── catalog_index ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_only_eligible_in_budget_products_are_loaded(db_session):
    shop = make_shop()
    db_session.add(shop)
    await db_session.flush()
    db_session.add_all([
        row(shop, "ok"), row(shop, "excluded", excluded=True), row(shop, "oos", available=False),
        row(shop, "pending", analyzed=False), row(shop, "pricey", price=300),
    ])
    await db_session.commit()
    items = await catalog_index.load_items(db_session, shop.id, Intake(**BRIEF))
    assert [i.product.product_id for i in items] == ["ok"]


@pytest.mark.asyncio
async def test_merchant_overrides_apply_to_search(db_session):
    shop = make_shop()
    db_session.add(shop)
    await db_session.flush()
    db_session.add(row(shop, "1", merchant_overrides={"vibes": ["luxurious"]}))
    await db_session.commit()
    [item] = await catalog_index.load_items(db_session, shop.id, Intake(**BRIEF))
    assert item.profile.vibes == ["luxurious"]


@pytest.mark.asyncio
async def test_small_catalog_skips_the_embedding_call(db_session):
    shop = make_shop()
    db_session.add(shop)
    await db_session.flush()
    db_session.add_all([row(shop, str(i)) for i in range(5)])
    await db_session.commit()
    embedder = AsyncMock(wraps=EMB)
    embedder.embed = AsyncMock(side_effect=EMB.embed)
    rec, ms = await catalog_index.recommend_for_shop(db_session, shop.id, Intake(**BRIEF), embedder,
                                                     "claude-haiku-4-5", chat_fn=picking_chat())
    assert rec.mode == "small_catalog" and len(rec.picks) == 3 and ms >= 0
    embedder.embed.assert_not_awaited()


@pytest.mark.asyncio
async def test_large_catalog_embeds_the_brief_once(db_session):
    shop = make_shop()
    db_session.add(shop)
    await db_session.flush()
    db_session.add_all([row(shop, str(i), title=f"Gift {i}") for i in range(70)])
    await db_session.commit()
    embedder = AsyncMock()
    embedder.embed = AsyncMock(side_effect=EMB.embed)
    rec, _ = await catalog_index.recommend_for_shop(db_session, shop.id, Intake(**BRIEF), embedder,
                                                    "claude-haiku-4-5", chat_fn=picking_chat())
    assert rec.mode == "vector" and len(rec.picks) == 3
    assert embedder.embed.await_count == 1


# ── API ──────────────────────────────────────────────────────────────────────

def call(db_session, method, path, **kw):
    async def override_db():
        yield db_session
    app.dependency_overrides[get_db] = override_db
    try:
        return TestClient(app).request(method, f"{path}?shop={TEST_SHOP_DOMAIN}", **kw)
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
def fake_models():
    with patch("app.routes.catalog.chat", picking_chat()) as chat, \
         patch("app.routes.catalog.OpenAIEmbedder", return_value=EMB):
        yield chat


@pytest.mark.asyncio
async def test_options_lists_the_intake_vocabulary(db_session):
    db_session.add(make_shop())
    await db_session.commit()
    data = call(db_session, "GET", "/api/catalog/playground/options").json()
    assert {"value": "friend", "label": "Friend"} in data["recipients"]
    assert [b["value"] for b in data["budgets"]][:2] == ["under_25", "25_50"]
    assert data["budgets"][1]["label"] == "$25–50"
    assert "cozy" in [v["value"] for v in data["vibes"]]


@pytest.mark.asyncio
async def test_playground_returns_picks_and_is_metered_like_a_search(db_session, fake_models):
    shop = make_shop(selected_model="advanced")
    db_session.add(shop)
    await db_session.flush()
    db_session.add_all([row(shop, str(i)) for i in range(4)])
    await db_session.commit()

    resp = call(db_session, "POST", "/api/catalog/playground", json={**BRIEF, "free_text": "loves reading"})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["picks"]) == 3 and data["ai_tier"] == "advanced" and data["ai_tier_label"] == "Advanced"
    assert data["ai_model_label"] == "GPT-6 Sol"
    pick = data["picks"][0]
    assert pick["title"].startswith("Candle") and pick["reason"] and pick["source"] == "ai"
    assert pick["price_min"] == 30.0
    # Metered like a shopper search: reserve +2 (Advanced), settle with cost.
    logs = (await db_session.execute(select(UsageLog).order_by(UsageLog.created_at))).scalars().all()
    assert {l.action_type for l in logs} == {"playground"}
    assert sum(l.generations_consumed for l in logs) == 2 and sum(float(l.cost_usd) for l in logs) > 0
    assert data["charged"] is True and data["ai_limit_reached"] is False
    assert not any(k in data for k in ("limited", "cost_usd", "budget"))


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    {"recipient": "alien"}, {"occasion": "nope"}, {"budget_band": "cheap"}, {"vibes": ["cozy", "wild"]},
    {"vibes": ["cozy", "funny", "classic", "techy"]}, {"free_text": "x" * 201}, {"age_band": "ancient"},
])
async def test_playground_rejects_values_outside_the_vocabulary(db_session, fake_models, bad):
    db_session.add(make_shop())
    await db_session.commit()
    assert call(db_session, "POST", "/api/catalog/playground", json={**BRIEF, **bad}).status_code == 422


@pytest.mark.asyncio
async def test_playground_empty_catalog_says_so(db_session, fake_models):
    db_session.add(make_shop())
    await db_session.commit()
    data = call(db_session, "POST", "/api/catalog/playground", json=BRIEF).json()
    assert data["picks"] == [] and fake_models.await_count == 0


@pytest.mark.asyncio
async def test_playground_daily_cap(db_session, fake_models):
    from app.routes.catalog import PLAYGROUND_DAILY_LIMIT
    shop = make_shop()
    db_session.add(shop)
    await db_session.flush()
    # Reservation rows (+1 each); settle rows (0) must not count toward the cap.
    db_session.add_all([UsageLog(id=uuid.uuid4(), shop_id=shop.id, action_type="playground",
                                 generations_consumed=g, model_used="gpt-6-luna")
                        for _ in range(PLAYGROUND_DAILY_LIMIT) for g in (1, 0)])
    await db_session.commit()
    resp = call(db_session, "POST", "/api/catalog/playground", json=BRIEF)
    assert resp.status_code == 429


@pytest.mark.asyncio
async def test_playground_requires_an_active_plan(db_session, fake_models):
    db_session.add(make_shop(plan_status="cancelled"))
    await db_session.commit()
    assert call(db_session, "POST", "/api/catalog/playground", json=BRIEF).status_code == 403


@pytest.mark.asyncio
async def test_playground_uses_plan_default_when_selected_model_not_allowed(db_session, fake_models):
    shop = make_shop(plan_tier="starter", selected_model="claude-sonnet-5")
    db_session.add(shop)
    await db_session.flush()
    db_session.add(row(shop, "1"))
    await db_session.commit()
    assert call(db_session, "POST", "/api/catalog/playground", json=BRIEF).json()["ai_tier"] == "standard"


@pytest.mark.asyncio
async def test_try_it_shows_what_is_left_today_and_this_month(db_session, fake_models):
    from app.routes.catalog import PLAYGROUND_DAILY_LIMIT
    shop = make_shop(selected_model="advanced")            # growth: 1,750 generations, Advanced = 2 each
    db_session.add(shop)
    await db_session.flush()
    db_session.add_all([row(shop, str(i)) for i in range(4)])
    await db_session.commit()
    before = call(db_session, "GET", "/api/catalog/playground/options").json()["usage"]
    assert before == {"tries_left_today": PLAYGROUND_DAILY_LIMIT, "daily_limit": PLAYGROUND_DAILY_LIMIT,
                      "generations_left": 1750, "generation_limit": 1750, "generations_per_search": 2}
    after = call(db_session, "POST", "/api/catalog/playground", json=BRIEF).json()["usage"]
    assert after["tries_left_today"] == PLAYGROUND_DAILY_LIMIT - 1 and after["generations_left"] == 1748
