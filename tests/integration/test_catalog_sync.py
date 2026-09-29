"""Catalog sync: Shopify products → catalog_products → AI profile + embedding.

Shopify GraphQL and the JSONL download are faked; the LLM is a fake chat_fn
returning LLMResponse (never patch openai/anthropic)."""
import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import func, select

from app.llm import LLMResponse
from app.services import catalog_sync as cs
from app.services.gifting.embeddings import FakeEmbedder
from core.db.models import CatalogProductRow, CatalogSync, UsageLog
from tests.conftest import make_shop


def node(pid, *, title=None, price="30.00", price_max=None, status="ACTIVE", gift_card=False,
         published="2026-08-20T10:00:00Z", tracks=True, inventory=5, tags=("cozy",), desc="<p>Soft.</p>"):
    return {
        "id": f"gid://shopify/Product/{pid}", "handle": f"p-{pid}", "title": title or f"Throw {pid}",
        "descriptionHtml": desc, "productType": "Blankets", "vendor": "Wool Co", "tags": list(tags),
        "status": status, "isGiftCard": gift_card, "publishedAt": published,
        "onlineStoreUrl": None,  # null on password-protected stores even when published
        "totalInventory": inventory, "tracksInventory": tracks,
        "priceRangeV2": {"minVariantPrice": {"amount": price}, "maxVariantPrice": {"amount": price_max or price}},
        "featuredMedia": {"preview": {"image": {"url": f"https://cdn/{pid}.jpg"}}},
    }


class FakeChat:
    def __init__(self):
        self.calls = 0

    async def __call__(self, **kw):
        self.calls += 1
        return LLMResponse(json.dumps({"giftable": 0.9, "recipients": ["friend"], "occasions": ["birthday"],
                                       "vibes": ["cozy"], "gift_pitch": "A soft throw.", "facts": ["wool"]}),
                           1000, 200)


def gql_response(data, status=200):
    resp = MagicMock(status_code=status)
    resp.json.return_value = {"data": data}
    return resp


async def add_shop(db, **kw):
    shop = make_shop(**kw)
    db.add(shop)
    await db.commit()
    return shop


async def rows(db, shop):
    return {r.product_id: r for r in (await db.execute(
        select(CatalogProductRow).where(CatalogProductRow.shop_id == shop.id))).scalars()}


# ── parsing ──────────────────────────────────────────────────────────────────

def test_parse_product_maps_fields():
    p = cs.parse_product(node(7, price="20.00", price_max="45.50"))
    assert p["product_id"] == "7" and p["price_min"] == 20.0 and p["price_max"] == 45.5
    assert p["image_url"] == "https://cdn/7.jpg" and p["available"] is True and p["tags"] == ["cozy"]
    assert p["url"] == "/products/p-7"   # storefront-relative: works on any domain, password page or not


@pytest.mark.parametrize("kw", [
    {"status": "DRAFT"}, {"status": "ARCHIVED"}, {"gift_card": True}, {"published": None}, {"price": "0.00"},
])
def test_parse_product_skips_non_gifts(kw):
    assert cs.parse_product(node(1, **kw)) is None


def test_availability_from_inventory():
    assert cs.parse_product(node(1, tracks=True, inventory=0))["available"] is False
    assert cs.parse_product(node(1, tracks=False, inventory=0))["available"] is True


def test_content_hash_ignores_price_and_stock_but_not_text():
    a = cs.parse_product(node(1))
    assert cs.content_hash(a) == cs.content_hash(cs.parse_product(node(1, price="99.00", inventory=0)))
    assert cs.content_hash(a) != cs.content_hash(cs.parse_product(node(1, title="New name")))
    assert cs.content_hash(a) == cs.content_hash(cs.parse_product(node(1, tags=("cozy",))))


def test_product_limit_by_plan_and_trial():
    assert cs.product_limit(make_shop(plan_tier="growth", plan_status="active")) == 2000
    assert cs.product_limit(make_shop(plan_tier="pro", plan_status="trial_active")) == 100
    assert cs.product_limit(make_shop(plan_tier="starter", plan_status="active")) == 250
    assert cs.product_limit(make_shop(plan_tier="growth", plan_status="cancelled")) == 0


# ── upsert + analyze ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_upsert_respects_the_product_limit(db_session, monkeypatch):
    shop = await add_shop(db_session, plan_tier="growth", plan_status="trial_active")
    monkeypatch.setattr(cs, "TRIAL_MAX_PRODUCTS", 2)
    counts = await cs.upsert_products(db_session, shop, [cs.parse_product(node(i)) for i in range(1, 5)])
    await db_session.commit()
    assert counts == {"added": 2, "updated": 0, "over_limit": 2}
    # Existing products still update when the shop is at its limit.
    counts = await cs.upsert_products(db_session, shop, [cs.parse_product(node(1, price="12.00"))])
    await db_session.commit()
    assert counts["updated"] == 1 and float((await rows(db_session, shop))["1"].price_min) == 12.0


@pytest.mark.asyncio
async def test_analyze_enriches_embeds_and_logs_cost_without_using_generations(db_session):
    shop = await add_shop(db_session)
    await cs.upsert_products(db_session, shop, [cs.parse_product(node(i)) for i in (1, 2, 3)])
    await db_session.commit()
    chat = FakeChat()

    assert await cs.analyze_pending(db_session, shop, FakeEmbedder(), chat) == 3
    r = (await rows(db_session, shop))["1"]
    assert r.gift_profile["vibes"] == ["cozy"] and r.profile_hash == r.content_hash
    assert len(r.embedding) == 512 and r.profile_fallback is False
    log = (await db_session.execute(select(UsageLog))).scalar_one()
    assert log.action_type == "catalog_analysis" and log.generations_consumed == 0
    assert log.tokens_input == 3000 and float(log.cost_usd) > 0

    # Nothing pending → no LLM calls.
    assert await cs.analyze_pending(db_session, shop, FakeEmbedder(), chat) == 0
    assert chat.calls == 3


@pytest.mark.asyncio
async def test_price_change_is_free_but_text_change_rereads(db_session):
    shop = await add_shop(db_session)
    await cs.upsert_products(db_session, shop, [cs.parse_product(node(1)), cs.parse_product(node(2))])
    await db_session.commit()
    chat = FakeChat()
    await cs.analyze_pending(db_session, shop, FakeEmbedder(), chat)

    await cs.upsert_products(db_session, shop, [cs.parse_product(node(1, price="55.00")),
                                                cs.parse_product(node(2, title="Merino throw"))])
    await db_session.commit()
    assert await cs.count_pending(db_session, shop.id) == 1
    await cs.analyze_pending(db_session, shop, FakeEmbedder(), chat)
    assert chat.calls == 3


@pytest.mark.asyncio
async def test_embedding_failure_keeps_products_pending(db_session):
    shop = await add_shop(db_session)
    await cs.upsert_products(db_session, shop, [cs.parse_product(node(1))])
    await db_session.commit()
    broken = MagicMock(embed=AsyncMock(side_effect=RuntimeError("openai down")))

    assert await cs.analyze_pending(db_session, shop, broken, FakeChat()) == 0
    assert await cs.count_pending(db_session, shop.id) == 1
    # Next run only needs the embedding: the profile is reused, no new LLM call.
    chat = FakeChat()
    assert await cs.analyze_pending(db_session, shop, FakeEmbedder(), chat) == 1
    assert chat.calls == 0


# ── bulk sync ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_start_bulk_sync_records_operation_and_reuses_running_sync(db_session):
    shop = await add_shop(db_session)
    gql = AsyncMock(return_value=gql_response({"bulkOperationRunQuery": {
        "bulkOperation": {"id": "gid://shopify/BulkOperation/1", "status": "CREATED"}, "userErrors": []}}))
    s1 = await cs.start_bulk_sync(db_session, shop, "tok", "initial", gql=gql)
    s2 = await cs.start_bulk_sync(db_session, shop, "tok", "reconcile", gql=gql)
    assert s1.id == s2.id and s1.bulk_operation_id == "gid://shopify/BulkOperation/1"
    assert gql.await_count == 1


@pytest.mark.asyncio
async def test_start_bulk_sync_user_error_marks_failed(db_session):
    shop = await add_shop(db_session)
    gql = AsyncMock(return_value=gql_response({"bulkOperationRunQuery": {
        "bulkOperation": None, "userErrors": [{"field": None, "message": "A bulk query is already running"}]}}))
    sync = await cs.start_bulk_sync(db_session, shop, "tok", "initial", gql=gql)
    assert sync.status == "failed" and "already running" in sync.error


@pytest.mark.asyncio
async def test_finish_bulk_imports_removes_missing_and_analyzes(db_session):
    shop = await add_shop(db_session)
    # A product that was deleted in Shopify since the last sync.
    await cs.upsert_products(db_session, shop, [cs.parse_product(node(99))])
    db_session.add(CatalogSync(id=uuid.uuid4(), shop_id=shop.id, kind="reconcile", status="running",
                               bulk_operation_id="op1"))
    await db_session.commit()

    jsonl = "\n".join(json.dumps(n) for n in [node(1), node(2), node(3, status="DRAFT")]) + "\n"
    gql = AsyncMock(return_value=gql_response({"node": {"id": "op1", "status": "COMPLETED", "url": "https://x"}}))
    sync = await cs.finish_bulk_sync(db_session, shop, "tok", "op1", FakeEmbedder(), FakeChat(), gql=gql,
                                     fetch_text=AsyncMock(return_value=jsonl))

    assert set(await rows(db_session, shop)) == {"1", "2"}
    assert sync.status == "done" and sync.total == 2 and sync.enriched == 2 and sync.finished_at
    assert await cs.count_pending(db_session, shop.id) == 0


@pytest.mark.asyncio
async def test_finish_bulk_still_running_is_a_no_op_and_failed_op_fails_sync(db_session):
    shop = await add_shop(db_session)
    db_session.add(CatalogSync(id=uuid.uuid4(), shop_id=shop.id, kind="initial", status="running",
                               bulk_operation_id="op1"))
    await db_session.commit()
    running = AsyncMock(return_value=gql_response({"node": {"id": "op1", "status": "RUNNING"}}))
    sync = await cs.finish_bulk_sync(db_session, shop, "tok", "op1", FakeEmbedder(), FakeChat(), gql=running)
    assert sync.status == "running"

    failed = AsyncMock(return_value=gql_response({"node": {"id": "op1", "status": "FAILED",
                                                            "errorCode": "INTERNAL_SERVER_ERROR"}}))
    sync = await cs.finish_bulk_sync(db_session, shop, "tok", "op1", FakeEmbedder(), FakeChat(), gql=failed)
    assert sync.status == "failed" and "INTERNAL_SERVER_ERROR" in sync.error


@pytest.mark.asyncio
async def test_finish_bulk_with_no_products_completes(db_session):
    shop = await add_shop(db_session)
    db_session.add(CatalogSync(id=uuid.uuid4(), shop_id=shop.id, kind="initial", status="running",
                               bulk_operation_id="op1"))
    await db_session.commit()
    gql = AsyncMock(return_value=gql_response({"node": {"id": "op1", "status": "COMPLETED", "url": None}}))
    sync = await cs.finish_bulk_sync(db_session, shop, "tok", "op1", FakeEmbedder(), FakeChat(), gql=gql)
    assert sync.status == "done" and sync.total == 0


# ── single product ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_sync_product_upserts_and_removes(db_session):
    shop = await add_shop(db_session)
    gql = AsyncMock(return_value=gql_response({"product": node(5)}))
    assert await cs.sync_product(db_session, shop, "tok", "5", FakeEmbedder(), FakeChat(), gql=gql) == "upserted"
    assert (await rows(db_session, shop))["5"].embedding is not None

    drafted = AsyncMock(return_value=gql_response({"product": node(5, status="DRAFT")}))
    assert await cs.sync_product(db_session, shop, "tok", "5", FakeEmbedder(), FakeChat(), gql=drafted) == "removed"
    assert await rows(db_session, shop) == {}


@pytest.mark.asyncio
async def test_finish_bulk_imports_only_once_when_called_twice(db_session):
    shop = await add_shop(db_session)
    db_session.add(CatalogSync(id=uuid.uuid4(), shop_id=shop.id, kind="initial", status="running",
                               bulk_operation_id="op1"))
    await db_session.commit()
    gql = AsyncMock(return_value=gql_response({"node": {"id": "op1", "status": "COMPLETED", "url": "https://x"}}))
    fetch = AsyncMock(return_value=json.dumps(node(1)) + "\n")
    chat = FakeChat()
    for _ in range(2):
        await cs.finish_bulk_sync(db_session, shop, "tok", "op1", FakeEmbedder(), chat, gql=gql, fetch_text=fetch)
    assert fetch.await_count == 1 and chat.calls == 1


@pytest.mark.asyncio
async def test_an_importing_sync_blocks_a_new_export(db_session):
    shop = await add_shop(db_session)
    db_session.add(CatalogSync(id=uuid.uuid4(), shop_id=shop.id, kind="initial", status="importing",
                               bulk_operation_id="op1"))
    await db_session.commit()
    gql = AsyncMock()
    await cs.start_bulk_sync(db_session, shop, "tok", "reconcile", gql=gql)
    gql.assert_not_awaited()


@pytest.mark.asyncio
async def test_new_catalog_model_rereads_gradually_while_old_profiles_keep_serving(db_session, monkeypatch):
    from app import ai_models
    shop = await add_shop(db_session)
    await cs.upsert_products(db_session, shop, [cs.parse_product(node(i)) for i in range(1, 6)])
    await db_session.commit()
    await cs.analyze_pending(db_session, shop, FakeEmbedder(), FakeChat())

    # Switch the catalog_analysis slot to another model.
    monkeypatch.setattr(ai_models, "SLOTS", {**ai_models.SLOTS, "catalog_analysis": {
        "model": "gpt-6-sol", "next": None, "rollout_pct": 0}})
    assert await cs.count_pending(db_session, shop.id) == 0      # still searchable
    assert await cs.count_upgrades(db_session, shop) == 5

    # A new product is analyzed first; then only `upgrade_limit` upgrades per run.
    await cs.upsert_products(db_session, shop, [cs.parse_product(node(9))])
    await db_session.commit()
    chat = FakeChat()
    assert await cs.analyze_pending(db_session, shop, FakeEmbedder(), chat, upgrade_limit=2) == 3
    assert chat.calls == 3 and await cs.count_upgrades(db_session, shop) == 3
    r = (await rows(db_session, shop))["9"]
    assert r.profile_version.endswith("+gpt-6-sol")
    log = (await db_session.execute(select(UsageLog).order_by(UsageLog.created_at.desc()))).scalars().first()
    assert log.model_used == "gpt-6-sol"


# ── Monthly re-read cap (PLANS[...]["product_rereads_per_month"]) ────────────

@pytest.mark.asyncio
async def test_edited_products_past_the_monthly_cap_keep_their_profile(db_session, monkeypatch):
    shop = await add_shop(db_session, plan_tier="starter")
    monkeypatch.setitem(cs.PLANS["starter"], "product_rereads_per_month", 2)
    await cs.upsert_products(db_session, shop, [cs.parse_product(node(i)) for i in range(1, 5)])
    await db_session.commit()
    await cs.analyze_pending(db_session, shop, FakeEmbedder(), FakeChat())   # first reads: free of the cap

    await cs.upsert_products(db_session, shop, [cs.parse_product(node(i, title=f"New {i}")) for i in range(1, 5)])
    await db_session.commit()
    chat = FakeChat()
    await cs.analyze_pending(db_session, shop, FakeEmbedder(), chat)
    assert chat.calls == 2 and shop.catalog_rereads_used == 2
    assert await cs.count_held(db_session, shop) == 2
    assert await cs.count_pending(db_session, shop.id) == 2   # held ones still count as not current…
    held = [r for r in (await rows(db_session, shop)).values() if r.profile_hash != r.content_hash]
    assert len(held) == 2 and all(r.embedding is not None for r in held)  # …but stay searchable

    # New products are never held.
    await cs.upsert_products(db_session, shop, [cs.parse_product(node(9))])
    await db_session.commit()
    chat = FakeChat()
    await cs.analyze_pending(db_session, shop, FakeEmbedder(), chat)
    assert chat.calls == 1


@pytest.mark.asyncio
async def test_reread_budget_resets_each_billing_cycle(db_session, monkeypatch):
    shop = await add_shop(db_session, plan_tier="starter")
    monkeypatch.setitem(cs.PLANS["starter"], "product_rereads_per_month", 1)
    shop.catalog_rereads_used = 1
    shop.catalog_rereads_cycle_start = shop.billing_cycle_start - timedelta(days=30)   # last cycle
    await db_session.commit()
    assert cs.rereads_remaining(shop) == 1 and shop.catalog_rereads_used == 0
