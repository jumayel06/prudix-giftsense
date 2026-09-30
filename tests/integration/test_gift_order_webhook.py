"""orders/create → gift_orders (+ annotate job), and GDPR for gift orders."""
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from app.services import gift_orders as go
from core.db.models import GiftOrder, GiftSession
from tests.conftest import TEST_SHOP_DOMAIN, make_shop
from tests.integration.test_webhooks import _headers, _make_client
from tests.unit.test_gift_orders_parse import SID, line, order


def post(db_session, topic, payload):
    body = json.dumps(payload).encode()
    for client in _make_client(db_session):
        return client.post("/webhooks", content=body, headers=_headers(body, topic))


@pytest.mark.asyncio
async def test_gift_order_is_stored_and_annotation_queued(db_session, job_pool):
    shop = make_shop()
    db_session.add(shop)
    await db_session.flush()
    db_session.add(GiftSession(shop_id=shop.id, sid=uuid.UUID(SID), intake={}, last_picks=[], searches=1,
                               note_drafts={"order": {"count": 1, "last": "Happy birthday, Mom!"}}))
    await db_session.commit()
    payload = order([line(props={"_giftsense_sid": SID, "_giftsense_gift": "1"})],
                    attrs={"_giftsense_sid": SID, "Gift note": "Happy birthday, dear Mom!"})
    assert post(db_session, "orders/create", payload).status_code == 200

    row = (await db_session.execute(select(GiftOrder))).scalar_one()
    assert row.order_id == "5551" and str(row.sid) == SID and row.gift_lines == 1
    assert float(row.gift_revenue) == 30.0 and row.note_source == "ai_edited"
    assert job_pool.names() == ["annotate_gift_order"]
    assert job_pool.jobs[0][2] == "gid://shopify/Order/5551"


@pytest.mark.asyncio
async def test_plain_order_is_ignored(db_session, job_pool):
    db_session.add(make_shop())
    await db_session.commit()
    post(db_session, "orders/create", order([line()]))
    assert (await db_session.execute(select(GiftOrder))).scalars().all() == [] and job_pool.jobs == []


@pytest.mark.asyncio
async def test_redelivered_order_is_stored_once(db_session, job_pool):
    db_session.add(make_shop())
    await db_session.commit()
    payload = order([line(props={"_giftsense_gift": "1"})])
    post(db_session, "orders/create", payload)
    body = json.dumps(payload).encode()
    for client in _make_client(db_session):   # same order, new webhook id
        client.post("/webhooks", content=body, headers=_headers(body, "orders/create", webhook_id=str(uuid.uuid4())))
    assert len((await db_session.execute(select(GiftOrder))).scalars().all()) == 1


@pytest.mark.asyncio
async def test_annotate_job_creates_definition_once_then_tags_and_writes_metafield(db_session):
    from app.workers.orders import annotate_gift_order
    shop = make_shop()
    db_session.add(shop)
    await db_session.commit()
    ok = MagicMock(status_code=200)
    ok.json.return_value = {"data": {}}
    gql = AsyncMock(return_value=ok)
    with patch("app.workers.orders.AsyncSessionLocal") as sess, \
         patch("app.workers.orders.get_valid_access_token", AsyncMock(return_value="tok")), \
         patch("app.services.gift_orders.shopify_graphql_post", gql):
        sess.return_value.__aenter__.return_value = db_session
        sess.return_value.__aexit__.return_value = False
        for _ in range(2):
            await annotate_gift_order({}, TEST_SHOP_DOMAIN, "gid://shopify/Order/1", {"version": 1, "groups": []})
    queries = [c.args[2] for c in gql.await_args_list]
    assert sum("metafieldDefinitionCreate" in q for q in queries) == 1
    assert sum("tagsAdd" in q for q in queries) == 2 and sum("metafieldsSet" in q for q in queries) == 2


@pytest.mark.asyncio
async def test_customers_redact_deletes_gift_orders_and_linked_sessions(db_session):
    from app.services.gdpr import collect_customer_data, redact_customer
    shop = make_shop()
    db_session.add(shop)
    await db_session.flush()
    sid = uuid.uuid4()
    db_session.add_all([
        GiftOrder(shop_id=shop.id, order_id="5551", order_name="#1001", sid=sid, gift_lines=1,
                  gift_revenue=30, order_total=120, currency="USD", groups=[]),
        GiftOrder(shop_id=shop.id, order_id="7777", order_name="#1002", gift_lines=1,
                  gift_revenue=10, order_total=10, currency="USD", groups=[]),
        GiftSession(shop_id=shop.id, sid=sid, intake={}, last_picks=[], searches=1),
    ])
    await db_session.commit()
    payload = {"customer": {"id": 42}, "orders_to_redact": [5551]}
    data = await collect_customer_data(shop.id, {"customer": {"id": 42}, "orders_requested": [5551]}, db_session)
    assert [r["order_id"] for r in data["gift_orders"]] == ["5551"] and data["gift_sessions"]
    await redact_customer(shop.id, payload, db_session)
    assert [r.order_id for r in (await db_session.execute(select(GiftOrder))).scalars()] == ["7777"]
    assert (await db_session.execute(select(GiftSession))).scalars().all() == []


@pytest.mark.asyncio
async def test_direct_mode_wrap_is_kept_for_the_orders_page(db_session, job_pool):
    db_session.add(make_shop())
    await db_session.commit()
    post(db_session, "orders/create", order([line(props={"_giftsense_gift": "order"})],
                                            attrs={"_giftsense_mode": "direct", "Gift wrap": "Gold"}))
    row = (await db_session.execute(select(GiftOrder))).scalar_one()
    assert row.groups == [{"id": "order", "label": None, "wrap": "Gold", "message": None}]
