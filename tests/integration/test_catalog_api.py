"""Catalog page API: status/progress, product list, exclude toggle, manual resync."""
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from fastapi.testclient import TestClient

from app.main import app
from app.services.gifting.enrichment import PROMPT_VERSION as ENRICH_VERSION
from core.db.models import CatalogProductRow, CatalogSync
from core.db.session import get_db
from tests.conftest import TEST_SHOP_DOMAIN, make_shop


def call(db_session, method, path, **kw):
    async def override_db():
        yield db_session
    app.dependency_overrides[get_db] = override_db
    try:
        sep = "&" if "?" in path else "?"
        return TestClient(app).request(method, f"{path}{sep}shop={TEST_SHOP_DOMAIN}", **kw)
    finally:
        app.dependency_overrides.clear()


def product(shop, pid, *, analyzed=True, title=None, **kw):
    return CatalogProductRow(
        id=uuid.uuid4(), shop_id=shop.id, product_id=pid, title=title or f"Throw {pid}",
        price_min=30, price_max=30, content_hash="h",
        profile_hash="h" if analyzed else None, profile_version=ENRICH_VERSION if analyzed else None,
        gift_profile={"giftable": 0.9, "recipients": ["friend"], "occasions": ["birthday"], "vibes": ["cozy"],
                      "interests": [], "age_band": "adult", "gift_pitch": "A soft throw.", "facts": []}
        if analyzed else None,
        embedding=[0.1] * 3 if analyzed else None, **kw,
    )


async def seed(db_session, **shop_kw):
    shop = make_shop(**shop_kw)
    db_session.add(shop)
    await db_session.flush()
    return shop


@pytest.mark.asyncio
async def test_status_reports_counts_limit_and_latest_sync(db_session):
    shop = await seed(db_session, plan_tier="growth", plan_status="trial_active")
    db_session.add_all([product(shop, "1"), product(shop, "2", analyzed=False), product(shop, "3", excluded=True)])
    db_session.add(CatalogSync(id=uuid.uuid4(), shop_id=shop.id, kind="initial", status="importing",
                               bulk_operation_id="op", total=3, enriched=1))
    await db_session.commit()

    data = call(db_session, "GET", "/api/catalog/status").json()
    assert data["products"] == 3 and data["analyzed"] == 2 and data["pending"] == 1 and data["excluded"] == 1
    assert data["limit"] == 100 and data["is_trial"] is True
    assert data["sync"]["status"] == "importing" and data["sync"]["total"] == 3 and data["sync"]["enriched"] == 1


@pytest.mark.asyncio
async def test_status_with_no_sync_yet(db_session):
    await seed(db_session)
    await db_session.commit()
    data = call(db_session, "GET", "/api/catalog/status").json()
    assert data["sync"] is None and data["products"] == 0


@pytest.mark.asyncio
async def test_products_list_search_and_paging(db_session):
    shop = await seed(db_session)
    db_session.add_all([product(shop, str(i), title=f"Candle {i}") for i in range(30)]
                       + [product(shop, "mug", title="Coffee Mug")])
    await db_session.commit()

    page = call(db_session, "GET", "/api/catalog/products?page=2").json()
    assert page["total"] == 31 and len(page["products"]) == 31 - page["page_size"]
    found = call(db_session, "GET", "/api/catalog/products?q=mug").json()
    assert [p["product_id"] for p in found["products"]] == ["mug"]
    p = found["products"][0]
    assert p["analyzed"] is True and p["profile"]["vibes"] == ["cozy"] and p["excluded"] is False


@pytest.mark.asyncio
async def test_exclude_toggle(db_session):
    shop = await seed(db_session)
    db_session.add(product(shop, "1"))
    await db_session.commit()
    resp = call(db_session, "PATCH", "/api/catalog/products/1", json={"excluded": True})
    assert resp.status_code == 200 and resp.json()["excluded"] is True
    assert call(db_session, "PATCH", "/api/catalog/products/404", json={"excluded": True}).status_code == 404


@pytest.mark.asyncio
async def test_other_shops_products_are_invisible(db_session):
    other = await seed(db_session, domain="other.myshopify.com")
    await seed(db_session)
    db_session.add(product(other, "1"))
    await db_session.commit()
    assert call(db_session, "GET", "/api/catalog/products").json()["total"] == 0
    assert call(db_session, "PATCH", "/api/catalog/products/1", json={"excluded": True}).status_code == 404


@pytest.mark.asyncio
async def test_resync_enqueues_unless_a_sync_is_running(db_session, job_pool):
    shop = await seed(db_session)
    await db_session.commit()
    assert call(db_session, "POST", "/api/catalog/resync").status_code == 202
    assert job_pool.jobs == [("catalog_start_sync", str(shop.id), "manual")]

    db_session.add(CatalogSync(id=uuid.uuid4(), shop_id=shop.id, kind="manual", status="running",
                               started_at=datetime.now(timezone.utc)))
    await db_session.commit()
    assert call(db_session, "POST", "/api/catalog/resync").status_code == 409


@pytest.mark.asyncio
async def test_inactive_shop_cannot_resync(db_session, job_pool):
    await seed(db_session, plan_status="cancelled")
    await db_session.commit()
    assert call(db_session, "POST", "/api/catalog/resync").status_code == 403
    assert job_pool.jobs == []


# ── Sync now: queued state, cooldown, queue failure ──────────────────────────

@pytest.mark.asyncio
async def test_resync_records_a_queued_sync_the_page_can_show(db_session, job_pool):
    shop = await seed(db_session)
    await db_session.commit()
    assert call(db_session, "POST", "/api/catalog/resync").status_code == 202
    data = call(db_session, "GET", "/api/catalog/status").json()
    assert data["sync"]["status"] == "queued" and data["sync"]["kind"] == "manual"
    # Second click while queued: refused, nothing enqueued twice.
    assert call(db_session, "POST", "/api/catalog/resync").status_code == 409
    assert len(job_pool.jobs) == 1


@pytest.mark.asyncio
async def test_resync_cooldown(db_session, job_pool):
    shop = await seed(db_session)
    db_session.add(CatalogSync(id=uuid.uuid4(), shop_id=shop.id, kind="manual", status="done",
                               started_at=datetime.now(timezone.utc) - timedelta(minutes=5)))
    await db_session.commit()
    resp = call(db_session, "POST", "/api/catalog/resync")
    assert resp.status_code == 429 and "minute" in resp.json()["detail"]
    assert call(db_session, "GET", "/api/catalog/status").json()["next_manual_sync_at"]


@pytest.mark.asyncio
async def test_resync_reports_queue_down(db_session, monkeypatch):
    await seed(db_session)
    await db_session.commit()
    monkeypatch.setattr("app.routes.catalog.enqueue", AsyncMock(return_value=False))
    assert call(db_session, "POST", "/api/catalog/resync").status_code == 503
    assert call(db_session, "GET", "/api/catalog/status").json()["sync"]["status"] == "failed"


@pytest.mark.asyncio
async def test_status_shows_reread_budget(db_session):
    shop = await seed(db_session, plan_tier="growth")
    shop.catalog_rereads_used = 40
    shop.catalog_rereads_cycle_start = shop.billing_cycle_start
    await db_session.commit()
    data = call(db_session, "GET", "/api/catalog/status").json()
    assert data["rereads_used"] == 40 and data["rereads_limit"] == 500 and data["held"] == 0


@pytest.mark.asyncio
async def test_long_queued_sync_is_flagged_slow(db_session):
    shop = await seed(db_session)
    db_session.add(CatalogSync(id=uuid.uuid4(), shop_id=shop.id, kind="manual", status="queued",
                               started_at=datetime.now(timezone.utc) - timedelta(minutes=5)))
    await db_session.commit()
    assert call(db_session, "GET", "/api/catalog/status").json()["sync"]["slow_start"] is True


# ── Merchant edits to a product's gift profile ───────────────────────────────

@pytest.fixture
def embedder():
    from app.services.gifting.embeddings import FakeEmbedder
    from unittest.mock import patch as _patch
    with _patch("app.routes.catalog.OpenAIEmbedder", return_value=FakeEmbedder()):
        yield


@pytest.mark.asyncio
async def test_merchant_overrides_change_the_profile_and_re_embed(db_session, embedder):
    shop = await seed(db_session)
    db_session.add(product(shop, "1"))
    await db_session.commit()
    before = (await db_session.execute(select(CatalogProductRow))).scalar_one().embedding

    resp = call(db_session, "PATCH", "/api/catalog/products/1", json={"overrides": {
        "recipients": ["parent", "grandparent"], "vibes": ["sentimental"], "gift_pitch": "A keepsake for Mom."}})
    assert resp.status_code == 200
    data = resp.json()
    assert data["overridden"] is True
    assert data["profile"]["recipients"] == ["parent", "grandparent"] and data["profile"]["vibes"] == ["sentimental"]
    assert data["profile"]["occasions"] == ["birthday"]          # untouched fields keep the AI's value
    assert data["profile"]["gift_pitch"] == "A keepsake for Mom."
    assert data["ai_profile"]["vibes"] == ["cozy"]                # shown for "reset"

    row = (await db_session.execute(select(CatalogProductRow))).scalar_one()
    await db_session.refresh(row)
    assert row.merchant_overrides["vibes"] == ["sentimental"] and row.embedding != before


@pytest.mark.asyncio
async def test_reset_overrides(db_session, embedder):
    shop = await seed(db_session)
    db_session.add(product(shop, "1", merchant_overrides={"vibes": ["funny"]}))
    await db_session.commit()
    data = call(db_session, "PATCH", "/api/catalog/products/1", json={"overrides": None}).json()
    assert data["overridden"] is False and data["profile"]["vibes"] == ["cozy"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [{"vibes": ["wild"]}, {"recipients": ["alien"]}, {"occasions": ["x"]},
                                 {"age_band": "ancient"}, {"gift_pitch": "x" * 201}, {"price": 1}])
async def test_overrides_are_validated(db_session, embedder, bad):
    shop = await seed(db_session)
    db_session.add(product(shop, "1"))
    await db_session.commit()
    assert call(db_session, "PATCH", "/api/catalog/products/1", json={"overrides": bad}).status_code == 422


@pytest.mark.asyncio
async def test_exclude_still_works_alone(db_session, embedder):
    shop = await seed(db_session)
    db_session.add(product(shop, "1", merchant_overrides={"vibes": ["funny"]}))
    await db_session.commit()
    data = call(db_session, "PATCH", "/api/catalog/products/1", json={"excluded": True}).json()
    assert data["excluded"] is True and data["overridden"] is True   # overrides untouched
