"""GET /api/gift-orders: the dashboard's Gift orders list."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from core.db.models import GiftOrder
from tests.conftest import make_shop
from fastapi.testclient import TestClient

from app.main import app
from core.db.session import get_db
from tests.conftest import TEST_SHOP_DOMAIN


def dashboard_get(db_session, path, **params):
    async def override_db():
        yield db_session
    app.dependency_overrides[get_db] = override_db
    try:
        return TestClient(app).get(path, params={"shop": TEST_SHOP_DOMAIN, **params})
    finally:
        app.dependency_overrides.clear()


def gift_order(shop, n, **kw):
    base = dict(shop_id=shop.id, order_id=str(n), order_name=f"#{1000 + n}", gift_lines=1, gift_revenue=30,
                order_total=40, currency="USD", groups=[],
                created_at=datetime.now(timezone.utc) - timedelta(minutes=n))
    return GiftOrder(**{**base, **kw})


@pytest.mark.asyncio
async def test_lists_gift_orders_newest_first(db_session):
    shop, other = make_shop(), make_shop(domain="other.myshopify.com")
    db_session.add_all([shop, other])
    await db_session.flush()
    db_session.add_all([
        gift_order(shop, 1, delivery_mode="self", sid=uuid.uuid4(), note_source="ai_accepted", annotated=True,
                   groups=[{"id": "g1", "label": "Mom", "wrap": "Gold"}, {"id": "g2", "label": "Dad", "wrap": "Gold"},
                           {"id": "g3", "label": "Sam", "wrap": None}]),
        gift_order(shop, 2, delivery_mode="direct", groups=[{"id": "order", "label": None, "wrap": "Kraft"}]),
        gift_order(other, 3),
    ])
    await db_session.commit()

    data = dashboard_get(db_session, "/api/gift-orders").json()
    assert data["total"] == 2 and data["has_next"] is False
    first, second = data["orders"]
    assert first["order_name"] == "#1001" and first["recipients"] == ["Mom", "Dad", "Sam"]
    assert first["wraps"] == ["Gold"] and first["has_note"] and first["from_finder"] and first["annotated"]
    assert second["recipients"] == [] and second["wraps"] == ["Kraft"] and second["has_note"] is False
    assert "note" not in first and "customer" not in first      # order facts only


@pytest.mark.asyncio
async def test_pages_of_25(db_session):
    shop = make_shop()
    db_session.add(shop)
    await db_session.flush()
    db_session.add_all([gift_order(shop, i) for i in range(30)])
    await db_session.commit()
    p1 = dashboard_get(db_session, "/api/gift-orders").json()
    assert len(p1["orders"]) == 25 and p1["has_next"] is True
    p2 = dashboard_get(db_session, "/api/gift-orders", page=2).json()
    assert len(p2["orders"]) == 5 and p2["has_next"] is False


@pytest.mark.asyncio
async def test_empty_list(db_session):
    db_session.add(make_shop())
    await db_session.commit()
    assert dashboard_get(db_session, "/api/gift-orders").json() == {
        "orders": [], "page": 1, "page_size": 25, "total": 0, "has_next": False}
