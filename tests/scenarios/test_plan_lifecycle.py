"""
Plan lifecycle scenario tests.

Covers:
  - Full install flow: shop saved with plan_tier="growth", billing activates
  - Reinstall trial guard: second install never gets a trial
  - Stale uninstall webhook after reinstall is ignored
  - Plan upgrade (Starter → Growth → Pro) via webhook
  - Plan downgrade (Pro → Growth → Starter) via webhook
  - Uninstall during trial
  - Uninstall after upgrade with usage
  - Uninstall after downgrade with usage
  - Auth security: expired nonce, HMAC mismatch, stale timestamp
  - Root path routing: install redirect vs embedded passthrough
  - Billing callback declined
  - Billing callback empty status (race condition) leaves shop pending
"""

import base64
import hashlib
import hmac
import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.config import GRACE_PERIOD_DAYS
from core.db.models import BillingEvent, Shop, UsageLog
from core.db.session import get_db
from core.shopify_auth import decrypt_token, encrypt_token
from tests.conftest import (
    TEST_API_SECRET,
    TEST_SHOP_DOMAIN,
    add_usage,
    make_shop,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_client(db_session):
    async def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app, raise_server_exceptions=True)
    yield client
    app.dependency_overrides.clear()


def _sign_webhook(body: bytes) -> str:
    digest = hmac.new(TEST_API_SECRET.encode(), body, hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


def _webhook_headers(body: bytes, topic: str, shop: str = TEST_SHOP_DOMAIN,
                     webhook_id: str | None = None) -> dict:
    return {
        "X-Shopify-Topic": topic,
        "X-Shopify-Hmac-Sha256": _sign_webhook(body),
        "X-Shopify-Webhook-Id": webhook_id or str(uuid.uuid4()),
        "X-Shopify-Shop-Domain": shop,
        "Content-Type": "application/json",
    }


def _subscription_webhook(status: str, name: str = "Prudix Growth Plan",
                           charge_id: str = "gid://shopify/AppSubscription/1") -> bytes:
    return json.dumps({
        "app_subscription": {
            "admin_graphql_api_id": charge_id,
            "name": name,
            "status": status,
        }
    }).encode()


def _graphql_subscription_response(status: str) -> MagicMock:
    return MagicMock(
        status_code=200,
        json=lambda: {"data": {"node": {"id": "gid://shopify/AppSubscription/1", "status": status}}},
    )


def _billing_client_mock(status: str = "active"):
    mock = AsyncMock()
    mock.__aenter__ = AsyncMock(return_value=mock)
    mock.__aexit__ = AsyncMock(return_value=False)
    mock.post = AsyncMock(return_value=_graphql_subscription_response(status))
    return mock


# ── Full install flow ─────────────────────────────────────────────────────────

class TestFullInstallFlow:

    @pytest.mark.asyncio
    async def test_fresh_install_sets_plan_tier_growth(self, db_session):
        """
        billing/callback (active) on fresh install → plan_tier=growth, trial_active,
        trial_used=True, billing_cycle_start set.
        """
        shop = make_shop(plan_tier="growth", plan_status="pending")
        shop.trial_used = False
        shop.billing_cycle_start = None
        shop.access_token_encrypted = encrypt_token("tok")
        db_session.add(shop)
        await db_session.commit()

        with patch("app.routes.billing.httpx.AsyncClient", return_value=_billing_client_mock("pending")):
            for client in _make_client(db_session):
                client.get(
                    f"/billing/callback?shop={TEST_SHOP_DOMAIN}&charge_id=1&plan=growth",
                    follow_redirects=False,
                )

        await db_session.refresh(shop)
        assert shop.plan_tier == "growth"
        assert shop.plan_status == "trial_active"
        assert shop.trial_used is True
        assert shop.billing_cycle_start is not None
        assert shop.trial_ends_at is not None

    @pytest.mark.asyncio
    async def test_fresh_install_creates_trial_started_billing_event(self, db_session):
        shop = make_shop(plan_tier="growth", plan_status="pending")
        shop.trial_used = False
        shop.billing_cycle_start = None
        shop.access_token_encrypted = encrypt_token("tok")
        db_session.add(shop)
        await db_session.commit()

        with patch("app.routes.billing.httpx.AsyncClient", return_value=_billing_client_mock("pending")):
            for client in _make_client(db_session):
                client.get(
                    f"/billing/callback?shop={TEST_SHOP_DOMAIN}&charge_id=1&plan=growth",
                    follow_redirects=False,
                )

        result = await db_session.execute(
            select(BillingEvent).where(BillingEvent.shop_id == shop.id)
        )
        event = result.scalar_one_or_none()
        assert event is not None
        assert event.event_type == "trial_started"
        assert event.plan_tier == "growth"

    @pytest.mark.asyncio
    async def test_webhook_activation_sets_plan_tier_from_subscription_name(self, db_session):
        """
        app_subscriptions/update webhook with status=active and name containing
        'Growth' must set shop.plan_tier = 'growth' even if it was 'none'.
        """
        shop = make_shop(plan_tier="none", plan_status="pending")
        shop.billing_cycle_start = None
        db_session.add(shop)
        await db_session.commit()

        body = _subscription_webhook("active", "Prudix Growth Plan")
        headers = _webhook_headers(body, "app_subscriptions/update")

        for client in _make_client(db_session):
            resp = client.post("/webhooks", content=body, headers=headers)
        assert resp.status_code == 200

        await db_session.refresh(shop)
        assert shop.plan_tier == "growth"
        assert shop.plan_status == "active"
        assert shop.billing_cycle_start is not None
        assert shop.shopify_charge_id is not None   # charge GID from webhook payload
        assert shop.grace_period_ends_at is None

        result = await db_session.execute(
            select(BillingEvent).where(BillingEvent.shop_id == shop.id)
        )
        event = result.scalar_one_or_none()
        assert event is not None
        assert event.plan_tier == "growth"
        assert event.shopify_charge_id is not None
        assert event.created_at is not None


# ── Reinstall / trial guard ───────────────────────────────────────────────────

class TestReinstallTrialGuard:

    @pytest.mark.asyncio
    async def test_reinstall_does_not_get_second_trial(self, db_session):
        """trial_used=True on reinstall → billing/callback must set active, not trial_active."""
        shop = make_shop(plan_tier="growth", plan_status="pending")
        shop.trial_used = True
        shop.access_token_encrypted = encrypt_token("tok")
        db_session.add(shop)
        await db_session.commit()

        with patch("app.routes.billing.httpx.AsyncClient", return_value=_billing_client_mock("active")):
            for client in _make_client(db_session):
                client.get(
                    f"/billing/callback?shop={TEST_SHOP_DOMAIN}&charge_id=1&plan=growth",
                    follow_redirects=False,
                )

        await db_session.refresh(shop)
        assert shop.plan_status == "active"
        assert shop.trial_used is True         # preserved — never changes
        assert shop.trial_started_at is None   # trial never started on this reinstall
        assert shop.trial_ends_at is None
        assert shop.billing_cycle_start is not None
        assert shop.shopify_charge_id == "1"   # charge_id from URL param
        assert shop.grace_period_ends_at is None

        result = await db_session.execute(
            select(BillingEvent).where(BillingEvent.shop_id == shop.id)
        )
        event = result.scalar_one_or_none()
        assert event is not None
        assert event.event_type == "activated"   # no trial → straight activated
        assert event.plan_tier == "growth"
        assert event.shopify_charge_id == "1"

    @pytest.mark.asyncio
    async def test_stale_uninstall_webhook_ignored_after_reinstall(self, db_session):
        """
        Shopify sometimes delivers a delayed app/uninstalled webhook after a reinstall.
        The guard (installed_at within 300s) must ignore it so the new install isn't wiped.
        """
        shop = make_shop(plan_status="active")
        shop.access_token_encrypted = encrypt_token("new_tok")
        # installed_at is very recent (within the 300s guard window)
        shop.installed_at = datetime.now(timezone.utc) - timedelta(seconds=30)
        db_session.add(shop)
        await db_session.commit()

        body = json.dumps({"shop_domain": TEST_SHOP_DOMAIN}).encode()
        headers = _webhook_headers(body, "app/uninstalled")

        for client in _make_client(db_session):
            resp = client.post("/webhooks", content=body, headers=headers)
        assert resp.status_code == 200

        await db_session.refresh(shop)
        # Shop must NOT be uninstalled — stale webhook ignored (all fields unchanged)
        assert shop.plan_status == "active"
        assert shop.plan_tier == "growth"
        # Fernet is non-deterministic; compare decrypted plaintext instead
        assert decrypt_token(shop.access_token_encrypted) == "new_tok"
        assert shop.uninstalled_at is None
        assert shop.data_purge_at is None

    @pytest.mark.asyncio
    async def test_real_uninstall_webhook_not_ignored_after_300s(self, db_session):
        """Uninstall webhook that arrives > 300s after install must be processed."""
        shop = make_shop(plan_status="active")
        shop.installed_at = datetime.now(timezone.utc) - timedelta(seconds=400)
        shop.access_token_encrypted = encrypt_token("tok")
        db_session.add(shop)
        await db_session.commit()

        body = json.dumps({"shop_domain": TEST_SHOP_DOMAIN}).encode()
        headers = _webhook_headers(body, "app/uninstalled")

        for client in _make_client(db_session):
            client.post("/webhooks", content=body, headers=headers)

        await db_session.refresh(shop)
        # Full uninstall field audit
        assert shop.plan_status == "uninstalled"
        assert shop.plan_tier == "none"
        assert shop.access_token_encrypted == ""
        assert shop.refresh_token_encrypted is None
        assert shop.access_token_expires_at is None
        assert shop.refresh_token_expires_at is None
        assert shop.shopify_charge_id is None
        assert shop.billing_cycle_start is None
        assert shop.trial_started_at is None
        assert shop.trial_ends_at is None
        assert shop.grace_period_ends_at is None
        assert shop.uninstalled_at is not None
        assert shop.data_purge_at is not None


# ── Plan upgrade ──────────────────────────────────────────────────────────────

class TestPlanUpgrade:

    @pytest.mark.asyncio
    async def test_starter_to_growth_upgrade(self, db_session):
        """Upgrading from Starter to Growth via subscription webhook."""
        old_cycle = datetime.now(timezone.utc) - timedelta(days=10)
        shop = make_shop(plan_tier="starter", plan_status="active", billing_cycle_start=old_cycle)
        db_session.add(shop)
        await db_session.commit()

        body = _subscription_webhook("active", "Prudix Growth Plan", "gid://shopify/AppSubscription/200")
        headers = _webhook_headers(body, "app_subscriptions/update")

        for client in _make_client(db_session):
            resp = client.post("/webhooks", content=body, headers=headers)
        assert resp.status_code == 200

        await db_session.refresh(shop)
        assert shop.plan_tier == "growth"
        assert shop.plan_status == "active"
        # Billing cycle resets on upgrade
        new_cycle = shop.billing_cycle_start.replace(tzinfo=None) if shop.billing_cycle_start.tzinfo else shop.billing_cycle_start
        assert new_cycle > old_cycle.replace(tzinfo=None)
        # Charge ID updated to the new subscription
        assert shop.shopify_charge_id == "200"
        assert shop.grace_period_ends_at is None

        result = await db_session.execute(
            select(BillingEvent).where(BillingEvent.shop_id == shop.id)
        )
        event = result.scalar_one_or_none()
        assert event is not None
        assert event.plan_tier == "growth"
        assert event.event_type == "renewed"   # had billing_cycle_start → renewed
        assert event.shopify_charge_id == "200"
        assert event.created_at is not None

    @pytest.mark.asyncio
    async def test_growth_to_pro_upgrade(self, db_session):
        old_cycle = datetime.now(timezone.utc) - timedelta(days=10)
        shop = make_shop(plan_tier="growth", plan_status="active", billing_cycle_start=old_cycle)
        db_session.add(shop)
        await db_session.commit()

        body = _subscription_webhook("active", "Prudix Pro Plan", "gid://shopify/AppSubscription/300")
        headers = _webhook_headers(body, "app_subscriptions/update")

        for client in _make_client(db_session):
            client.post("/webhooks", content=body, headers=headers)

        await db_session.refresh(shop)
        assert shop.plan_tier == "pro"
        assert shop.plan_status == "active"
        # Billing cycle resets on upgrade
        new_cycle = shop.billing_cycle_start.replace(tzinfo=None) if shop.billing_cycle_start.tzinfo else shop.billing_cycle_start
        assert new_cycle > old_cycle.replace(tzinfo=None)
        assert shop.shopify_charge_id == "300"
        assert shop.grace_period_ends_at is None

        result = await db_session.execute(
            select(BillingEvent).where(BillingEvent.shop_id == shop.id)
        )
        event = result.scalar_one_or_none()
        assert event is not None
        assert event.plan_tier == "pro"
        assert event.event_type == "renewed"
        assert event.shopify_charge_id == "300"

    @pytest.mark.asyncio
    async def test_upgrade_billing_event_recorded_with_new_tier(self, db_session):
        shop = make_shop(plan_tier="starter", plan_status="active")
        db_session.add(shop)
        await db_session.commit()

        body = _subscription_webhook("active", "Prudix Growth Plan", "gid://shopify/AppSubscription/201")
        headers = _webhook_headers(body, "app_subscriptions/update")

        for client in _make_client(db_session):
            client.post("/webhooks", content=body, headers=headers)

        result = await db_session.execute(
            select(BillingEvent).where(BillingEvent.shop_id == shop.id)
        )
        event = result.scalar_one_or_none()
        assert event is not None
        assert event.plan_tier == "growth"
        assert event.event_type == "renewed"   # had existing billing_cycle_start
        assert event.shopify_charge_id == "201"
        assert event.created_at is not None

    @pytest.mark.asyncio
    async def test_usage_before_upgrade_not_counted_in_new_cycle(self, db_session):
        """Usage logged before the upgrade must not count against the new billing cycle."""
        old_cycle = datetime.now(timezone.utc) - timedelta(days=15)
        shop = make_shop(plan_tier="starter", plan_status="active", billing_cycle_start=old_cycle)
        db_session.add(shop)
        await db_session.commit()

        # Add 50 generations in the old Starter cycle
        await add_usage(db_session, shop, 50, created_at=datetime.now(timezone.utc) - timedelta(days=5))

        # Upgrade fires the webhook
        body = _subscription_webhook("active", "Prudix Growth Plan")
        headers = _webhook_headers(body, "app_subscriptions/update")

        for client in _make_client(db_session):
            client.post("/webhooks", content=body, headers=headers)

        await db_session.refresh(shop)
        new_cycle_start = shop.billing_cycle_start

        # Usage before new cycle start must not be counted
        result = await db_session.execute(
            select(UsageLog).where(
                UsageLog.shop_id == shop.id,
                UsageLog.created_at >= new_cycle_start,
            )
        )
        assert len(result.scalars().all()) == 0


# ── Plan downgrade ────────────────────────────────────────────────────────────

class TestPlanDowngrade:

    @pytest.mark.asyncio
    async def test_pro_to_growth_downgrade(self, db_session):
        """Downgrading Pro → Growth via subscription webhook."""
        old_cycle = datetime.now(timezone.utc) - timedelta(days=10)
        shop = make_shop(plan_tier="pro", plan_status="active", billing_cycle_start=old_cycle)
        db_session.add(shop)
        await db_session.commit()

        body = _subscription_webhook("active", "Prudix Growth Plan", "gid://shopify/AppSubscription/400")
        headers = _webhook_headers(body, "app_subscriptions/update")

        for client in _make_client(db_session):
            client.post("/webhooks", content=body, headers=headers)

        await db_session.refresh(shop)
        assert shop.plan_tier == "growth"
        assert shop.plan_status == "active"
        # Billing cycle resets on downgrade (same as upgrade — new subscription)
        new_cycle = shop.billing_cycle_start.replace(tzinfo=None) if shop.billing_cycle_start.tzinfo else shop.billing_cycle_start
        assert new_cycle > old_cycle.replace(tzinfo=None)
        assert shop.shopify_charge_id == "400"
        assert shop.grace_period_ends_at is None

        result = await db_session.execute(
            select(BillingEvent).where(BillingEvent.shop_id == shop.id)
        )
        event = result.scalar_one_or_none()
        assert event is not None
        assert event.plan_tier == "growth"
        assert event.event_type == "renewed"
        assert event.shopify_charge_id == "400"

    @pytest.mark.asyncio
    async def test_growth_to_starter_downgrade(self, db_session):
        old_cycle = datetime.now(timezone.utc) - timedelta(days=10)
        shop = make_shop(plan_tier="growth", plan_status="active", billing_cycle_start=old_cycle)
        db_session.add(shop)
        await db_session.commit()

        body = _subscription_webhook("active", "Prudix Starter Plan", "gid://shopify/AppSubscription/401")
        headers = _webhook_headers(body, "app_subscriptions/update")

        for client in _make_client(db_session):
            client.post("/webhooks", content=body, headers=headers)

        await db_session.refresh(shop)
        assert shop.plan_tier == "starter"
        assert shop.plan_status == "active"
        new_cycle = shop.billing_cycle_start.replace(tzinfo=None) if shop.billing_cycle_start.tzinfo else shop.billing_cycle_start
        assert new_cycle > old_cycle.replace(tzinfo=None)
        assert shop.shopify_charge_id == "401"
        assert shop.grace_period_ends_at is None

        result = await db_session.execute(
            select(BillingEvent).where(BillingEvent.shop_id == shop.id)
        )
        event = result.scalar_one_or_none()
        assert event is not None
        assert event.plan_tier == "starter"
        assert event.event_type == "renewed"
        assert event.shopify_charge_id == "401"

    @pytest.mark.asyncio
    async def test_usage_before_downgrade_not_counted_in_new_cycle(self, db_session):
        """Usage from the Pro cycle must not bleed into the new Starter cycle after downgrade."""
        old_cycle = datetime.now(timezone.utc) - timedelta(days=20)
        shop = make_shop(plan_tier="pro", plan_status="active", billing_cycle_start=old_cycle)
        db_session.add(shop)
        await db_session.commit()

        await add_usage(db_session, shop, 300, created_at=datetime.now(timezone.utc) - timedelta(days=5))

        body = _subscription_webhook("active", "Prudix Starter Plan")
        headers = _webhook_headers(body, "app_subscriptions/update")

        for client in _make_client(db_session):
            client.post("/webhooks", content=body, headers=headers)

        await db_session.refresh(shop)
        result = await db_session.execute(
            select(UsageLog).where(
                UsageLog.shop_id == shop.id,
                UsageLog.created_at >= shop.billing_cycle_start,
            )
        )
        assert len(result.scalars().all()) == 0


# ── Uninstall during trial ────────────────────────────────────────────────────

class TestUninstallDuringTrial:

    @pytest.mark.asyncio
    async def test_uninstall_during_trial_marks_uninstalled(self, db_session):
        """Uninstalling during trial: shop becomes uninstalled, token cleared."""
        trial_ends = datetime.now(timezone.utc) + timedelta(days=5)
        shop = make_shop(plan_tier="growth", plan_status="trial_active", trial_ends_at=trial_ends)
        shop.trial_used = True
        shop.access_token_encrypted = encrypt_token("trial_tok")
        shop.installed_at = datetime.now(timezone.utc) - timedelta(days=2)
        db_session.add(shop)
        await db_session.commit()

        body = json.dumps({"shop_domain": TEST_SHOP_DOMAIN}).encode()
        headers = _webhook_headers(body, "app/uninstalled")

        for client in _make_client(db_session):
            resp = client.post("/webhooks", content=body, headers=headers)
        assert resp.status_code == 200

        await db_session.refresh(shop)
        # Status
        assert shop.plan_status == "uninstalled"
        assert shop.plan_tier == "none"
        # Data retention window set
        assert shop.data_purge_at is not None
        # All credentials cleared
        assert shop.access_token_encrypted == ""
        assert shop.refresh_token_encrypted is None
        assert shop.access_token_expires_at is None
        assert shop.refresh_token_expires_at is None
        # Billing state cleared
        assert shop.shopify_charge_id is None
        assert shop.billing_cycle_start is None
        assert shop.trial_started_at is None
        assert shop.trial_ends_at is None
        assert shop.grace_period_ends_at is None
        # Timestamps
        assert shop.uninstalled_at is not None

    @pytest.mark.asyncio
    async def test_uninstall_during_trial_trial_used_flag_preserved(self, db_session):
        """trial_used must remain True after uninstall — prevents re-trial on reinstall."""
        trial_ends = datetime.now(timezone.utc) + timedelta(days=5)
        shop = make_shop(plan_tier="growth", plan_status="trial_active", trial_ends_at=trial_ends)
        shop.trial_used = True
        shop.installed_at = datetime.now(timezone.utc) - timedelta(days=2)
        db_session.add(shop)
        await db_session.commit()

        body = json.dumps({"shop_domain": TEST_SHOP_DOMAIN}).encode()
        headers = _webhook_headers(body, "app/uninstalled")

        for client in _make_client(db_session):
            client.post("/webhooks", content=body, headers=headers)

        await db_session.refresh(shop)
        assert shop.trial_used is True

    @pytest.mark.asyncio
    async def test_reinstall_after_trial_uninstall_gets_no_trial(self, db_session):
        """
        Merchant uninstalled during trial → reinstalls → trial_used=True
        → billing/callback must activate as paid, not trial.
        """
        shop = make_shop(plan_tier="growth", plan_status="uninstalled")
        shop.trial_used = True
        shop.access_token_encrypted = encrypt_token("new_tok")
        db_session.add(shop)
        await db_session.commit()

        with patch("app.routes.billing.httpx.AsyncClient", return_value=_billing_client_mock("active")):
            for client in _make_client(db_session):
                client.get(
                    f"/billing/callback?shop={TEST_SHOP_DOMAIN}&charge_id=99&plan=growth",
                    follow_redirects=False,
                )

        await db_session.refresh(shop)
        assert shop.plan_status == "active"
        assert shop.trial_ends_at is None


# ── Uninstall after upgrade with usage ───────────────────────────────────────

class TestUninstallAfterUpgrade:

    @pytest.mark.asyncio
    async def test_uninstall_after_upgrade_clears_token_preserves_usage(self, db_session):
        """
        Merchant upgraded to Pro, used 10 days, then uninstalled.
        Token must be cleared; usage logs preserved until data_purge_at.
        """
        cycle_start = datetime.now(timezone.utc) - timedelta(days=10)
        shop = make_shop(plan_tier="pro", plan_status="active", billing_cycle_start=cycle_start)
        shop.access_token_encrypted = encrypt_token("pro_tok")
        shop.installed_at = datetime.now(timezone.utc) - timedelta(days=10)
        db_session.add(shop)
        await db_session.commit()

        await add_usage(db_session, shop, 200)

        body = json.dumps({"shop_domain": TEST_SHOP_DOMAIN}).encode()
        headers = _webhook_headers(body, "app/uninstalled")

        for client in _make_client(db_session):
            resp = client.post("/webhooks", content=body, headers=headers)
        assert resp.status_code == 200

        await db_session.refresh(shop)
        # Status
        assert shop.plan_status == "uninstalled"
        assert shop.plan_tier == "none"
        assert shop.uninstalled_at is not None
        assert shop.data_purge_at is not None
        # Credentials cleared
        assert shop.access_token_encrypted == ""
        assert shop.refresh_token_encrypted is None
        assert shop.access_token_expires_at is None
        assert shop.refresh_token_expires_at is None
        # Billing cleared
        assert shop.shopify_charge_id is None
        assert shop.billing_cycle_start is None
        assert shop.trial_started_at is None
        assert shop.trial_ends_at is None
        assert shop.grace_period_ends_at is None

        # Usage logs must still be present (not purged until data_purge_at)
        result = await db_session.execute(
            select(UsageLog).where(UsageLog.shop_id == shop.id)
        )
        assert len(result.scalars().all()) == 1

    @pytest.mark.asyncio
    async def test_uninstall_after_upgrade_data_purge_at_is_30_days(self, db_session):
        shop = make_shop(plan_tier="pro", plan_status="active")
        shop.installed_at = datetime.now(timezone.utc) - timedelta(days=15)
        db_session.add(shop)
        await db_session.commit()

        body = json.dumps({"shop_domain": TEST_SHOP_DOMAIN}).encode()
        headers = _webhook_headers(body, "app/uninstalled")

        for client in _make_client(db_session):
            client.post("/webhooks", content=body, headers=headers)

        await db_session.refresh(shop)
        now = datetime.now(timezone.utc)
        purge = shop.data_purge_at.replace(tzinfo=timezone.utc) if shop.data_purge_at.tzinfo is None else shop.data_purge_at
        days_until_purge = (purge - now).days
        assert 29 <= days_until_purge <= 30


# ── Uninstall after downgrade with usage ──────────────────────────────────────

class TestUninstallAfterDowngrade:

    @pytest.mark.asyncio
    async def test_uninstall_after_downgrade_clears_token(self, db_session):
        """
        Merchant downgraded from Pro to Growth, used 5 days, then uninstalled.
        Token cleared, data_purge_at set.
        """
        cycle_start = datetime.now(timezone.utc) - timedelta(days=5)
        shop = make_shop(plan_tier="growth", plan_status="active", billing_cycle_start=cycle_start)
        shop.access_token_encrypted = encrypt_token("downgraded_tok")
        shop.installed_at = datetime.now(timezone.utc) - timedelta(days=35)
        db_session.add(shop)
        await db_session.commit()

        await add_usage(db_session, shop, 50)

        body = json.dumps({"shop_domain": TEST_SHOP_DOMAIN}).encode()
        headers = _webhook_headers(body, "app/uninstalled")

        for client in _make_client(db_session):
            resp = client.post("/webhooks", content=body, headers=headers)
        assert resp.status_code == 200

        await db_session.refresh(shop)
        assert shop.plan_status == "uninstalled"
        assert shop.plan_tier == "none"
        assert shop.uninstalled_at is not None
        assert shop.data_purge_at is not None
        assert shop.access_token_encrypted == ""
        assert shop.refresh_token_encrypted is None
        assert shop.shopify_charge_id is None
        assert shop.billing_cycle_start is None
        assert shop.trial_started_at is None
        assert shop.trial_ends_at is None
        assert shop.grace_period_ends_at is None

    @pytest.mark.asyncio
    async def test_reinstall_after_downgrade_uninstall_gets_no_trial(self, db_session):
        """Merchant who downgraded then uninstalled still cannot get another trial."""
        shop = make_shop(plan_tier="growth", plan_status="uninstalled")
        shop.trial_used = True
        shop.access_token_encrypted = encrypt_token("tok2")
        db_session.add(shop)
        await db_session.commit()

        with patch("app.routes.billing.httpx.AsyncClient", return_value=_billing_client_mock("active")):
            for client in _make_client(db_session):
                client.get(
                    f"/billing/callback?shop={TEST_SHOP_DOMAIN}&charge_id=55&plan=growth",
                    follow_redirects=False,
                )

        await db_session.refresh(shop)
        assert shop.plan_status == "active"
        assert shop.trial_ends_at is None


# ── Auth security ─────────────────────────────────────────────────────────────

class TestAuthSecurity:

    def test_auth_callback_invalid_hmac_returns_403(self, db_session):
        """HMAC mismatch in auth/callback must return 403."""
        params = (
            f"?shop={TEST_SHOP_DOMAIN}&code=abc&state=bad_state"
            f"&hmac=badhmacsignature&timestamp={int(time.time())}"
        )
        for client in _make_client(db_session):
            resp = client.get(f"/auth/callback{params}", follow_redirects=False)
        assert resp.status_code == 403

    def test_auth_callback_stale_timestamp_returns_403(self, db_session):
        """Timestamp older than 300s must be rejected."""
        stale_ts = int(time.time()) - 400
        params = f"?shop={TEST_SHOP_DOMAIN}&code=abc&state=s&hmac=x&timestamp={stale_ts}"
        for client in _make_client(db_session):
            resp = client.get(f"/auth/callback{params}", follow_redirects=False)
        assert resp.status_code == 403

    def test_auth_callback_missing_nonce_returns_403(self, db_session):
        """No stored nonce (because /auth was never called) → 403."""
        ts = int(time.time())
        # Build a valid HMAC but with a state that was never stored
        raw_params = f"code=validcode&shop={TEST_SHOP_DOMAIN}&state=never_stored&timestamp={ts}"
        digest = hmac.new(TEST_API_SECRET.encode(), raw_params.encode(), hashlib.sha256).hexdigest()
        params = f"?shop={TEST_SHOP_DOMAIN}&code=validcode&state=never_stored&hmac={digest}&timestamp={ts}"
        for client in _make_client(db_session):
            resp = client.get(f"/auth/callback{params}", follow_redirects=False)
        assert resp.status_code == 403


# ── Root path routing ─────────────────────────────────────────────────────────

class TestRootPathRouting:

    def test_root_with_shop_and_no_embedded_redirects_to_auth(self, db_session):
        """GET /?shop=x&hmac=y (Shopify install redirect) must redirect to /auth."""
        for client in _make_client(db_session):
            resp = client.get(
                f"/?shop={TEST_SHOP_DOMAIN}&hmac=abc&timestamp=123",
                follow_redirects=False,
            )
        assert resp.status_code in (301, 302, 307, 308)
        assert "/auth" in resp.headers["location"]

    def test_root_with_embedded_serves_spa(self, db_session):
        """GET /?embedded=1&shop=x must serve index.html regardless of DB state."""
        for client in _make_client(db_session):
            resp = client.get(
                f"/?embedded=1&shop={TEST_SHOP_DOMAIN}&hmac=abc",
                follow_redirects=False,
            )
        assert resp.status_code == 200


# ── Billing callback edge cases ───────────────────────────────────────────────

class TestBillingCallbackEdgeCases:

    @pytest.mark.asyncio
    async def test_declined_billing_sets_declined_status(self, db_session):
        shop = make_shop(plan_tier="growth", plan_status="pending")
        shop.access_token_encrypted = encrypt_token("tok")
        db_session.add(shop)
        await db_session.commit()

        with patch("app.routes.billing.httpx.AsyncClient", return_value=_billing_client_mock("declined")):
            for client in _make_client(db_session):
                client.get(
                    f"/billing/callback?shop={TEST_SHOP_DOMAIN}&charge_id=1&plan=growth",
                    follow_redirects=False,
                )

        await db_session.refresh(shop)
        assert shop.plan_status == "declined"
        # Declined does NOT get a grace period (only cancelled/expired does)
        assert shop.grace_period_ends_at is None
        # Billing callback only sets plan_status on declined — nothing else changes
        assert shop.shopify_charge_id is None   # not set on declined
        assert shop.plan_tier == "growth"       # unchanged from fixture
        assert shop.data_purge_at is None
        assert shop.uninstalled_at is None

    @pytest.mark.asyncio
    async def test_subscription_found_on_retry_activates(self, db_session):
        """
        Race: the callback fires before Shopify's subscription is queryable
        (node null), then it appears on a retry → activate normally.
        """
        shop = make_shop(plan_tier="growth", plan_status="pending")
        shop.access_token_encrypted = encrypt_token("tok")
        db_session.add(shop)
        await db_session.commit()

        empty = MagicMock(status_code=200, json=lambda: {"data": {"node": None}})
        found = MagicMock(status_code=200, json=lambda: {"data": {"node": {
            "id": "gid://shopify/AppSubscription/1", "status": "ACTIVE",
            "name": "GiftSense Growth Monthly Plan",
        }}})
        mock = AsyncMock()
        mock.__aenter__ = AsyncMock(return_value=mock)
        mock.__aexit__ = AsyncMock(return_value=False)
        mock.post = AsyncMock(side_effect=[empty, found])

        with patch("app.routes.billing.httpx.AsyncClient", return_value=mock), \
             patch("app.routes.billing.asyncio.sleep", AsyncMock()):
            for client in _make_client(db_session):
                resp = client.get(
                    f"/billing/callback?shop={TEST_SHOP_DOMAIN}&charge_id=1&plan=growth",
                    follow_redirects=False,
                )

        assert resp.status_code in (301, 302, 307, 308)
        await db_session.refresh(shop)
        assert shop.plan_status == "trial_active"
        assert shop.plan_tier == "growth"

    @pytest.mark.asyncio
    async def test_subscription_never_found_does_not_activate(self, db_session):
        """
        SECURITY: a made-up charge_id also returns node null. Commerce activated
        optimistically here (tier from the plan= URL param), so anyone could get
        Pro for free with /billing/callback?charge_id=<anything>&plan=pro.
        GiftSense retries, then leaves the shop untouched; the
        app_subscriptions/update webhook activates genuine charges.
        """
        shop = make_shop(plan_tier="none", plan_status="pending")
        shop.access_token_encrypted = encrypt_token("tok")
        db_session.add(shop)
        await db_session.commit()

        empty = MagicMock(status_code=200, json=lambda: {"data": {"node": None}})
        mock = AsyncMock()
        mock.__aenter__ = AsyncMock(return_value=mock)
        mock.__aexit__ = AsyncMock(return_value=False)
        mock.post = AsyncMock(return_value=empty)

        with patch("app.routes.billing.httpx.AsyncClient", return_value=mock), \
             patch("app.routes.billing.asyncio.sleep", AsyncMock()):
            for client in _make_client(db_session):
                resp = client.get(
                    f"/billing/callback?shop={TEST_SHOP_DOMAIN}&charge_id=999999&plan=pro",
                    follow_redirects=False,
                )

        assert resp.status_code in (301, 302, 307, 308)
        await db_session.refresh(shop)
        assert shop.plan_status == "pending"
        assert shop.plan_tier == "none"
        assert shop.shopify_charge_id is None
        assert shop.trial_used is False


# ── Grace period: cancel webhook then attempt generation ─────────────────────

class TestGracePeriodGeneration:

    @pytest.mark.asyncio
    async def test_cancelled_webhook_sets_grace_then_generation_blocked(self, db_session):
        """
        Step 1: Active shop receives cancelled webhook → grace_period_ends_at set
        Step 2: Attempt generation → 403 (cancelled is not in GENERATE_STATUSES)
        Step 3: GET /api/stats (view only) → still works if grace period active
        """
        import json as _json
        import uuid as _uuid

        # Use an already-expired billing cycle so the paid period has ended.
        # The cancellation webhook then puts the shop into a grace-period-only state,
        # where viewing is allowed but generation is blocked.
        from datetime import timedelta as _td
        expired_cycle = datetime.now(timezone.utc) - _td(days=35)
        shop = make_shop(plan_tier="growth", plan_status="active", billing_cycle_start=expired_cycle)
        shop.shopify_charge_id = "100"
        shop.access_token_encrypted = encrypt_token("tok")
        db_session.add(shop)
        await db_session.commit()

        # --- Step 1: Cancellation webhook ---
        payload = _json.dumps({
            "app_subscription": {
                "admin_graphql_api_id": "gid://shopify/AppSubscription/100",
                "status": "cancelled",
            }
        }).encode()
        sig = _sign_webhook(payload)
        headers = {
            "X-Shopify-Topic": "app_subscriptions/update",
            "X-Shopify-Hmac-Sha256": sig,
            "X-Shopify-Webhook-Id": str(_uuid.uuid4()),
            "X-Shopify-Shop-Domain": TEST_SHOP_DOMAIN,
        }
        for client in _make_client(db_session):
            wh_resp = client.post("/webhooks", content=payload, headers=headers)
        assert wh_resp.status_code == 200

        await db_session.refresh(shop)
        assert shop.plan_status == "cancelled"
        assert shop.grace_period_ends_at is not None
        # Grace period must be in the future (paid period already ended, grace just started)
        grace = shop.grace_period_ends_at
        if grace.tzinfo is None:
            grace = grace.replace(tzinfo=timezone.utc)
        assert grace > datetime.now(timezone.utc)

        # --- Step 2: Generation blocked (past paid period, grace is view-only) ---
        # Commerce hit /api/generate here; GiftSense AI routes all go through
        # require_generation, so assert on it directly.
        from fastapi import HTTPException as _HTTPException
        from app.plan_guard import require_generation
        with pytest.raises(_HTTPException) as exc:
            await require_generation("gift_finder", TEST_SHOP_DOMAIN, db_session)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_grace_period_expired_blocks_view_routes(self, db_session):
        """
        A cancelled shop whose grace period has EXPIRED must be blocked
        even from view-only routes (require_feature raises 403).
        """
        from datetime import timedelta as _td
        grace_ends = datetime.now(timezone.utc) - _td(hours=1)
        expired_cycle = datetime.now(timezone.utc) - _td(days=35)
        shop = make_shop(
            plan_tier="growth",
            plan_status="cancelled",
            billing_cycle_start=expired_cycle,
            grace_period_ends_at=grace_ends,
        )
        db_session.add(shop)
        await db_session.commit()

        from app.plan_guard import require_feature
        from unittest.mock import AsyncMock as _AsyncMock, MagicMock as _MagicMock

        mock_db = _AsyncMock()
        result = _MagicMock()
        result.scalar_one_or_none.return_value = shop
        mock_db.execute.return_value = result

        import pytest as _pytest
        with _pytest.raises(Exception) as exc:
            await require_feature("gift_finder", shop.shop_domain, mock_db)
        assert exc.value.status_code == 403
