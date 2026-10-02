"""Arrive-by end to end: order webhook → ship-by date, order job → tags + hold,
hourly cron → release (app/services/holds.py, app/workers/orders.py)."""
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from app.services import holds
from core.db.models import GiftOrder
from tests.conftest import TEST_SHOP_DOMAIN, make_shop
from tests.integration.test_gift_order_webhook import post
from tests.unit.test_gift_orders_parse import line, order

FO = "gid://shopify/FulfillmentOrder/77"


@pytest.fixture(autouse=True)
def noon_today(monkeypatch):
    """Pin "now" to 12:00 UTC today so the store's daily cutoff never makes
    these tests depend on the hour they run."""
    from app.services import delivery
    noon = datetime.now(timezone.utc).replace(hour=12, minute=0, second=0, microsecond=0)
    real = delivery.store_today
    monkeypatch.setattr(delivery, "store_today", lambda tz, now=None: real(tz, now or noon))


def resp(data):
    r = MagicMock(status_code=200)
    r.json.return_value = {"data": data}
    return r


def fulfillment_orders(*nodes):
    return resp({"order": {"fulfillmentOrders": {"nodes": list(nodes)}}})


def fo(fid=FO, actions=("HOLD",), held_by=()):
    return {"id": fid, "status": "OPEN", "supportedActions": [{"action": a} for a in actions],
            "fulfillmentHolds": [{"id": f"gid://shopify/FulfillmentHold/{i}", "handle": h} for i, h in enumerate(held_by)]}


def delivery_shop(**kw):
    shop = make_shop(**kw)
    shop.gift_settings = {"delivery": {"enabled": True, "processing_days": 1, "transit_days": 3,
                                       "ship_weekdays": [0, 1, 2, 3, 4, 5, 6], "blackout_dates": [],
                                       "max_days_ahead": 60, "cutoff_hour": 23}}
    return shop


def in_days(n):
    return (datetime.now(timezone.utc).date() + timedelta(days=n)).isoformat()


# ── holds service ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_hold_only_what_the_merchant_ships():
    gql = AsyncMock(side_effect=[
        fulfillment_orders(fo(), fo("gid://shopify/FulfillmentOrder/88", actions=())),   # second: a 3PL
        resp({"fulfillmentOrderHold": {"fulfillmentHold": {"id": "h1"}, "userErrors": []}}),
    ])
    assert await holds.hold_order("s", "t", "gid://shopify/Order/1", "2026-10-16", "2026-10-20", gql=gql) == "held"
    hold_call = gql.await_args_list[1].args[3]
    assert hold_call["id"] == FO and hold_call["hold"]["handle"] == holds.HOLD_HANDLE
    assert "ship on Oct 16 to arrive by Oct 20" in hold_call["hold"]["reasonNotes"]
    assert gql.await_count == 2                                   # the 3PL one isn't touched


@pytest.mark.asyncio
async def test_nothing_holdable_is_reported():
    gql = AsyncMock(return_value=fulfillment_orders(fo(actions=())))
    assert await holds.hold_order("s", "t", "gid://shopify/Order/1", "2026-10-16", "2026-10-20", gql=gql) == "none"


@pytest.mark.asyncio
async def test_release_only_touches_our_holds():
    gql = AsyncMock(side_effect=[
        fulfillment_orders(fo(held_by=("merchant-hold", holds.HOLD_HANDLE))),
        resp({"fulfillmentOrderReleaseHold": {"fulfillmentOrder": {"id": FO}, "userErrors": []}}),
    ])
    assert await holds.release_order("s", "t", "gid://shopify/Order/1", gql=gql) == "released"
    assert gql.await_args_list[1].args[3]["holdIds"] == ["gid://shopify/FulfillmentHold/1"]


# ── order webhook ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_order_with_a_date_gets_a_ship_by_date(db_session, job_pool):
    db_session.add(delivery_shop(plan_tier="growth"))
    await db_session.commit()
    post(db_session, "orders/create", order([line(props={"_giftsense_gift": "order"})],
                                            attrs={"Gift note": "Happy birthday", "Arrive by": in_days(10)}))
    row = (await db_session.execute(select(GiftOrder))).scalar_one()
    assert row.arrive_by.isoformat() == in_days(10) and row.ship_by.isoformat() == in_days(7)
    assert row.hold_status == "pending"
    value = job_pool.jobs[0][3]
    assert value["arrive_by"] == in_days(10) and value["ship_by"] == in_days(7)


@pytest.mark.asyncio
async def test_starter_plan_ignores_the_date(db_session, job_pool):
    db_session.add(delivery_shop(plan_tier="starter"))
    await db_session.commit()
    post(db_session, "orders/create", order([line(props={"_giftsense_gift": "order"})],
                                            attrs={"Arrive by": in_days(10)}))
    row = (await db_session.execute(select(GiftOrder))).scalar_one()
    assert row.ship_by is None and row.hold_status is None


@pytest.mark.asyncio
async def test_a_date_too_soon_ships_asap_without_a_hold(db_session, job_pool):
    db_session.add(delivery_shop())
    await db_session.commit()
    post(db_session, "orders/create", order([line(props={"_giftsense_gift": "order"})],
                                            attrs={"Arrive by": in_days(1)}))
    assert (await db_session.execute(select(GiftOrder))).scalar_one().hold_status == "late"


# ── order job and hourly release ─────────────────────────────────────────────

async def run_annotate(db_session, gql):
    from app.workers.orders import annotate_gift_order
    with patch("app.workers.orders.AsyncSessionLocal") as sess, \
            patch("app.workers.orders.get_valid_access_token", AsyncMock(return_value="tok")), \
            patch("app.services.gift_orders.shopify_graphql_post", AsyncMock(return_value=resp({}))), \
            patch("app.workers.orders.shopify_graphql_post", gql), \
            patch("app.workers.orders.holds.hold_order", AsyncMock(return_value="held")) as hold:
        sess.return_value.__aenter__.return_value = db_session
        sess.return_value.__aexit__.return_value = False
        await annotate_gift_order({}, TEST_SHOP_DOMAIN, "gid://shopify/Order/5551", {"version": 1, "groups": []})
    return hold


@pytest.mark.asyncio
async def test_job_tags_and_holds_a_scheduled_order(db_session):
    shop = delivery_shop()
    db_session.add(shop)
    await db_session.flush()
    db_session.add(GiftOrder(shop_id=shop.id, order_id="5551", order_name="#1", gift_lines=1, gift_revenue=30,
                             order_total=30, currency="USD", groups=[], arrive_by=date.fromisoformat(in_days(10)),
                             ship_by=date.fromisoformat(in_days(7)), hold_status="pending"))
    await db_session.commit()
    tag_gql = AsyncMock(return_value=resp({}))
    hold = await run_annotate(db_session, tag_gql)
    assert tag_gql.await_args.args[3]["tags"] == [f"giftsense-ship-by-{in_days(7)}", "giftsense-scheduled"]
    hold.assert_awaited_once()
    assert (await db_session.execute(select(GiftOrder))).scalar_one().hold_status == "held"


@pytest.mark.asyncio
async def test_job_never_holds_a_late_order(db_session):
    shop = delivery_shop()
    db_session.add(shop)
    await db_session.flush()
    db_session.add(GiftOrder(shop_id=shop.id, order_id="5551", order_name="#1", gift_lines=1, gift_revenue=30,
                             order_total=30, currency="USD", groups=[], arrive_by=date.fromisoformat(in_days(1)),
                             ship_by=date.fromisoformat(in_days(1)), hold_status="late"))
    await db_session.commit()
    tag_gql = AsyncMock(return_value=resp({}))
    hold = await run_annotate(db_session, tag_gql)
    assert "giftsense-ship-asap" in tag_gql.await_args.args[3]["tags"]
    hold.assert_not_awaited()


@pytest.mark.asyncio
async def test_hourly_release_frees_holds_on_their_ship_by_day(db_session):
    from app.workers.orders import release_due_holds
    shop = delivery_shop()
    db_session.add(shop)
    await db_session.flush()
    today, later = date.fromisoformat(in_days(0)), date.fromisoformat(in_days(3))
    for oid, ship in (("1", today), ("2", later)):
        db_session.add(GiftOrder(shop_id=shop.id, order_id=oid, order_name=f"#{oid}", gift_lines=1, gift_revenue=1,
                                 order_total=1, currency="USD", groups=[], arrive_by=ship + timedelta(days=3),
                                 ship_by=ship, hold_status="held"))
    await db_session.commit()
    with patch("app.workers.orders.AsyncSessionLocal") as sess, \
            patch("app.workers.orders.get_valid_access_token", AsyncMock(return_value="tok")), \
            patch("app.workers.orders.holds.release_order", AsyncMock(return_value="released")) as release:
        sess.return_value.__aenter__.return_value = db_session
        sess.return_value.__aexit__.return_value = False
        await release_due_holds({})
    assert release.await_args.args[2] == "gid://shopify/Order/1" and release.await_count == 1
    by_id = {r.order_id: r.hold_status for r in (await db_session.execute(select(GiftOrder))).scalars()}
    assert by_id == {"1": "released", "2": "held"}


# ── settings and widget config ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_only_growth_and_pro_can_turn_dates_on(db_session):
    from tests.integration.test_gift_settings import call
    db_session.add(make_shop(plan_tier="starter"))
    await db_session.commit()
    resp = call(db_session, "PUT", json={"delivery": {"enabled": True}})
    assert resp.status_code == 403 and resp.json()["detail"]["code"] == "feature_not_available"


@pytest.mark.asyncio
async def test_growth_saves_rules(db_session):
    from tests.integration.test_gift_settings import call
    db_session.add(make_shop(plan_tier="growth"))
    await db_session.commit()
    assert call(db_session, "PUT", json={"delivery": {"enabled": True, "transit_days": 5,
                                                      "blackout_dates": ["2026-12-25"]}}).status_code == 200
    d = call(db_session, "GET").json()["delivery"]
    assert d["enabled"] and d["transit_days"] == 5 and d["blackout_dates"] == ["2026-12-25"]


@pytest.mark.asyncio
async def test_widget_gets_the_date_range_only_when_on(db_session):
    from tests.integration.test_storefront import call as proxy_call
    shop = delivery_shop(plan_tier="growth")
    db_session.add(shop)
    await db_session.commit()
    w = proxy_call(db_session, "GET", "/api/storefront/config").json()["delivery"]
    # Today is the 1 processing day → ships tomorrow → + 3 days in transit.
    assert w == {"earliest": in_days(4), "latest": in_days(60)}
    shop.gift_settings = {"delivery": {**shop.gift_settings["delivery"], "enabled": False}}
    await db_session.commit()
    assert proxy_call(db_session, "GET", "/api/storefront/config").json()["delivery"] is None
