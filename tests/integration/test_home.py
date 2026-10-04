"""GET /api/home: the dashboard Home page's one-call summary."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from core.db.models import CatalogProductRow, GiftEvent, GiftOrder
from tests.conftest import make_shop
from tests.integration.test_media import dashboard_get


@pytest.mark.asyncio
async def test_home_summary(db_session):
    shop = make_shop(plan_tier="growth")
    shop.gift_settings = {"theme_check": {"embed": "on", "theme_name": "Dawn"},
                          "wrap": {"enabled": True, "ready": True, "product_id": "p",
                                   "styles": [{"name": "Gold", "price": 5.0, "variant_id": "v"}]}}
    db_session.add(shop)
    await db_session.flush()
    now = datetime.now(timezone.utc)
    db_session.add_all([
        CatalogProductRow(shop_id=shop.id, product_id="1", title="A", content_hash="h"),
        CatalogProductRow(shop_id=shop.id, product_id="2", title="B", content_hash="h", excluded=True),
        GiftEvent(shop_id=shop.id, sid=uuid.uuid4(), type="widget_open"),
        GiftOrder(shop_id=shop.id, order_id="9", order_name="#1009", sid=uuid.uuid4(), gift_lines=1, gift_revenue=40,
                  order_total=40, currency="USD", groups=[{"id": "g1", "label": "Mom", "wrap": "Gold"}]),
        GiftOrder(shop_id=shop.id, order_id="8", order_name="#1008", sid=None, gift_lines=1, gift_revenue=10,
                  order_total=10, currency="USD", groups=[], created_at=now - timedelta(days=40)),
    ])
    await db_session.commit()

    d = dashboard_get(db_session, "/api/home").json()
    s = d["summary"]
    assert s["gift_orders"] == 1 and s["sessions"] == 1 and s["attributed_revenue"] == 40
    assert s["changes"]["gift_orders"] == 0.0 and len(s["daily"]) == 14       # 1 vs 1 in the 30 days before
    assert [o["order_name"] for o in d["recent_orders"]] == ["#1009", "#1008"]
    assert d["recent_orders"][0]["recipients"] == ["Mom"] and d["recent_orders"][0]["has_wrap"] is True
    assert d["catalog"] == {"products": 2, "analyzed": 0, "excluded": 1, "sync_status": None, "synced_at": None}
    assert d["storefront"] == {"embed": "on", "theme_name": "Dawn", "warning": False}
    f = d["features"]
    assert f["wrap"] == {"available": True, "on": True, "detail": "1 style"}
    assert f["arrive_by"]["available"] is True and f["arrive_by"]["on"] is False
    assert f["registries"]["available"] is False                              # Pro only
    assert d["store"] == shop.shop_domain.removesuffix(".myshopify.com")
