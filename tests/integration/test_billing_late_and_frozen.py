"""Billing audit 2026-10-07 (part 2): late webhooks after uninstall, FROZEN
subscriptions, the callback for an uninstalled store, duplicate team emails.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.main import app
from app.services import install_notify
from core.db.session import get_db
from core.shopify_auth import encrypt_token
from tests.conftest import TEST_SHOP_DOMAIN, make_shop
from tests.integration.test_webhooks import _headers, _make_webhook_body

NOW = datetime.now(timezone.utc)
D = lambda n: timedelta(days=n)  # noqa: E731


def _client(db_session):
    from fastapi.testclient import TestClient

    async def override_db():
        yield db_session
    app.dependency_overrides[get_db] = override_db
    yield TestClient(app, raise_server_exceptions=True)
    app.dependency_overrides.clear()


def _webhook(db_session, status, charge, name="GiftSense Growth Monthly Plan", send=None):
    payload = {"app_subscription": {"admin_graphql_api_id": f"gid://shopify/AppSubscription/{charge}",
                                    "status": status, "name": name}}
    body = _make_webhook_body("app_subscriptions/update", payload)
    with patch("app.services.install_notify.send_email", send or AsyncMock()):
        for client in _client(db_session):
            assert client.post("/webhooks", content=body,
                               headers=_headers(body, "app_subscriptions/update")).status_code == 200


async def _drain():
    while install_notify._pending:
        await asyncio.gather(*list(install_notify._pending))


def _utc(dt):
    return dt.replace(tzinfo=timezone.utc) if dt and dt.tzinfo is None else dt


# ── 1. Late billing webhooks after uninstall are ignored ─────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("gone_status", ["uninstalled", "purged"])
@pytest.mark.parametrize("status", ["expired", "declined", "active", "cancelled", "frozen"])
async def test_billing_webhooks_for_a_gone_store_change_nothing(db_session, monkeypatch, gone_status, status):
    from core import config as core_config
    monkeypatch.setattr(core_config.settings, "install_notify_email", "team@example.com")
    shop = make_shop(plan_status=gone_status, plan_tier="none")
    shop.access_token_encrypted = ""
    shop.trial_used = False
    shop.data_purge_at = NOW + D(29)
    db_session.add(shop)
    await db_session.commit()
    send = AsyncMock()
    _webhook(db_session, status, "1300", send=send)
    await _drain()
    await db_session.refresh(shop)
    assert shop.plan_status == gone_status            # purge cron + GDPR shop/redact still find it
    assert shop.trial_used is False and shop.grace_period_ends_at is None
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_unapproved_charge_after_uninstall_still_gets_purged(db_session):
    """The likely trigger: plan picker → uninstall → Shopify expires the charge
    ~48h later (same time as GDPR shop/redact). The store must still match the
    purge cron's query (plan_status == uninstalled, data_purge_at due)."""
    from sqlalchemy import select
    from core.db.models import Shop
    shop = make_shop(plan_status="uninstalled", plan_tier="none")
    shop.access_token_encrypted = ""
    shop.data_purge_at = NOW - timedelta(minutes=1)
    db_session.add(shop)
    await db_session.commit()
    _webhook(db_session, "expired", "1400")
    due = (await db_session.execute(select(Shop.id).where(
        Shop.plan_status == "uninstalled",
        Shop.data_purge_at.is_not(None),
        Shop.data_purge_at <= datetime.now(timezone.utc),
    ))).scalars().all()
    assert shop.id in due


# ── 2. FROZEN ────────────────────────────────────────────────────────────────

async def _live(db_session, status="active", trial_days_left=None, charge="1500"):
    shop = make_shop(plan_status=status, plan_tier="growth", billing_cycle_start=NOW - D(10))
    shop.access_token_encrypted = encrypt_token("tok")
    shop.trial_used = True
    shop.shopify_charge_id = charge
    if trial_days_left is not None:
        shop.trial_ends_at = NOW + D(trial_days_left)
    db_session.add(shop)
    await db_session.commit()
    return shop


@pytest.mark.asyncio
async def test_frozen_pauses_generation_and_background_work(db_session):
    shop = await _live(db_session)
    _webhook(db_session, "frozen", "1500")
    await db_session.refresh(shop)
    assert shop.plan_status == "frozen"
    from app.plan_guard import require_feature, require_generation
    for guard in (require_generation, require_feature):
        with pytest.raises(HTTPException) as exc:
            await guard("ad_copy", TEST_SHOP_DOMAIN, db_session)
        assert exc.value.status_code == 403 and exc.value.detail["code"] == "subscription_frozen"


@pytest.mark.asyncio
async def test_unfreeze_restores_active_without_resetting_the_quota_window(db_session):
    shop = await _live(db_session)
    anchor = _utc(shop.billing_cycle_start)
    _webhook(db_session, "frozen", "1500")
    _webhook(db_session, "active", "1500")
    await db_session.refresh(shop)
    assert shop.plan_status == "active"
    assert abs((_utc(shop.billing_cycle_start) - anchor).total_seconds()) < 2


@pytest.mark.asyncio
async def test_unfreeze_mid_trial_restores_the_trial(db_session):
    shop = await _live(db_session, status="trial_active", trial_days_left=4)
    _webhook(db_session, "frozen", "1500")
    _webhook(db_session, "active", "1500")
    await db_session.refresh(shop)
    assert shop.plan_status == "trial_active"


@pytest.mark.asyncio
async def test_unfreeze_after_trial_end_is_active(db_session):
    shop = await _live(db_session, status="trial_active", trial_days_left=4)
    _webhook(db_session, "frozen", "1500")
    shop.trial_ends_at = NOW - D(1)
    await db_session.commit()
    _webhook(db_session, "active", "1500")
    await db_session.refresh(shop)
    assert shop.plan_status == "active"


@pytest.mark.asyncio
async def test_frozen_for_an_old_charge_is_ignored(db_session):
    shop = await _live(db_session, charge="1600")
    _webhook(db_session, "frozen", "1599")
    await db_session.refresh(shop)
    assert shop.plan_status == "active"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "cancelled", "expired"])
async def test_frozen_only_pauses_live_plans(db_session, status):
    shop = await _live(db_session, status=status)
    _webhook(db_session, "frozen", "1500")
    await db_session.refresh(shop)
    assert shop.plan_status == status


# ── 3. /billing/callback for a store that's gone ─────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("status,token", [("uninstalled", ""), ("purged", ""), ("active", "")])
async def test_callback_for_a_gone_store_redirects_instead_of_500(db_session, status, token):
    shop = make_shop(plan_status=status, plan_tier="none")
    shop.access_token_encrypted = token
    db_session.add(shop)
    await db_session.commit()
    with patch("app.routes.billing.httpx.AsyncClient") as cls:
        for client in _client(db_session):
            resp = client.get(f"/billing/callback?shop={TEST_SHOP_DOMAIN}&charge_id=1&plan=pro",
                              follow_redirects=False)
        cls.assert_not_called()
    assert resp.status_code in (302, 307)
    assert resp.headers["location"].startswith(f"https://{TEST_SHOP_DOMAIN}/admin/apps/")
    await db_session.refresh(shop)
    assert shop.plan_status == status


# ── 4. One team email per (store, charge) ────────────────────────────────────

@pytest.mark.asyncio
async def test_callback_and_webhook_racing_send_one_email(monkeypatch):
    from core import config as core_config
    monkeypatch.setattr(core_config.settings, "install_notify_email", "team@example.com")
    shop = MagicMock(shop_domain="race.myshopify.com", shop_owner_email=None, store_timezone=None)
    send = AsyncMock()
    with patch("app.services.install_notify.send_email", send):
        for _ in range(2):   # both paths decided "first approval" at the same instant
            install_notify.notify_first_subscription(shop, plan="pro", trial=False, annual=False,
                                                     returning=False, charge_id="777")
        await _drain()
        install_notify.notify_first_subscription(shop, plan="pro", trial=False, annual=False,
                                                 returning=True, charge_id="778")   # a later re-subscribe
        await _drain()
    assert send.await_count == 2


@pytest.mark.asyncio
async def test_claim_fails_open_when_redis_errors(monkeypatch):
    from core import config as core_config
    monkeypatch.setattr(core_config.settings, "redis_url", "redis://127.0.0.1:1")   # nothing listening
    assert await install_notify._claim("x:1") is True
