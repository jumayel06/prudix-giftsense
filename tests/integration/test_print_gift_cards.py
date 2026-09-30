"""GET /print/gift-cards: the admin Print action's printable page (one card per gift)."""
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import jwt
import pytest
from fastapi.testclient import TestClient

from app.main import app
from core.config import settings
from core.db.session import get_db
from tests.conftest import TEST_API_SECRET, TEST_SHOP_DOMAIN, make_shop

ORDER_GID = "gid://shopify/Order/5551"


def session_token(shop=TEST_SHOP_DOMAIN, secret=TEST_API_SECRET):
    now = int(time.time())
    return jwt.encode({"iss": f"https://{shop}/admin", "dest": f"https://{shop}", "aud": settings.shopify_api_key,
                       "sub": "1", "exp": now + 60, "nbf": now - 5, "iat": now}, secret, algorithm="HS256")


def order_response(attrs=None, lines=None):
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"data": {
        "shop": {"name": "Snow & Co"},
        "order": {"id": ORDER_GID, "name": "#1001",
                  "customAttributes": [{"key": k, "value": v} for k, v in (attrs or {}).items()],
                  "lineItems": {"nodes": lines or []}},
    }}
    return resp


def line(name, props=None, qty=1):
    return {"name": name, "quantity": qty, "customAttributes": [{"key": k, "value": v} for k, v in (props or {}).items()]}


def get(db_session, gql_resp, params="", headers=None):
    async def override_db():
        yield db_session
    app.dependency_overrides[get_db] = override_db
    try:
        with patch("app.routes.print_cards.get_valid_access_token", AsyncMock(return_value="tok")), \
             patch("app.routes.print_cards.shopify_graphql_post", AsyncMock(return_value=gql_resp)) as gql:
            resp = TestClient(app).get(f"/print/gift-cards?orderId={ORDER_GID}{params}", headers=headers or {})
            return resp, gql
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
async def shop(db_session):
    s = make_shop()
    db_session.add(s)
    await db_session.commit()
    return s


@pytest.mark.asyncio
async def test_self_mode_prints_one_card_per_gift(db_session, shop):
    groups = [{"id": "g1", "label": "Mom", "note": "Love you, Mom!"}, {"id": "g2", "label": "Dad", "note": ""}]
    resp, _ = get(db_session, order_response(
        attrs={"_giftsense_gifts": json.dumps(groups), "_giftsense_mode": "self"},
        lines=[line("Ski Wax", {"Gift for": "Mom", "_giftsense_gift": "g1"}),
               line("Snowboard", {"Gift for": "Dad", "_giftsense_gift": "g2"}), line("Socks")]),
        headers={"Authorization": f"Bearer {session_token()}"})
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/html")
    html = resp.text
    assert html.count('class="card"') == 2
    assert "For Mom" in html and "Love you, Mom!" in html and "Ski Wax" in html
    assert "For Dad" in html and "Snowboard" in html and "Socks" not in html
    assert "Snow &amp; Co" in html
    assert resp.headers["access-control-allow-origin"] == "https://admin.shopify.com"


@pytest.mark.asyncio
async def test_direct_mode_prints_the_order_note(db_session, shop):
    resp, _ = get(db_session, order_response(attrs={"Gift note": "Happy birthday!", "_giftsense_mode": "direct"},
                                             lines=[line("Ski Wax", {"_giftsense_gift": "order"})]),
                  params=f"&id_token={session_token()}")
    assert resp.status_code == 200 and resp.text.count('class="card"') == 1 and "Happy birthday!" in resp.text


@pytest.mark.asyncio
async def test_text_is_escaped(db_session, shop):
    resp, _ = get(db_session, order_response(attrs={"Gift note": "<script>alert(1)</script>"}),
                  headers={"Authorization": f"Bearer {session_token()}"})
    assert "<script>" not in resp.text and "&lt;script&gt;" in resp.text


@pytest.mark.asyncio
async def test_order_without_gift_data(db_session, shop):
    resp, _ = get(db_session, order_response(lines=[line("Socks")]),
                  headers={"Authorization": f"Bearer {session_token()}"})
    assert resp.status_code == 200 and "no GiftSense gift" in resp.text and 'class="card"' not in resp.text


@pytest.mark.asyncio
async def test_requires_a_valid_session_token(db_session, shop, monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")   # no ?shop= dev fallback
    assert get(db_session, order_response())[0].status_code == 401
    bad = session_token(secret="wrong")
    assert get(db_session, order_response(), headers={"Authorization": f"Bearer {bad}"})[0].status_code == 401


@pytest.mark.asyncio
async def test_only_order_gids_are_accepted(db_session, shop):
    async def override_db():
        yield db_session
    app.dependency_overrides[get_db] = override_db
    try:
        resp = TestClient(app).get("/print/gift-cards?orderId=gid://shopify/Customer/1",
                                   headers={"Authorization": f"Bearer {session_token()}"})
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_query_reads_no_customer_fields(db_session, shop):
    _, gql = get(db_session, order_response(attrs={"Gift note": "Hi"}),
                 headers={"Authorization": f"Bearer {session_token()}"})
    query = gql.await_args.args[2]
    for field in ("customer", "email", "shippingAddress", "billingAddress", "phone"):
        assert field not in query
