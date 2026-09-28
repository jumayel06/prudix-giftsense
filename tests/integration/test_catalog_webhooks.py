"""Catalog webhooks (products/*, bulk_operations/finish) and catalog crons."""
import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.workers.catalog import kick_catalog_syncs, reconcile_catalogs
from core.db.models import CatalogProductRow, CatalogSync
from tests.conftest import TEST_SHOP_DOMAIN, make_shop
from tests.integration.test_webhooks import _headers, _make_client


def post(db_session, topic, payload):
    body = json.dumps(payload).encode()
    for client in _make_client(db_session):
        return client.post("/webhooks", content=body, headers=_headers(body, topic))


@pytest.mark.asyncio
@pytest.mark.parametrize("topic", ["products/create", "products/update"])
async def test_product_change_enqueues_a_deduplicated_sync(db_session, job_pool, topic):
    db_session.add(make_shop())
    await db_session.commit()
    assert post(db_session, topic, {"id": 123, "title": "Throw"}).status_code == 200
    assert job_pool.jobs == [("catalog_sync_product", TEST_SHOP_DOMAIN, "123")]


@pytest.mark.asyncio
async def test_product_change_ignored_for_inactive_shop(db_session, job_pool):
    db_session.add(make_shop(plan_status="cancelled"))
    await db_session.commit()
    post(db_session, "products/update", {"id": 123})
    assert job_pool.jobs == []


@pytest.mark.asyncio
async def test_product_delete_removes_row(db_session):
    shop = make_shop()
    db_session.add(shop)
    await db_session.flush()
    db_session.add(CatalogProductRow(id=uuid.uuid4(), shop_id=shop.id, product_id="123", title="Throw",
                                     price_min=20, price_max=20, content_hash="h"))
    await db_session.commit()
    assert post(db_session, "products/delete", {"id": 123}).status_code == 200
    assert (await db_session.execute(select(CatalogProductRow))).scalars().all() == []


@pytest.mark.asyncio
async def test_bulk_finish_enqueues_import(db_session, job_pool):
    db_session.add(make_shop())
    await db_session.commit()
    post(db_session, "bulk_operations/finish",
         {"admin_graphql_api_id": "gid://shopify/BulkOperation/9", "status": "completed"})
    assert job_pool.jobs == [("catalog_finish_bulk", TEST_SHOP_DOMAIN, "gid://shopify/BulkOperation/9")]


def _session(db_session):
    p = patch("app.workers.catalog.AsyncSessionLocal")
    m = p.start()
    m.return_value.__aenter__.return_value = db_session
    m.return_value.__aexit__.return_value = False
    return p


@pytest.mark.asyncio
async def test_kick_starts_first_sync_only_for_active_shops_without_one(db_session):
    new, synced, cancelled = (make_shop(domain="a.myshopify.com"), make_shop(domain="b.myshopify.com"),
                              make_shop(domain="c.myshopify.com", plan_status="cancelled"))
    db_session.add_all([new, synced, cancelled])
    await db_session.flush()
    db_session.add(CatalogSync(id=uuid.uuid4(), shop_id=synced.id, kind="initial", status="done"))
    await db_session.commit()

    p = _session(db_session)
    try:
        with patch("app.workers.catalog.get_valid_access_token", AsyncMock(return_value="tok")), \
             patch("app.workers.catalog.catalog_sync.start_bulk_sync", AsyncMock()) as start:
            await kick_catalog_syncs({})
    finally:
        p.stop()
    assert [c.args[1].shop_domain for c in start.await_args_list] == ["a.myshopify.com"]


@pytest.mark.asyncio
async def test_kick_finishes_running_exports_whose_webhook_was_missed(db_session):
    shop = make_shop()
    db_session.add(shop)
    await db_session.flush()
    db_session.add(CatalogSync(id=uuid.uuid4(), shop_id=shop.id, kind="initial", status="running",
                               bulk_operation_id="op1"))
    await db_session.commit()

    p = _session(db_session)
    try:
        with patch("app.workers.catalog.get_valid_access_token", AsyncMock(return_value="tok")), \
             patch("app.workers.catalog.catalog_sync.finish_bulk_sync", AsyncMock()) as finish:
            await kick_catalog_syncs({})
    finally:
        p.stop()
    assert finish.await_args.args[3] == "op1"


@pytest.mark.asyncio
async def test_reconcile_isolates_shop_failures(db_session):
    db_session.add_all([make_shop(domain="a.myshopify.com"), make_shop(domain="b.myshopify.com")])
    await db_session.commit()
    p = _session(db_session)
    try:
        with patch("app.workers.catalog.get_valid_access_token", AsyncMock(side_effect=[RuntimeError("x"), "tok"])), \
             patch("app.workers.catalog.catalog_sync.start_bulk_sync", AsyncMock()) as start:
            await reconcile_catalogs({})
    finally:
        p.stop()
    assert start.await_count == 1 and start.await_args.args[3] == "reconcile"


@pytest.mark.asyncio
async def test_kick_fails_imports_interrupted_hours_ago(db_session):
    from datetime import datetime, timedelta, timezone
    shop = make_shop()
    db_session.add(shop)
    await db_session.flush()
    stuck = CatalogSync(id=uuid.uuid4(), shop_id=shop.id, kind="initial", status="importing",
                        bulk_operation_id="op1", started_at=datetime.now(timezone.utc) - timedelta(hours=7))
    db_session.add(stuck)
    await db_session.commit()
    p = _session(db_session)
    try:
        with patch("app.workers.catalog.get_valid_access_token", AsyncMock(return_value="tok")):
            await kick_catalog_syncs({})
    finally:
        p.stop()
    await db_session.refresh(stuck)
    assert stuck.status == "failed" and stuck.error == "import interrupted"
