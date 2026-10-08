"""Basic analytics: storefront events beacon, all-orders counter, GET /api/analytics."""
import json
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from core.db.models import GiftEvent, GiftOrder, GiftSession, OrderCountDaily, UsageLog
from core.db.session import get_db
from tests.conftest import TEST_SHOP_DOMAIN, make_shop
from tests.integration.test_storefront import call as proxy_call
from tests.integration.test_webhooks import _headers, _make_client
from tests.unit.test_gift_orders_parse import line, order

SID = str(uuid.uuid4())


async def seeded(db_session, **kw):
    shop = make_shop(**kw)
    db_session.add(shop)
    await db_session.commit()
    return shop


# ── Events beacon ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_events_are_stored(db_session):
    shop = await seeded(db_session)
    resp = proxy_call(db_session, "POST", "/api/storefront/events", json={"sid": SID, "events": [
        {"type": "widget_open"}, {"type": "pick_click", "product_id": "7"}, {"type": "pick_atc", "product_id": "7"}]})
    assert resp.status_code == 200 and resp.json() == {"stored": 3}
    rows = (await db_session.execute(select(GiftEvent).where(GiftEvent.shop_id == shop.id))).scalars().all()
    assert sorted(r.type for r in rows) == ["pick_atc", "pick_click", "widget_open"]
    assert {r.product_id for r in rows} == {None, "7"} and all(str(r.sid) == SID for r in rows)


@pytest.mark.asyncio
async def test_unknown_event_types_are_dropped(db_session):
    await seeded(db_session)
    resp = proxy_call(db_session, "POST", "/api/storefront/events",
                      json={"sid": SID, "events": [{"type": "widget_open"}, {"type": "hack_the_planet"}]})
    assert resp.json() == {"stored": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{"sid": "nope", "events": []},
                                  {"sid": SID, "events": [{"type": "widget_open"}] * 21},
                                  {"sid": SID, "events": [{"type": "pick_click", "product_id": "x" * 41}]}])
async def test_bad_batches_are_rejected(db_session, body):
    await seeded(db_session)
    assert proxy_call(db_session, "POST", "/api/storefront/events", json=body).status_code == 422


@pytest.mark.asyncio
async def test_events_need_a_signed_request(db_session):
    await seeded(db_session)
    resp = proxy_call(db_session, "POST", "/api/storefront/events", params={"shop": TEST_SHOP_DOMAIN},
                      json={"sid": SID, "events": [{"type": "widget_open"}]})
    assert resp.status_code == 401


# ── All-orders counter ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_every_order_is_counted_per_day(db_session):
    shop = await seeded(db_session)
    for i, payload in enumerate([order([line()], id=1), order([line(props={"_giftsense_gift": "1"})], id=2)]):
        body = json.dumps(payload).encode()
        for client in _make_client(db_session):
            client.post("/webhooks", content=body, headers=_headers(body, "orders/create", webhook_id=f"w{i}"))
    row = (await db_session.execute(select(OrderCountDaily).where(OrderCountDaily.shop_id == shop.id))).scalar_one()
    assert row.orders == 2 and float(row.revenue) == 240.0


# ── Dashboard analytics ──────────────────────────────────────────────────────

def dashboard_get(db_session, path="/api/analytics", **params):
    async def override_db():
        yield db_session
    app.dependency_overrides[get_db] = override_db
    try:
        return TestClient(app).get(path, params={"shop": TEST_SHOP_DOMAIN, **params})
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_analytics_summary(db_session):
    shop = await seeded(db_session)
    sids = [uuid.uuid4() for _ in range(4)]
    now = datetime.now(timezone.utc)
    for s in sids:
        db_session.add(GiftEvent(shop_id=shop.id, sid=s, type="widget_open"))
    db_session.add(GiftEvent(shop_id=shop.id, sid=sids[0], type="widget_open"))        # same session twice
    for s, occasion in ((sids[0], "birthday"), (sids[1], "birthday"), (sids[2], "anniversary")):
        db_session.add(GiftSession(shop_id=shop.id, sid=s, searches=1, last_picks=[],
                                   intake={"occasion": occasion, "budget_band": "25_50", "recipient": "parent"}))
    db_session.add_all([
        GiftOrder(shop_id=shop.id, order_id="1", order_name="#1", sid=sids[0], gift_lines=1, gift_revenue=40,
                  order_total=55, currency="USD", groups=[], note_source="ai_accepted"),
        GiftOrder(shop_id=shop.id, order_id="2", order_name="#2", sid=None, gift_lines=1, gift_revenue=30,
                  order_total=30, currency="USD", groups=[], note_source="manual"),
        GiftOrder(shop_id=shop.id, order_id="3", order_name="#3", sid=sids[1], gift_lines=2, gift_revenue=80,
                  order_total=80, currency="USD", groups=[]),
        OrderCountDaily(shop_id=shop.id, day=date.today(), orders=10, revenue=900),
        # outside the 30-day window
        GiftOrder(shop_id=shop.id, order_id="9", order_name="#9", sid=None, gift_lines=1, gift_revenue=999,
                  order_total=999, currency="USD", groups=[], created_at=now - timedelta(days=45)),
    ])
    await db_session.commit()

    data = dashboard_get(db_session).json()
    assert data["days"] == 30 and data["currency"] == "USD"
    assert data["sessions"] == 4 and data["searched_sessions"] == 3
    assert data["completion_rate"] == pytest.approx(0.75)
    assert data["gift_orders"] == 3 and data["attributed_orders"] == 2 and data["all_orders"] == 10
    assert data["conversion_rate"] == pytest.approx(2 / 3)
    assert data["attributed_revenue"] == pytest.approx(135.0) and data["gift_revenue"] == pytest.approx(150.0)
    assert data["note_attach_rate"] == pytest.approx(2 / 3)
    assert data["note_acceptance_rate"] == pytest.approx(0.5)
    assert data["top_occasions"][0] == {"value": "birthday", "label": "Birthday", "count": 2}
    assert data["top_budgets"][0]["label"] == "$25–50"
    assert len(data["daily"]) == 30 and data["daily"][-1]["gift_orders"] == 3


@pytest.mark.asyncio
async def test_empty_shop_has_zeros_not_errors(db_session):
    await seeded(db_session)
    data = dashboard_get(db_session).json()
    assert data["sessions"] == 0 and data["completion_rate"] is None and data["conversion_rate"] is None


# ── Generation history (GET /api/analytics/generations) ─────────────────────

@pytest.mark.asyncio
async def test_generation_log_pairs_refunds_and_drops_completions(db_session):
    shop = await seeded(db_session)
    now = datetime.now(timezone.utc)
    db_session.add_all([
        # Charged + its completion supplement (0 gens) — completion is dropped.
        UsageLog(shop_id=shop.id, action_type="gift_search", generations_consumed=1,
                 model_used="gpt-6-luna", created_at=now),
        UsageLog(shop_id=shop.id, action_type="gift_search", generations_consumed=0,
                 model_used="gpt-6-luna", created_at=now),
        # Charged, then refunded (failure) — shows as "Refunded".
        UsageLog(shop_id=shop.id, action_type="gift_note", generations_consumed=1,
                 model_used="gpt-6-sol", created_at=now - timedelta(minutes=2)),
        UsageLog(shop_id=shop.id, action_type="gift_note", generations_consumed=-1,
                 model_used="gpt-6-sol", created_at=now - timedelta(minutes=1)),
    ])
    await db_session.commit()

    data = dashboard_get(db_session, "/api/analytics/generations").json()
    assert data["total"] == 2
    gens = data["generations"]
    assert [g["feature"] for g in gens] == ["Gift finder searches", "AI gift notes"]
    assert [g["status"] for g in gens] == ["Completed", "Refunded"]
    assert gens[0]["model"] == "GPT-6 Luna" and gens[1]["model"] == "GPT-6 Sol"
    assert set(data["facets"]["features"]) == {"Gift finder searches", "AI gift notes"}
    assert {m["value"] for m in data["facets"]["models"]} == {"gpt-6-luna", "gpt-6-sol"}


@pytest.mark.asyncio
async def test_generation_log_filters_by_feature_and_model(db_session):
    shop = await seeded(db_session)
    now = datetime.now(timezone.utc)
    db_session.add_all([
        UsageLog(shop_id=shop.id, action_type="gift_search", generations_consumed=1, model_used="gpt-6-luna", created_at=now),
        UsageLog(shop_id=shop.id, action_type="playground", generations_consumed=1, model_used="gpt-6-sol", created_at=now),
    ])
    await db_session.commit()

    by_feature = dashboard_get(db_session, "/api/analytics/generations", feature="Try it (dashboard)").json()
    assert by_feature["total"] == 1 and by_feature["generations"][0]["feature"] == "Try it (dashboard)"

    by_model = dashboard_get(db_session, "/api/analytics/generations", model="gpt-6-luna").json()
    assert by_model["total"] == 1 and by_model["generations"][0]["model"] == "GPT-6 Luna"


@pytest.mark.asyncio
async def test_generation_log_paginates(db_session):
    shop = await seeded(db_session)
    now = datetime.now(timezone.utc)
    db_session.add_all([
        UsageLog(shop_id=shop.id, action_type="gift_search", generations_consumed=1, model_used="gpt-6-luna",
                 created_at=now - timedelta(minutes=i))
        for i in range(3)
    ])
    await db_session.commit()

    page1 = dashboard_get(db_session, "/api/analytics/generations", limit=2).json()
    assert page1["total"] == 3 and len(page1["generations"]) == 2 and page1["has_more"] is True

    page2 = dashboard_get(db_session, "/api/analytics/generations", limit=2, offset=2).json()
    assert len(page2["generations"]) == 1 and page2["has_more"] is False


@pytest.mark.asyncio
async def test_generation_log_empty_shop(db_session):
    await seeded(db_session)
    data = dashboard_get(db_session, "/api/analytics/generations").json()
    assert data == {"generations": [], "total": 0, "has_more": False, "window_capped": False,
                    "facets": {"features": [], "models": []}}


@pytest.mark.asyncio
async def test_old_events_are_purged_after_90_days(db_session):
    from unittest.mock import patch
    from app.workers.main import purge_old_gift_events
    shop = await seeded(db_session)
    now = datetime.now(timezone.utc)
    db_session.add_all([GiftEvent(shop_id=shop.id, sid=uuid.uuid4(), type="widget_open", created_at=now - timedelta(days=91)),
                        GiftEvent(shop_id=shop.id, sid=uuid.uuid4(), type="widget_open", created_at=now - timedelta(days=5))])
    await db_session.commit()
    with patch("app.workers.main.AsyncSessionLocal") as sess:
        sess.return_value.__aenter__.return_value = db_session
        sess.return_value.__aexit__.return_value = False
        await purge_old_gift_events({})
    assert len((await db_session.execute(select(GiftEvent))).scalars().all()) == 1
