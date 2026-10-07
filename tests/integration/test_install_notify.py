"""Internal team email on a merchant's FIRST billing approval.

Not at install time; not on upgrades/renewals/duplicates; never breaks billing.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.main import app
from app.services import install_notify
from core.db.session import get_db
from core.shopify_auth import encrypt_token
from tests.conftest import TEST_SHOP_DOMAIN, make_shop
from tests.integration.test_billing import _graphql_subscription_response
from tests.integration.test_webhooks import _headers, _make_webhook_body

_SEND = "app.services.install_notify.send_email"


@pytest.fixture(autouse=True)
def notify_on(monkeypatch):
    from core import config as core_config
    monkeypatch.setattr(core_config.settings, "install_notify_email", "team@example.com")


def _client(db_session):
    from fastapi.testclient import TestClient

    async def override_db():
        yield db_session
    app.dependency_overrides[get_db] = override_db
    yield TestClient(app, raise_server_exceptions=True)
    app.dependency_overrides.clear()


def _callback(db_session, send, status="ACTIVE", name="GiftSense Growth Monthly Plan", charge="999"):
    with patch("app.routes.billing.httpx.AsyncClient") as cls, patch(_SEND, send):
        http = AsyncMock()
        http.__aenter__ = AsyncMock(return_value=http)
        http.__aexit__ = AsyncMock(return_value=False)
        http.post = AsyncMock(return_value=_graphql_subscription_response(status, name=name))
        cls.return_value = http
        for client in _client(db_session):
            client.get(f"/billing/callback?shop={TEST_SHOP_DOMAIN}&charge_id={charge}&plan=growth",
                       follow_redirects=False)


def _webhook(db_session, send, status="active", charge_id="gid://shopify/AppSubscription/123",
             name="GiftSense Growth Monthly Plan"):
    payload = {"app_subscription": {"admin_graphql_api_id": charge_id, "status": status, "name": name}}
    body = _make_webhook_body("app_subscriptions/update", payload)
    with patch(_SEND, send):
        for client in _client(db_session):
            resp = client.post("/webhooks", content=body, headers=_headers(body, "app_subscriptions/update"))
            assert resp.status_code == 200


async def _drain():
    while install_notify._pending:
        await asyncio.gather(*list(install_notify._pending))


async def _pending_shop(db_session, **kw):
    shop = make_shop(plan_tier="none", plan_status="pending", **kw)
    shop.access_token_encrypted = encrypt_token("tok")
    shop.shop_owner_email = "owner@example.com"
    db_session.add(shop)
    await db_session.commit()
    return shop


@pytest.mark.asyncio
async def test_billing_approval_via_callback_emails_the_team(db_session):
    await _pending_shop(db_session)
    send = AsyncMock()
    _callback(db_session, send)
    await _drain()
    send.assert_awaited_once()
    kw = send.await_args.kwargs
    assert kw["to_email"] == "team@example.com"
    assert "New merchant subscribed" in kw["subject"] and TEST_SHOP_DOMAIN in kw["subject"]
    assert "Growth" in kw["subject"] and "free trial" in kw["text_body"]
    assert "owner@example.com" in kw["text_body"]


@pytest.mark.asyncio
async def test_returning_merchant_is_labelled(db_session):
    await _pending_shop(db_session, trial_used=True)
    send = AsyncMock()
    _callback(db_session, send)
    await _drain()
    assert "Returning merchant subscribed" in send.await_args.kwargs["subject"]
    assert "paid" in send.await_args.kwargs["text_body"]


@pytest.mark.asyncio
async def test_declined_or_unapproved_charge_sends_nothing(db_session):
    await _pending_shop(db_session)
    send = AsyncMock()
    _callback(db_session, send, status="DECLINED")
    _callback(db_session, send, status="PENDING")
    await _drain()
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_callback_replay_sends_only_once(db_session):
    await _pending_shop(db_session)
    send = AsyncMock()
    _callback(db_session, send)
    _callback(db_session, send)  # same charge re-opened
    await _drain()
    assert send.await_count == 1


@pytest.mark.asyncio
async def test_upgrade_of_a_live_plan_sends_nothing(db_session):
    shop = make_shop(plan_tier="growth", plan_status="active")
    shop.access_token_encrypted = encrypt_token("tok")
    db_session.add(shop)
    await db_session.commit()
    send = AsyncMock()
    _callback(db_session, send, name="GiftSense Pro Monthly Plan", charge="1000")
    await _drain()
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_first_approval_via_webhook_emails_once_and_callback_after_is_silent(db_session):
    await _pending_shop(db_session)
    send = AsyncMock()
    _webhook(db_session, send)
    await _drain()
    assert send.await_count == 1
    _webhook(db_session, send, charge_id="gid://shopify/AppSubscription/124")  # duplicate / renewal
    await _drain()
    assert send.await_count == 1


@pytest.mark.asyncio
async def test_trial_conversion_and_cancellation_webhooks_send_nothing(db_session):
    shop = make_shop(plan_status="trial_active")
    db_session.add(shop)
    await db_session.commit()
    send = AsyncMock()
    _webhook(db_session, send)                      # trial confirmation / conversion
    _webhook(db_session, send, status="cancelled")
    await _drain()
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_failure_never_breaks_billing(db_session):
    from sqlalchemy import select
    from core.db.models import Shop
    await _pending_shop(db_session)
    send = AsyncMock(side_effect=RuntimeError("postmark down"))
    _callback(db_session, send)
    await _drain()
    send.assert_awaited_once()
    shop = (await db_session.execute(select(Shop).where(Shop.shop_domain == TEST_SHOP_DOMAIN))).scalar_one()
    assert shop.plan_status == "trial_active"       # billing still activated


@pytest.mark.asyncio
async def test_empty_setting_disables_the_email(db_session, monkeypatch):
    from core import config as core_config
    monkeypatch.setattr(core_config.settings, "install_notify_email", "")
    await _pending_shop(db_session)
    send = AsyncMock()
    _callback(db_session, send)
    await _drain()
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_email_escapes_html_in_store_fields():
    send = AsyncMock()
    with patch(_SEND, send):
        await install_notify._send("x.myshopify.com", "<b>o</b>@e.com", "UTC", "pro", False, True, False)
    html = send.await_args.kwargs["html_body"]
    assert "&lt;b&gt;" in html and "<b>o</b>" not in html
    assert "annual" in send.await_args.kwargs["text_body"]


def test_scheduling_never_raises_without_a_running_loop():
    shop = MagicMock(shop_domain="x", shop_owner_email=None, store_timezone=None)
    install_notify.notify_first_subscription(shop, plan="pro", trial=False, annual=False, returning=False)
