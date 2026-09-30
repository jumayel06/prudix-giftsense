"""Gift wrap end to end: settings save → sync job → storefront config; catalog skips the wrap product."""
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from app.services.catalog_sync import parse_product
from core.db.models import Shop
from tests.conftest import TEST_SHOP_DOMAIN, make_shop
from tests.integration.test_gift_settings import call as settings_call
from tests.integration.test_storefront import call as proxy_call

SYNCED = {"enabled": True, "ready": True, "product_id": "gid://shopify/Product/9", "error": None,
          "styles": [{"name": "Gold", "price": 5.0, "variant_id": "gid://shopify/ProductVariant/1"}]}


@pytest.mark.asyncio
async def test_saving_styles_queues_a_sync(db_session):
    db_session.add(make_shop())
    await db_session.commit()
    with patch("app.routes.settings.enqueue", new=AsyncMock(return_value=True)) as enq:
        resp = settings_call(db_session, "PUT", json={"gift_wrap": {"enabled": True,
                                                                     "styles": [{"name": "Gold", "price": 5}]}})
    assert resp.status_code == 200
    enq.assert_awaited_once_with("sync_wrap", TEST_SHOP_DOMAIN)
    w = settings_call(db_session, "GET").json()["gift_wrap"]
    assert w["enabled"] is True and w["ready"] is False and w["styles"][0]["name"] == "Gold"


@pytest.mark.asyncio
async def test_toggling_off_does_not_sync(db_session):
    shop = make_shop()
    shop.gift_settings = {"wrap": dict(SYNCED)}
    db_session.add(shop)
    await db_session.commit()
    with patch("app.routes.settings.enqueue", new=AsyncMock()) as enq:
        settings_call(db_session, "PUT", json={"gift_wrap": {"enabled": False}})
    enq.assert_not_awaited()
    assert settings_call(db_session, "GET").json()["gift_wrap"]["ready"] is True   # product kept, just not offered


@pytest.mark.asyncio
async def test_bad_styles_are_rejected(db_session):
    db_session.add(make_shop())
    await db_session.commit()
    resp = settings_call(db_session, "PUT", json={"gift_wrap": {"styles": [{"name": "A", "price": 5000}]}})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_config_offers_synced_wrap_styles(db_session):
    shop = make_shop(plan_tier="starter")
    shop.gift_settings = {"wrap": dict(SYNCED)}
    db_session.add(shop)
    await db_session.commit()
    data = proxy_call(db_session, "GET", "/api/storefront/config").json()
    assert data["wrap"] == [{"variant_id": 1, "name": "Gold", "price": 5.0}]


@pytest.mark.asyncio
async def test_config_has_no_wrap_by_default(db_session):
    db_session.add(make_shop())
    await db_session.commit()
    assert proxy_call(db_session, "GET", "/api/storefront/config").json()["wrap"] == []


def test_catalog_skips_the_wrap_product():
    node = {"id": "gid://shopify/Product/9", "status": "UNLISTED", "publishedAt": "2026-09-29T00:00:00Z",
            "tags": ["giftsense-wrap"], "handle": "gift-wrap", "title": "Gift wrap",
            "priceRangeV2": {"minVariantPrice": {"amount": "5"}, "maxVariantPrice": {"amount": "5"}}}
    assert parse_product(node) is None
    assert parse_product({**node, "status": "ACTIVE"}) is None


@pytest.mark.asyncio
async def test_sync_job_stores_variant_ids(db_session):
    from app.workers.orders import sync_wrap
    shop = make_shop()
    shop.gift_settings = {"wrap": {"enabled": True, "styles": [{"name": "Gold", "price": 5.0}]}}
    db_session.add(shop)
    await db_session.commit()
    with patch("app.workers.orders.AsyncSessionLocal") as sess, \
            patch("app.workers.orders.get_valid_access_token", new=AsyncMock(return_value="tok")), \
            patch("app.workers.orders.wrap.sync_wrap_product", new=AsyncMock(return_value=dict(SYNCED))):
        sess.return_value.__aenter__.return_value = db_session
        sess.return_value.__aexit__.return_value = False
        await sync_wrap({}, shop.shop_domain)
    row = (await db_session.execute(select(Shop))).scalar_one()
    assert row.gift_settings["wrap"]["ready"] is True
    assert row.gift_settings["wrap"]["styles"][0]["variant_id"] == "gid://shopify/ProductVariant/1"


@pytest.mark.asyncio
async def test_sync_job_drops_a_stale_result(db_session):
    from app.workers.orders import sync_wrap
    shop = make_shop()
    shop.gift_settings = {"wrap": {"enabled": True, "styles": [{"name": "Gold", "price": 5.0}]}}
    db_session.add(shop)
    await db_session.commit()

    async def slow_sync(domain, token, settings):
        shop.gift_settings = {"wrap": {"enabled": True, "styles": [{"name": "Kraft", "price": 3.0}]}}  # edited meanwhile
        await db_session.commit()
        return dict(SYNCED)

    with patch("app.workers.orders.AsyncSessionLocal") as sess, \
            patch("app.workers.orders.get_valid_access_token", new=AsyncMock(return_value="tok")), \
            patch("app.workers.orders.wrap.sync_wrap_product", new=slow_sync):
        sess.return_value.__aenter__.return_value = db_session
        sess.return_value.__aexit__.return_value = False
        await sync_wrap({}, shop.shop_domain)
    row = (await db_session.execute(select(Shop))).scalar_one()
    assert row.gift_settings["wrap"]["styles"] == [{"name": "Kraft", "price": 3.0}]
