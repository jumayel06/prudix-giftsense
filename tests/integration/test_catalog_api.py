"""Catalog page API: status/progress, product list, exclude toggle, manual resync."""
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app
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
        profile_hash="h" if analyzed else None, profile_version="enrich-v1" if analyzed else None,
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
    assert data["limit"] == 250 and data["is_trial"] is True
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
