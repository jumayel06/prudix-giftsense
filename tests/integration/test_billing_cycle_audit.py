"""Billing audit 2026-10-07: the current period is derived, never assumed.

Shopify doesn't reliably send app_subscriptions/update on renewal, so
billing_cycle_start can stay at the original approval date forever. Every
period-dependent decision (quota window, paid-until on cancel, scheduled
downgrade date, access_until) now derives the CURRENT period from it.
Plus: cancel during trial, plan switch mid-trial (webhook first), and
scheduled changes cleared on uninstall / reinstall.
"""
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.config import GRACE_PERIOD_DAYS, PLANS
from app.main import app
from app.plan_guard import (
    _paid_period_ends_at, check_generation_limit, current_period_bounds, effective_cycle_start,
)
from core.db.models import Shop, UsageLog
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


def _webhook(db_session, status, charge, name="GiftSense Growth Monthly Plan"):
    payload = {"app_subscription": {"admin_graphql_api_id": f"gid://shopify/AppSubscription/{charge}",
                                    "status": status, "name": name}}
    body = _make_webhook_body("app_subscriptions/update", payload)
    with patch("app.services.install_notify.send_email", AsyncMock()):
        for client in _client(db_session):
            assert client.post("/webhooks", content=body,
                               headers=_headers(body, "app_subscriptions/update")).status_code == 200


def _shop(**kw):
    s = make_shop(**kw)
    s.access_token_encrypted = encrypt_token("tok")
    return s


# ── Period math ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("days_in,expected_start_offset", [(0, 0), (29, 0), (30, 30), (65, 60), (400, 390)])
def test_monthly_quota_window_rolls_every_30_days(days_in, expected_start_offset):
    anchor = NOW - D(days_in)
    shop = make_shop(plan_status="active", billing_cycle_start=anchor)
    assert effective_cycle_start(shop, NOW) == anchor + D(expected_start_offset)


@pytest.mark.parametrize("days_in,end_offset", [(5, 30), (35, 60), (95, 120)])
def test_current_period_bounds(days_in, end_offset):
    anchor = NOW - D(days_in)
    shop = make_shop(billing_cycle_start=anchor)
    start, end = current_period_bounds(shop, NOW)
    assert end == anchor + D(end_offset) and end - start == D(30)
    assert start <= NOW < end


def test_future_or_missing_anchor():
    shop = make_shop(billing_cycle_start=NOW + D(2))
    assert effective_cycle_start(shop, NOW) == shop.billing_cycle_start
    shop.billing_cycle_start = None
    assert effective_cycle_start(shop, NOW) is None and current_period_bounds(shop) is None


# ── 1. A paying merchant isn't blocked in month 2 ────────────────────────────

@pytest.mark.asyncio
async def test_month_two_merchant_gets_a_fresh_quota(db_session):
    anchor = NOW - D(35)
    shop = make_shop(plan_status="active", plan_tier="starter", billing_cycle_start=anchor)
    db_session.add(shop)
    await db_session.commit()
    limit = PLANS["starter"]["generation_limit"]
    # Month 1 fully used up.
    db_session.add(UsageLog(id=uuid.uuid4(), shop_id=shop.id, action_type="ad_copy_generation",
                            generations_consumed=limit, tokens_input=1, tokens_output=1,
                            model_used="gpt-6-luna", cost_usd=0.01, created_at=anchor + D(10)))
    await db_session.commit()
    remaining = await check_generation_limit(shop, db_session)
    assert remaining == limit - 1          # month 2 starts fresh (was: 429 forever)


# ── 1b. Cancel in month 2+ keeps what was paid for ───────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("days_in,paid_offset", [(35, 60), (5, 30)])
async def test_cancel_keeps_the_current_paid_period(db_session, days_in, paid_offset):
    anchor = NOW - D(days_in)
    shop = _shop(plan_status="active", plan_tier="growth", billing_cycle_start=anchor)
    shop.shopify_charge_id = "100"
    db_session.add(shop)
    await db_session.commit()
    _webhook(db_session, "cancelled", "100", name="GiftSense Growth Monthly Plan")
    await db_session.refresh(shop)
    assert shop.plan_status == "cancelled"
    assert abs((_paid_period_ends_at(shop) - (anchor + D(paid_offset))).total_seconds()) < 2

    for client in _client(db_session):
        data = client.get(f"/api/stats?shop={TEST_SHOP_DOMAIN}").json()
    assert datetime.fromisoformat(data["access_until"]) > NOW


@pytest.mark.asyncio
async def test_paid_end_is_fixed_at_cancellation_not_rolling(db_session):
    """After the cancel, the paid end must not keep moving forward."""
    shop = make_shop(plan_status="cancelled", billing_cycle_start=NOW - D(95),
                     grace_period_ends_at=NOW - D(1))
    assert _paid_period_ends_at(shop) == shop.grace_period_ends_at - D(GRACE_PERIOD_DAYS)
    assert _paid_period_ends_at(shop) < NOW


# ── 2. Cancel during trial: nothing paid ─────────────────────────────────────

@pytest.mark.asyncio
async def test_cancel_during_trial_ends_access_now_then_grace(db_session):
    shop = _shop(plan_status="trial_active", plan_tier="pro", billing_cycle_start=NOW - D(1))
    shop.trial_used = True
    shop.trial_started_at = NOW - D(1)
    shop.trial_ends_at = NOW + D(6)
    shop.shopify_charge_id = "300"
    db_session.add(shop)
    await db_session.commit()
    _webhook(db_session, "cancelled", "300", name="GiftSense Pro Monthly Plan")
    await db_session.refresh(shop)
    assert shop.plan_status == "cancelled"
    paid_end = _paid_period_ends_at(shop)
    assert paid_end <= datetime.now(timezone.utc)                  # no free month
    grace = shop.grace_period_ends_at.replace(tzinfo=timezone.utc) if shop.grace_period_ends_at.tzinfo is None else shop.grace_period_ends_at
    assert grace - D(GRACE_PERIOD_DAYS) == paid_end              # read-only grace only

    from app.plan_guard import require_generation
    with pytest.raises(HTTPException) as exc:
        await require_generation("ad_copy", TEST_SHOP_DOMAIN, db_session)
    assert exc.value.status_code == 403


# ── 3. Plan switch mid-trial, webhook first ──────────────────────────────────

@pytest.mark.asyncio
async def test_switch_during_trial_via_webhook_first_becomes_paid(db_session):
    shop = _shop(plan_status="trial_active", plan_tier="growth", billing_cycle_start=NOW - D(2))
    shop.trial_used = True
    shop.trial_started_at = NOW - D(2)
    shop.trial_ends_at = NOW + D(5)
    shop.shopify_charge_id = "400"                        # the trial subscription
    db_session.add(shop)
    await db_session.commit()

    _webhook(db_session, "active", "401", name="GiftSense Pro Monthly Plan")   # new, paid
    await db_session.refresh(shop)
    assert (shop.plan_status, shop.plan_tier, shop.shopify_charge_id) == ("active", "pro", "401")
    cycle = shop.billing_cycle_start.replace(tzinfo=timezone.utc) if shop.billing_cycle_start.tzinfo is None else shop.billing_cycle_start
    assert NOW - D(1) < cycle <= datetime.now(timezone.utc)

    # The callback for the same charge then no-ops (replay guard) and stays paid.
    from tests.integration.test_billing import _graphql_subscription_response
    with patch("app.routes.billing.httpx.AsyncClient") as cls:
        http = AsyncMock()
        http.__aenter__ = AsyncMock(return_value=http)
        http.__aexit__ = AsyncMock(return_value=False)
        http.post = AsyncMock(return_value=_graphql_subscription_response("ACTIVE", name="GiftSense Pro Monthly Plan"))
        cls.return_value = http
        for client in _client(db_session):
            client.get(f"/billing/callback?shop={TEST_SHOP_DOMAIN}&charge_id=401&plan=pro", follow_redirects=False)
    await db_session.refresh(shop)
    assert shop.plan_status == "active"


@pytest.mark.asyncio
async def test_initial_trial_confirmation_still_keeps_the_trial(db_session):
    shop = _shop(plan_status="trial_active", plan_tier="growth", billing_cycle_start=NOW - D(1))
    shop.trial_used = True
    shop.trial_ends_at = NOW + D(6)
    shop.shopify_charge_id = "500"
    db_session.add(shop)
    await db_session.commit()
    _webhook(db_session, "active", "500")                 # same subscription
    await db_session.refresh(shop)
    assert shop.plan_status == "trial_active"


# ── 4. Scheduled change cleared on uninstall / reinstall ─────────────────────

@pytest.mark.asyncio
async def test_uninstall_clears_a_scheduled_change(db_session):
    shop = _shop(plan_status="active", plan_tier="pro", billing_cycle_start=NOW - D(5))
    shop.scheduled_plan_tier = "growth"
    shop.scheduled_change_at = NOW + D(25)
    db_session.add(shop)
    await db_session.commit()
    from app.routes.webhooks import _handle_uninstalled
    await _handle_uninstalled(TEST_SHOP_DOMAIN, db_session, authoritative=True)
    await db_session.refresh(shop)
    assert shop.scheduled_plan_tier is None and shop.scheduled_change_at is None


@pytest.mark.asyncio
async def test_reinstall_clears_a_leftover_scheduled_change(db_session):
    shop = _shop(plan_status="uninstalled", plan_tier="none")
    shop.access_token_encrypted = ""
    shop.scheduled_plan_tier = "growth"
    shop.scheduled_change_at = NOW - D(3)
    db_session.add(shop)
    await db_session.commit()
    from core.shopify_deps import _reactivate_shop_on_reinstall
    token_data = {"access_token": "t", "refresh_token": "r",
                  "access_token_expires_at": NOW + timedelta(hours=1), "refresh_token_expires_at": NOW + D(90)}
    with patch("core.shopify_deps.exchange_session_token_for_offline_token", AsyncMock(return_value=token_data)), \
         patch("core.shopify_deps._fetch_and_store_shop_meta", AsyncMock()):
        await _reactivate_shop_on_reinstall(shop, "jwt", db_session)
    assert shop.scheduled_plan_tier is None and shop.scheduled_change_at is None


# ── Downgrade scheduled in month 3 lands at the end of the CURRENT period ────

@pytest.mark.asyncio
async def test_downgrade_in_month_three_schedules_end_of_current_period(db_session):
    from tests.integration.test_billing import _graphql_subscription_response
    anchor = NOW - D(65)
    shop = _shop(plan_status="active", plan_tier="pro", billing_cycle_start=anchor)
    shop.trial_used = True
    shop.shopify_charge_id = "600"
    db_session.add(shop)
    await db_session.commit()
    with patch("app.routes.billing.httpx.AsyncClient") as cls:
        http = AsyncMock()
        http.__aenter__ = AsyncMock(return_value=http)
        http.__aexit__ = AsyncMock(return_value=False)
        http.post = AsyncMock(return_value=_graphql_subscription_response("ACTIVE", name="GiftSense Growth Monthly Plan"))
        cls.return_value = http
        for client in _client(db_session):
            client.get(f"/billing/callback?shop={TEST_SHOP_DOMAIN}&charge_id=601&plan=growth", follow_redirects=False)
    await db_session.refresh(shop)
    change = shop.scheduled_change_at.replace(tzinfo=timezone.utc) if shop.scheduled_change_at.tzinfo is None else shop.scheduled_change_at
    assert shop.scheduled_plan_tier == "growth"
    assert abs((change - (anchor + D(90))).total_seconds()) < 2       # was anchor + 30 (in the past)


# ── Repeat "active" webhook for the same live subscription ───────────────────

@pytest.mark.asyncio
async def test_mid_cycle_repeat_active_webhook_does_not_reset_quota(db_session):
    anchor = NOW - D(10)
    shop = _shop(plan_status="active", plan_tier="growth", billing_cycle_start=anchor)
    shop.shopify_charge_id = "700"
    db_session.add(shop)
    await db_session.commit()
    _webhook(db_session, "active", "700")
    await db_session.refresh(shop)
    cycle = shop.billing_cycle_start.replace(tzinfo=timezone.utc) if shop.billing_cycle_start.tzinfo is None else shop.billing_cycle_start
    assert abs((cycle - anchor).total_seconds()) < 2      # unchanged window
    assert shop.plan_status == "active"


@pytest.mark.asyncio
async def test_renewal_webhook_at_the_boundary_starts_the_new_period(db_session):
    anchor = NOW - D(30) - timedelta(minutes=5)          # just past the 30-day boundary
    shop = _shop(plan_status="active", plan_tier="growth", billing_cycle_start=anchor)
    shop.shopify_charge_id = "800"
    db_session.add(shop)
    await db_session.commit()
    _webhook(db_session, "active", "800")
    await db_session.refresh(shop)
    cycle = shop.billing_cycle_start.replace(tzinfo=timezone.utc) if shop.billing_cycle_start.tzinfo is None else shop.billing_cycle_start
    assert abs((cycle - (anchor + D(30))).total_seconds()) < 2


@pytest.mark.asyncio
async def test_resubscribe_after_cancel_starts_a_fresh_cycle(db_session):
    shop = _shop(plan_status="cancelled", plan_tier="growth", billing_cycle_start=NOW - D(20))
    shop.trial_used = True
    shop.shopify_charge_id = "900"
    db_session.add(shop)
    await db_session.commit()
    _webhook(db_session, "active", "901")
    await db_session.refresh(shop)
    cycle = shop.billing_cycle_start.replace(tzinfo=timezone.utc) if shop.billing_cycle_start.tzinfo is None else shop.billing_cycle_start
    assert shop.plan_status == "active" and cycle > NOW - timedelta(minutes=1)


# ── Re-subscribe via webhook first (cancelled / expired store) ───────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("trial_used,expected", [(False, "trial_active"), (True, "active")])
async def test_resubscribe_webhook_first_mirrors_the_callback_trial_rule(db_session, monkeypatch, trial_used, expected):
    from core import config as core_config
    monkeypatch.setattr(core_config.settings, "install_notify_email", "team@example.com")
    shop = _shop(plan_status="expired", plan_tier="growth", billing_cycle_start=NOW - D(40))
    shop.trial_used = trial_used
    shop.shopify_charge_id = "1000"
    db_session.add(shop)
    await db_session.commit()
    send = AsyncMock()
    payload = {"app_subscription": {"admin_graphql_api_id": "gid://shopify/AppSubscription/1001",
                                    "status": "active", "name": "GiftSense Growth Monthly Plan"}}
    body = _make_webhook_body("app_subscriptions/update", payload)
    with patch("app.services.install_notify.send_email", send):
        for client in _client(db_session):
            client.post("/webhooks", content=body, headers=_headers(body, "app_subscriptions/update"))
        from app.services import install_notify
        import asyncio
        while install_notify._pending:
            await asyncio.gather(*list(install_notify._pending))
    await db_session.refresh(shop)
    assert shop.plan_status == expected and shop.trial_used is True
    send.assert_awaited_once()
    assert "Returning merchant" in send.await_args.kwargs["subject"] or not trial_used


@pytest.mark.asyncio
async def test_cancelled_then_active_out_of_order_plan_switch_sends_no_team_email(db_session, monkeypatch):
    """Plan switch where Shopify delivers the old CANCELLED before the new ACTIVE
    and the callback is late: billing ends correct, no misleading email."""
    from core import config as core_config
    monkeypatch.setattr(core_config.settings, "install_notify_email", "team@example.com")
    shop = _shop(plan_status="active", plan_tier="growth", billing_cycle_start=NOW - D(10))
    shop.trial_used = True
    shop.shopify_charge_id = "1200"
    db_session.add(shop)
    await db_session.commit()
    send = AsyncMock()
    for status, charge, name in [("cancelled", "1200", "GiftSense Growth Monthly Plan"),
                                 ("active", "1201", "GiftSense Pro Monthly Plan")]:
        payload = {"app_subscription": {"admin_graphql_api_id": f"gid://shopify/AppSubscription/{charge}",
                                        "status": status, "name": name}}
        body = _make_webhook_body("app_subscriptions/update", payload)
        with patch("app.services.install_notify.send_email", send):
            for client in _client(db_session):
                client.post("/webhooks", content=body, headers=_headers(body, "app_subscriptions/update"))
    await db_session.refresh(shop)
    assert (shop.plan_status, shop.plan_tier, shop.shopify_charge_id) == ("active", "pro", "1201")
    send.assert_not_awaited()
