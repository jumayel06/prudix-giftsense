"""GET /print/gifts: the admin Print actions' printable page.

docs=cards → one gift card per gift group (note + items); docs=receipt → a
price-free gift receipt per order. orderIds takes one order (order page) or
up to 50 (orders list bulk print)."""
import json
import re
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


AUTH = {"Authorization": f"Bearer {session_token()}"}


def order(gid=ORDER_GID, name="#1001", attrs=None, lines=None):
    return {"id": gid, "name": name,
            "customAttributes": [{"key": k, "value": v} for k, v in (attrs or {}).items()],
            "lineItems": {"nodes": lines or []}}


def gql_response(*orders, shop=None):
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"data": {"shop": shop or {"name": "Snow & Co"}, "nodes": list(orders)}}
    return resp


def line(name, props=None, qty=1, price="49.00"):
    return {"name": name, "quantity": qty, "originalUnitPriceSet": {"shopMoney": {"amount": price}},
            "customAttributes": [{"key": k, "value": v} for k, v in (props or {}).items()]}


def get(db_session, gql_resp, query="", headers=None):
    async def override_db():
        yield db_session
    app.dependency_overrides[get_db] = override_db
    try:
        with patch("app.routes.print_cards.get_valid_access_token", AsyncMock(return_value="tok")), \
             patch("app.routes.print_cards.shopify_graphql_post", AsyncMock(return_value=gql_resp)) as gql:
            resp = TestClient(app).get(f"/print/gifts?{query or 'orderIds=' + ORDER_GID}",
                                       headers=AUTH if headers is None else headers)
            return resp, gql
    finally:
        app.dependency_overrides.clear()


@pytest.fixture
async def shop(db_session):
    s = make_shop()
    db_session.add(s)
    await db_session.commit()
    return s


SELF_ORDER = order(
    attrs={"_giftsense_gifts": json.dumps([{"id": "g1", "label": "Mom", "note": "Love you, Mom!"},
                                           {"id": "g2", "label": "Dad", "note": ""}]),
           "_giftsense_mode": "self", "Gift receipt": "Yes"},
    lines=[line("Ski Wax", {"Gift for": "Mom", "_giftsense_gift": "g1"}),
           line("Snowboard", {"Gift for": "Dad", "_giftsense_gift": "g2"}, price="699.95"), line("Socks")])


# ── Gift cards ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_self_mode_prints_one_card_per_gift(db_session, shop):
    resp, _ = get(db_session, gql_response(SELF_ORDER))
    assert resp.status_code == 200 and resp.headers["content-type"].startswith("text/html")
    html = resp.text
    assert html.count('class="card"') == 2 and 'class="receipt"' not in html   # cards are the default
    assert "For Mom" in html and "Love you, Mom!" in html and "Ski Wax" in html
    assert "For Dad" in html and "Snowboard" in html and "Socks" not in html
    assert "Snow &amp; Co" in html
    assert resp.headers["access-control-allow-origin"] == "https://admin.shopify.com"


@pytest.mark.asyncio
async def test_direct_mode_prints_the_order_note(db_session, shop):
    resp, _ = get(db_session, gql_response(order(attrs={"Gift note": "Happy birthday!", "_giftsense_mode": "direct"},
                                                 lines=[line("Ski Wax", {"_giftsense_gift": "order"})])),
                  query=f"orderIds={ORDER_GID}&id_token={session_token()}", headers={})
    assert resp.status_code == 200 and resp.text.count('class="card"') == 1 and "Happy birthday!" in resp.text


@pytest.mark.asyncio
async def test_text_is_escaped(db_session, shop):
    resp, _ = get(db_session, gql_response(order(attrs={"Gift note": "<script>alert(1)</script>"})))
    assert "<script>" not in resp.text and "&lt;script&gt;" in resp.text


@pytest.mark.asyncio
async def test_order_without_gift_data(db_session, shop):
    resp, _ = get(db_session, gql_response(order(lines=[line("Socks")])))
    assert resp.status_code == 200 and "no GiftSense gift" in resp.text and 'class="card"' not in resp.text


# ── Gift receipt (price-free packing slip) ───────────────────────────────────

@pytest.mark.asyncio
async def test_receipt_lists_gift_items_by_recipient_without_prices(db_session, shop):
    resp, _ = get(db_session, gql_response(SELF_ORDER), query=f"orderIds={ORDER_GID}&docs=receipt")
    html = resp.text
    assert html.count('class="receipt"') == 1 and 'class="card"' not in html
    assert "Gift receipt" in html and "#1001" in html and "Snow &amp; Co" in html
    assert "Mom" in html and "Ski Wax" in html and "Dad" in html and "Snowboard" in html
    assert "Socks" not in html                        # not a gift item
    assert "49" not in html and "699" not in html and "$" not in html   # no prices anywhere


@pytest.mark.asyncio
async def test_receipt_for_a_cart_note_order_lists_all_items(db_session, shop):
    o = order(attrs={"Gift note": "Enjoy!"}, lines=[line("Ski Wax"), line("Gloves", qty=2)])
    html = get(db_session, gql_response(o), query=f"orderIds={ORDER_GID}&docs=receipt")[0].text
    assert "Ski Wax" in html and "Gloves" in html and '<td class="qty">2</td>' in html


@pytest.mark.asyncio
async def test_cards_and_receipt_together(db_session, shop):
    html = get(db_session, gql_response(SELF_ORDER), query=f"orderIds={ORDER_GID}&docs=cards,receipt")[0].text
    assert html.count('class="card"') == 2 and html.count('class="receipt"') == 1


# ── Bulk print ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_bulk_print_several_orders(db_session, shop):
    second = order("gid://shopify/Order/5552", "#1002", attrs={"Gift note": "Cheers!"})
    ids = f"{ORDER_GID},gid://shopify/Order/5552"
    resp, gql = get(db_session, gql_response(SELF_ORDER, second), query=f"orderIds={ids}&docs=cards")
    assert resp.text.count('class="card"') == 3
    assert gql.await_args.args[3]["ids"] == [ORDER_GID, "gid://shopify/Order/5552"]


@pytest.mark.asyncio
@pytest.mark.parametrize("query", [
    "orderIds=gid://shopify/Customer/1",
    "orderIds=" + ",".join(f"gid://shopify/Order/{i}" for i in range(51)),
    f"orderIds={ORDER_GID}&docs=invoice",
])
async def test_bad_parameters_are_rejected(db_session, shop, query):
    assert get(db_session, gql_response(SELF_ORDER), query=query)[0].status_code == 422


# ── Auth and data minimization ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_requires_a_valid_session_token(db_session, shop, monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")   # no ?shop= dev fallback
    assert get(db_session, gql_response(SELF_ORDER), headers={})[0].status_code == 401
    bad = {"Authorization": f"Bearer {session_token(secret='wrong')}"}
    assert get(db_session, gql_response(SELF_ORDER), headers=bad)[0].status_code == 401


@pytest.mark.asyncio
async def test_query_reads_no_customer_fields_or_prices(db_session, shop):
    _, gql = get(db_session, gql_response(SELF_ORDER), query=f"orderIds={ORDER_GID}&docs=cards,receipt")
    query = gql.await_args.args[2]
    for field in ("customer", "shippingAddress", "billingAddress", "phone", "Price", "price"):
        assert field not in query
    # Only the store's own public contact email (shop.contactEmail), never a customer's.
    assert re.findall(r"\w*[Ee]mail\w*", query) == ["contactEmail"]


@pytest.mark.asyncio
async def test_receipt_has_date_options_and_how_to_exchange(db_session, shop):
    o = order(attrs={"Gift note": "Enjoy!"}, lines=[
        {**line("Merino Sweater - M / Charcoal", {"_giftsense_gift": "order"}, qty=2),
         "title": "Merino Sweater", "variantTitle": "M / Charcoal"},
        {**line("Ski Wax", {"_giftsense_gift": "order"}), "title": "Ski Wax", "variantTitle": "Default Title"}])
    o["createdAt"] = "2026-10-01T03:59:08Z"            # Sept 30 in New York, Oct 1 in UTC
    shop.store_timezone = "America/New_York"
    await db_session.commit()
    store = {"name": "Snow & Co", "contactEmail": "help@snowco.com", "primaryDomain": {"host": "snowco.com"}}
    html = get(db_session, gql_response(o, shop=store), query=f"orderIds={ORDER_GID}&docs=receipt")[0].text
    assert "Order #1001 · September 30, 2026" in html             # in the store's timezone
    assert "Merino Sweater" in html and "M / Charcoal" in html and '<td class="qty">2</td>' in html
    assert "Default Title" not in html
    assert "help@snowco.com" in html and "snowco.com" in html and "mention order <b>#1001</b>" in html
    assert "<!--email_off--><b>help@snowco.com</b><!--/email_off-->" in html   # Cloudflare leaves it alone
    assert "$" not in html and "49" not in html                   # still no prices


@pytest.mark.asyncio
async def test_cards_with_a_message_get_a_qr_code(db_session, shop):
    from core.db.models import GiftMedia
    db_session.add(GiftMedia(shop_id=shop.id, token="mom-token-000001", view_token="mom-view-token-0000001",
                             kind="video", storage_key="k", mime="video/mp4", status="linked"))
    await db_session.commit()
    groups = [{"id": "g1", "label": "Mom", "note": "Hi", "message": "mom-token-000001"},
              {"id": "g2", "label": "Dad", "message": "made-up-token-01"}]
    o = order(attrs={"_giftsense_gifts": json.dumps(groups)},
              lines=[line("Ski Wax", {"_giftsense_gift": "g1"}), line("Board", {"_giftsense_gift": "g2"})])
    resp, _ = get(db_session, gql_response(o, shop={"name": "Snow & Co", "primaryDomain": {"host": "snow.co"}}))
    html = resp.text
    assert html.count('class="qr"') == 1 and html.count("<svg") == 1          # Dad's token isn't a real recording
    assert "Scan to watch your message" in html
