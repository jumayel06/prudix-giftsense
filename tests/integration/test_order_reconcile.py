"""Every order counted once (webhook + hourly reconcile), and missed orders/create
webhooks recovered by the reconcile."""
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from app.services import gift_orders as go
from app.workers.orders import reconcile_orders, webhook_payload
from core.db.models import CountedOrder, GiftOrder, OrderCountDaily
from tests.conftest import TEST_SHOP_DOMAIN, make_shop
from tests.integration.test_webhooks import _headers, _make_client
from tests.unit.test_gift_orders_parse import SID, line, order


def post(db_session, payload, webhook_id=None):
    body = json.dumps(payload).encode()
    for client in _make_client(db_session):
        return client.post("/webhooks", content=body, headers=_headers(body, "orders/create", webhook_id=webhook_id))


async def seeded(db_session):
    shop = make_shop()
    db_session.add(shop)
    await db_session.commit()
    return shop


async def counts(db_session):
    rows = (await db_session.execute(select(OrderCountDaily))).scalars().all()
    return sum(r.orders for r in rows), sum(float(r.revenue) for r in rows)


def gql_order(oid=7007, created=None, gift=True):
    """A GraphQL Order as the reconcile's detail query returns it."""
    created = created or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    props = [{"key": "_giftsense_sid", "value": SID}, {"key": "_giftsense_gift", "value": "1"}] if gift else []
    return {"id": f"gid://shopify/Order/{oid}", "legacyResourceId": str(oid), "name": "#1007", "createdAt": created,
            "totalPriceSet": {"shopMoney": {"amount": "74.69", "currencyCode": "USD"}},
            "customAttributes": [{"key": "_giftsense_sid", "value": SID}] if gift else [],
            "lineItems": {"nodes": [{"quantity": 1, "originalUnitPriceSet": {"shopMoney": {"amount": "43.69"}},
                                     "customAttributes": props}]}}


def fake_shopify(orders):
    """shopify_graphql_post stand-in: the ids listing, then details for asked ids."""
    by_gid = {o["id"]: o for o in orders}
    calls = []

    async def gql(shop, token, query, variables):
        calls.append(variables)
        r = MagicMock(status_code=200)
        if "ids" in variables:
            r.json.return_value = {"data": {"nodes": [by_gid[g] for g in variables["ids"]]}}
        else:
            r.json.return_value = {"data": {"orders": {"pageInfo": {"hasNextPage": False, "endCursor": None},
                                                       "nodes": [{"id": o["id"], "legacyResourceId": o["legacyResourceId"]}
                                                                 for o in orders]}}}
        return r
    gql.calls = calls
    return gql


async def run_reconcile(db_session, gql):
    with patch("app.workers.orders.AsyncSessionLocal") as sess, \
            patch("app.workers.orders.get_valid_access_token", AsyncMock(return_value="tok")), \
            patch("app.workers.orders.shopify_graphql_post", gql):
        sess.return_value.__aenter__.return_value = db_session
        sess.return_value.__aexit__.return_value = False
        await reconcile_orders({})


# ── the counter ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_same_order_twice_is_counted_once(db_session, job_pool):
    await seeded(db_session)
    payload = order([line()], id=4242)
    post(db_session, payload)
    post(db_session, payload, webhook_id=str(uuid.uuid4()))      # same order, new webhook id
    assert await counts(db_session) == (1, 120.0)


@pytest.mark.asyncio
async def test_order_is_counted_on_its_own_day(db_session, job_pool):
    await seeded(db_session)
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    post(db_session, order([line()], id=4243, created_at=yesterday.isoformat()))
    row = (await db_session.execute(select(OrderCountDaily))).scalar_one()
    assert row.day == yesterday.date()


# ── the hourly reconcile ─────────────────────────────────────────────────────

def test_graphql_order_parses_like_the_webhook():
    parsed = go.parse_gift_order(webhook_payload(gql_order()))
    assert parsed.order_id == "7007" and parsed.order_gid == "gid://shopify/Order/7007"
    assert str(parsed.sid) == SID and parsed.gift_lines == 1 and parsed.gift_revenue == 43.69
    assert parsed.order_total == 74.69 and parsed.currency == "USD" and parsed.order_name == "#1007"


@pytest.mark.asyncio
async def test_reconcile_recovers_a_missed_gift_order_once(db_session, job_pool):
    await seeded(db_session)
    gql = fake_shopify([gql_order()])
    await run_reconcile(db_session, gql)
    assert await counts(db_session) == (1, 74.69)
    gift = (await db_session.execute(select(GiftOrder))).scalar_one()
    assert gift.order_id == "7007" and gift.gift_lines == 1
    assert job_pool.names() == ["annotate_gift_order"]

    gql.calls.clear()
    await run_reconcile(db_session, gql)                          # next hour: nothing new
    assert await counts(db_session) == (1, 74.69)
    assert len((await db_session.execute(select(GiftOrder))).scalars().all()) == 1
    assert all("ids" not in c for c in gql.calls)                  # no detail fetch for counted orders


@pytest.mark.asyncio
async def test_reconcile_skips_orders_the_webhook_already_counted(db_session, job_pool):
    await seeded(db_session)
    post(db_session, order([line(props={"_giftsense_gift": "1"})], id=7007))
    gql = fake_shopify([gql_order()])
    await run_reconcile(db_session, gql)
    assert await counts(db_session) == (1, 120.0)                  # the webhook's count, not doubled
    assert len((await db_session.execute(select(GiftOrder))).scalars().all()) == 1
    assert all("ids" not in c for c in gql.calls)


@pytest.mark.asyncio
async def test_reconcile_counts_plain_orders_too(db_session, job_pool):
    await seeded(db_session)
    await run_reconcile(db_session, fake_shopify([gql_order(gift=False)]))
    assert await counts(db_session) == (1, 74.69)
    assert (await db_session.execute(select(GiftOrder))).scalars().all() == []
    assert (await db_session.execute(select(CountedOrder))).scalar_one().order_id == "7007"


@pytest.mark.asyncio
async def test_reconcile_failure_for_one_shop_is_contained(db_session, job_pool):
    await seeded(db_session)

    async def broken(*a, **k):
        r = MagicMock(status_code=503)
        r.json.return_value = {}
        return r
    await run_reconcile(db_session, broken)                        # logs, doesn't raise
    assert await counts(db_session) == (0, 0)
