"""customers/redact + customers/data_request (ported from Prudix Commerce's
2026-09-27 fix, where both were no-ops claiming "no PII").

GiftSense holds no customer- or order-linked rows yet (gift orders arrive in
week 4), so today these assert the plumbing: shop lookup, HMAC, the owner
email for data requests, and retry-on-failure. Week 4 adds the gift tables to
app/services/gdpr.py and extends these tests with seeded rows.
"""
import base64
import hashlib
import hmac
import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from core.db.session import get_db
from tests.conftest import TEST_API_SECRET, TEST_SHOP_DOMAIN, make_shop


def _sign(body: bytes) -> str:
    return base64.b64encode(hmac.new(TEST_API_SECRET.encode(), body, hashlib.sha256).digest()).decode()


def _client(db_session):
    async def override_db():
        yield db_session
    app.dependency_overrides[get_db] = override_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _payload(**extra):
    return {"shop_domain": TEST_SHOP_DOMAIN, "customer": {"id": 42}, "orders_requested": [1001],
            "orders_to_redact": [1001], "data_request": {"id": 9}, **extra}


@pytest.mark.asyncio
async def test_data_request_emails_store_owner(db_session):
    shop = make_shop()
    shop.shop_owner_email = "owner@example.com"
    db_session.add(shop)
    await db_session.commit()

    body = json.dumps(_payload()).encode()
    with patch("app.services.postmark_client.send_email", AsyncMock(return_value={})) as send:
        for client in _client(db_session):
            resp = client.post("/webhooks/gdpr/customers/data_request", content=body,
                               headers={"X-Shopify-Hmac-Sha256": _sign(body)})
    assert resp.status_code == 200
    send.assert_awaited_once()
    kwargs = send.await_args.kwargs
    assert kwargs["to_email"] == "owner@example.com"
    assert "42" in kwargs["text_body"]


@pytest.mark.asyncio
async def test_data_request_email_failure_returns_500_so_shopify_retries(db_session):
    shop = make_shop()
    shop.shop_owner_email = "owner@example.com"
    db_session.add(shop)
    await db_session.commit()

    body = json.dumps(_payload()).encode()
    with patch("app.services.postmark_client.send_email", AsyncMock(side_effect=RuntimeError("down"))):
        for client in _client(db_session):
            resp = client.post("/webhooks/gdpr/customers/data_request", content=body,
                               headers={"X-Shopify-Hmac-Sha256": _sign(body)})
    assert resp.status_code == 500


@pytest.mark.asyncio
async def test_data_request_without_owner_email_is_acknowledged(db_session):
    db_session.add(make_shop())
    await db_session.commit()

    body = json.dumps(_payload()).encode()
    with patch("app.services.postmark_client.send_email", AsyncMock()) as send:
        for client in _client(db_session):
            resp = client.post("/webhooks/gdpr/customers/data_request", content=body,
                               headers={"X-Shopify-Hmac-Sha256": _sign(body)})
    assert resp.status_code == 200
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_customers_redact_runs_redaction(db_session):
    db_session.add(make_shop())
    await db_session.commit()

    body = json.dumps(_payload()).encode()
    with patch("app.services.gdpr.redact_customer", AsyncMock(return_value={})) as redact:
        for client in _client(db_session):
            resp = client.post("/webhooks/gdpr/customers/redact", content=body,
                               headers={"X-Shopify-Hmac-Sha256": _sign(body)})
    assert resp.status_code == 200
    redact.assert_awaited_once()


@pytest.mark.asyncio
async def test_customer_webhooks_require_hmac(db_session):
    body = json.dumps(_payload()).encode()
    for client in _client(db_session):
        for path in ("/webhooks/gdpr/customers/redact", "/webhooks/gdpr/customers/data_request"):
            resp = client.post(path, content=body, headers={"X-Shopify-Hmac-Sha256": "bad"})
            assert resp.status_code == 401


def test_gdpr_ids_parsing():
    from app.services.gdpr import customer_and_order_ids
    assert customer_and_order_ids({"customer": {"id": 5}, "orders_to_redact": [1, 2]}) == ("5", ["1", "2"])
    assert customer_and_order_ids({"orders_requested": [7]}) == (None, ["7"])
