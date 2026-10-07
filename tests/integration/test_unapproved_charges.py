"""A store that never had a plan stays `pending` when Shopify declines or
expires its unapproved charge (2026-10-06 billing audit).

Before: the store became `declined` / `expired`, so a merchant returning later
saw a "Payment Failed"/"Expired" dashboard instead of the plan picker, an
`expired` store got a 7-day grace period (free read access on the Starter
default), and a later approval could be mistaken for a renewal by the webhook
(no trial in our records, no team email).
"""
from unittest.mock import AsyncMock, patch

import pytest

from app.main import app
from core.db.models import Shop
from core.db.session import get_db
from tests.conftest import make_shop
from tests.integration.test_webhooks import _headers, _make_webhook_body


def _client(db_session):
    from fastapi.testclient import TestClient

    async def override_db():
        yield db_session
    app.dependency_overrides[get_db] = override_db
    yield TestClient(app, raise_server_exceptions=True)
    app.dependency_overrides.clear()


def _webhook(db_session, status, charge="555", name="Prudix Growth Monthly Plan", send=None):
    payload = {"app_subscription": {"admin_graphql_api_id": f"gid://shopify/AppSubscription/{charge}",
                                    "status": status, "name": name}}
    body = _make_webhook_body("app_subscriptions/update", payload)
    with patch("app.services.install_notify.send_email", send or AsyncMock()):
        for client in _client(db_session):
            assert client.post("/webhooks", content=body,
                               headers=_headers(body, "app_subscriptions/update")).status_code == 200


async def _shop(db_session, **kw):
    shop = make_shop(**kw)
    db_session.add(shop)
    await db_session.commit()
    return shop


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["declined", "expired"])
async def test_unapproved_charge_on_a_pending_store_keeps_it_pending(db_session, status):
    shop = await _shop(db_session, plan_status="pending", plan_tier="none")
    _webhook(db_session, status)
    await db_session.refresh(shop)
    assert shop.plan_status == "pending"
    assert shop.grace_period_ends_at is None          # no free read access
    assert shop.plan_tier == "none" and shop.shopify_charge_id is None


@pytest.mark.asyncio
async def test_expired_then_approved_gets_trial_and_team_email(db_session, monkeypatch):
    """The 48h-expiry return path end to end: expiry keeps pending, the later
    approval (webhook first) applies the trial and emails the team."""
    from core import config as core_config
    monkeypatch.setattr(core_config.settings, "install_notify_email", "team@example.com")
    shop = await _shop(db_session, plan_status="pending", plan_tier="none", trial_used=False)
    _webhook(db_session, "expired", charge="555")
    send = AsyncMock()
    _webhook(db_session, "active", charge="556", send=send)
    from app.services import install_notify
    import asyncio
    while install_notify._pending:
        await asyncio.gather(*list(install_notify._pending))
    await db_session.refresh(shop)
    assert shop.plan_status == "trial_active" and shop.trial_used is True
    send.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("old_status", ["declined", "expired"])
async def test_legacy_declined_or_expired_row_without_a_plan_activates_like_pending(db_session, old_status):
    """Rows stored as declined/expired before the fix (never had a plan)."""
    shop = await _shop(db_session, plan_status=old_status, plan_tier="none", trial_used=False)
    _webhook(db_session, "active", charge="777")
    await db_session.refresh(shop)
    assert shop.plan_status == "trial_active" and shop.trial_used is True


@pytest.mark.asyncio
async def test_legacy_row_that_used_its_trial_activates_paid(db_session):
    shop = await _shop(db_session, plan_status="expired", plan_tier="none", trial_used=True)
    _webhook(db_session, "active", charge="778")
    await db_session.refresh(shop)
    assert shop.plan_status == "active"


# ── Stores that had a plan are unchanged ─────────────────────────────────────

@pytest.mark.asyncio
async def test_expiry_of_the_current_charge_on_a_plan_store_still_expires(db_session):
    shop = await _shop(db_session, plan_status="active", plan_tier="growth")
    shop.shopify_charge_id = "555"
    await db_session.commit()
    _webhook(db_session, "expired", charge="555")
    await db_session.refresh(shop)
    assert shop.plan_status == "expired" and shop.grace_period_ends_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["declined", "expired"])
async def test_unapproved_upgrade_on_a_paying_store_is_ignored(db_session, status):
    shop = await _shop(db_session, plan_status="active", plan_tier="growth")
    shop.shopify_charge_id = "100"
    await db_session.commit()
    _webhook(db_session, status, charge="200")          # the unapproved upgrade
    await db_session.refresh(shop)
    assert (shop.plan_status, shop.plan_tier) == ("active", "growth")


@pytest.mark.asyncio
async def test_declined_resubscribe_keeps_a_cancelled_store_cancelled(db_session):
    """Callback path: a cancelled merchant (still in paid period / grace) who
    declines a new charge keeps that access — not demoted to declined."""
    from tests.integration.test_billing import _graphql_subscription_response
    from core.shopify_auth import encrypt_token
    shop = await _shop(db_session, plan_status="cancelled", plan_tier="growth")
    shop.access_token_encrypted = encrypt_token("tok")
    await db_session.commit()
    with patch("app.routes.billing.httpx.AsyncClient") as cls:
        http = AsyncMock()
        http.__aenter__ = AsyncMock(return_value=http)
        http.__aexit__ = AsyncMock(return_value=False)
        http.post = AsyncMock(return_value=_graphql_subscription_response("DECLINED"))
        cls.return_value = http
        for client in _client(db_session):
            client.get(f"/billing/callback?shop={shop.shop_domain}&charge_id=9&plan=growth",
                       follow_redirects=False)
    await db_session.refresh(shop)
    assert shop.plan_status == "cancelled"
