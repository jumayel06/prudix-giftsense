"""Tests for ARQ background cron jobs (copied from Prudix Commerce, plus
GiftSense's reconcile_trial_conversions safety net).
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import pytest
from sqlalchemy import select

from app.workers.main import reconcile_uninstalled_shops
from core.db.models import Shop
from tests.conftest import make_shop


class TestReconcileUninstalledShops:
    """Safety-net cron catches missed `app/uninstalled` webhooks by probing
    the Shopify Admin API — 401 means the merchant uninstalled but we
    never got the webhook."""

    def _mock_httpx(self, status_code: int):
        resp = MagicMock(status_code=status_code)
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.post = AsyncMock(return_value=resp)
        return client

    def _mock_httpx_sequence(self, status_codes: list[int]):
        """Client whose .post() returns responses with the given status codes in order."""
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.post = AsyncMock(side_effect=[MagicMock(status_code=code) for code in status_codes])
        return client

    @pytest.mark.asyncio
    async def test_two_401s_marks_shop_uninstalled(self, db_session):
        """Both probes return 401 → shop is genuinely uninstalled."""
        shop = make_shop(plan_status="active", plan_tier="growth")
        db_session.add(shop)
        await db_session.commit()

        with patch("app.workers.main.AsyncSessionLocal") as mock_session_cls, \
             patch("app.workers.main.get_valid_access_token", AsyncMock(return_value="token")), \
             patch("asyncio.sleep", AsyncMock()), \
             patch("httpx.AsyncClient", return_value=self._mock_httpx_sequence([401, 401])):
            mock_session_cls.return_value.__aenter__.return_value = db_session
            mock_session_cls.return_value.__aexit__.return_value = False
            await reconcile_uninstalled_shops(ctx={})

        refreshed = (await db_session.execute(
            select(Shop).where(Shop.id == shop.id)
        )).scalar_one()
        assert refreshed.plan_status == "uninstalled"
        assert refreshed.access_token_encrypted == ""

    @pytest.mark.asyncio
    async def test_transient_401_then_200_leaves_shop_active(self, db_session):
        """First probe 401, second probe 200 → transient auth blip, shop stays active."""
        shop = make_shop(plan_status="active", plan_tier="growth")
        db_session.add(shop)
        await db_session.commit()

        with patch("app.workers.main.AsyncSessionLocal") as mock_session_cls, \
             patch("app.workers.main.get_valid_access_token", AsyncMock(return_value="token")), \
             patch("asyncio.sleep", AsyncMock()), \
             patch("httpx.AsyncClient", return_value=self._mock_httpx_sequence([401, 200])):
            mock_session_cls.return_value.__aenter__.return_value = db_session
            mock_session_cls.return_value.__aexit__.return_value = False
            await reconcile_uninstalled_shops(ctx={})

        refreshed = (await db_session.execute(
            select(Shop).where(Shop.id == shop.id)
        )).scalar_one()
        assert refreshed.plan_status == "active"

    @pytest.mark.asyncio
    async def test_200_leaves_shop_active(self, db_session):
        shop = make_shop(plan_status="active", plan_tier="growth")
        db_session.add(shop)
        await db_session.commit()

        with patch("app.workers.main.AsyncSessionLocal") as mock_session_cls, \
             patch("app.workers.main.get_valid_access_token", AsyncMock(return_value="token")), \
             patch("httpx.AsyncClient", return_value=self._mock_httpx(200)):
            mock_session_cls.return_value.__aenter__.return_value = db_session
            mock_session_cls.return_value.__aexit__.return_value = False
            await reconcile_uninstalled_shops(ctx={})

        refreshed = (await db_session.execute(
            select(Shop).where(Shop.id == shop.id)
        )).scalar_one()
        assert refreshed.plan_status == "active"

    @pytest.mark.asyncio
    async def test_already_uninstalled_shop_skipped(self, db_session):
        """Shops with plan_status='uninstalled' shouldn't be probed at all —
        no wasted Shopify API calls."""
        shop = make_shop(plan_status="uninstalled", plan_tier="none")
        db_session.add(shop)
        await db_session.commit()

        client = self._mock_httpx(401)
        with patch("app.workers.main.AsyncSessionLocal") as mock_session_cls, \
             patch("app.workers.main.get_valid_access_token", AsyncMock(return_value="token")), \
             patch("httpx.AsyncClient", return_value=client):
            mock_session_cls.return_value.__aenter__.return_value = db_session
            mock_session_cls.return_value.__aexit__.return_value = False
            await reconcile_uninstalled_shops(ctx={})

        client.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_network_error_leaves_shop_alone(self, db_session):
        """Timeouts/network errors must NOT flip healthy shops to uninstalled."""
        shop = make_shop(plan_status="active", plan_tier="growth")
        db_session.add(shop)
        await db_session.commit()

        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.post = AsyncMock(side_effect=Exception("timeout"))
        with patch("app.workers.main.AsyncSessionLocal") as mock_session_cls, \
             patch("app.workers.main.get_valid_access_token", AsyncMock(return_value="token")), \
             patch("httpx.AsyncClient", return_value=client):
            mock_session_cls.return_value.__aenter__.return_value = db_session
            mock_session_cls.return_value.__aexit__.return_value = False
            await reconcile_uninstalled_shops(ctx={})

        refreshed = (await db_session.execute(
            select(Shop).where(Shop.id == shop.id)
        )).scalar_one()
        assert refreshed.plan_status == "active"


class TestReconcileScheduledPlanChanges:
    """Safety-net cron for deferred plan changes whose activation webhook was
    dropped. Reconciles a past-due `scheduled_change_at` against Shopify's live
    active subscription."""

    def _mock_httpx_active_sub(self, name: str | None):
        subs = [] if name is None else [{"id": "gid://shopify/AppSubscription/1", "name": name, "status": "ACTIVE"}]
        resp = MagicMock(
            status_code=200,
            json=lambda: {"data": {"currentAppInstallation": {"activeSubscriptions": subs}}},
        )
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.post = AsyncMock(return_value=resp)
        return client

    async def _run(self, db_session, client):
        from app.workers.main import reconcile_scheduled_plan_changes
        with patch("app.workers.main.AsyncSessionLocal") as mock_session_cls, \
             patch("app.workers.main.get_valid_access_token", AsyncMock(return_value="token")), \
             patch("httpx.AsyncClient", return_value=client):
            mock_session_cls.return_value.__aenter__.return_value = db_session
            mock_session_cls.return_value.__aexit__.return_value = False
            await reconcile_scheduled_plan_changes(ctx={})

    @pytest.mark.asyncio
    async def test_missed_webhook_applies_switch(self, db_session):
        from core.db.models import BillingEvent
        scheduled_at = datetime(2026, 9, 1, tzinfo=timezone.utc)  # well past due
        shop = make_shop(plan_tier="pro", plan_status="active", shopify_charge_id="c1")
        shop.scheduled_plan_tier = "growth"
        shop.scheduled_change_at = scheduled_at
        db_session.add(shop)
        await db_session.commit()

        # Shopify says the merchant is actually on Growth now — webhook was missed.
        await self._run(db_session, self._mock_httpx_active_sub("GiftSense Growth Monthly Plan"))

        s = (await db_session.execute(select(Shop).where(Shop.id == shop.id))).scalar_one()
        assert s.plan_tier == "growth"
        assert s.plan_status == "active"
        assert s.scheduled_plan_tier is None
        assert s.scheduled_change_at is None
        cycle = s.billing_cycle_start.replace(tzinfo=timezone.utc) if s.billing_cycle_start.tzinfo is None else s.billing_cycle_start
        assert cycle == scheduled_at  # anchored at the switch boundary
        events = (await db_session.execute(select(BillingEvent).where(BillingEvent.shop_id == shop.id))).scalars().all()
        assert any(e.event_type == "change_reconciled" and e.plan_tier == "growth" for e in events)

    @pytest.mark.asyncio
    async def test_already_applied_just_clears_schedule(self, db_session):
        from core.db.models import BillingEvent
        shop = make_shop(plan_tier="growth", plan_status="active", shopify_charge_id="c1")
        shop.scheduled_plan_tier = "growth"
        shop.scheduled_change_at = datetime(2026, 9, 1, tzinfo=timezone.utc)
        db_session.add(shop)
        await db_session.commit()

        # Webhook already flipped the tier; Shopify agrees it's Growth.
        await self._run(db_session, self._mock_httpx_active_sub("GiftSense Growth Monthly Plan"))

        s = (await db_session.execute(select(Shop).where(Shop.id == shop.id))).scalar_one()
        assert s.plan_tier == "growth"
        assert s.scheduled_plan_tier is None
        assert s.scheduled_change_at is None
        # No reconcile event — nothing to apply.
        events = (await db_session.execute(select(BillingEvent).where(BillingEvent.shop_id == shop.id))).scalars().all()
        assert not any(e.event_type == "change_reconciled" for e in events)

    @pytest.mark.asyncio
    async def test_no_active_sub_leaves_schedule(self, db_session):
        shop = make_shop(plan_tier="pro", plan_status="active", shopify_charge_id="c1")
        shop.scheduled_plan_tier = "growth"
        shop.scheduled_change_at = datetime(2026, 9, 1, tzinfo=timezone.utc)
        db_session.add(shop)
        await db_session.commit()

        # Shopify returns no active subscription (transient) → don't guess.
        await self._run(db_session, self._mock_httpx_active_sub(None))

        s = (await db_session.execute(select(Shop).where(Shop.id == shop.id))).scalar_one()
        assert s.plan_tier == "pro"
        assert s.scheduled_plan_tier == "growth"   # untouched, retried next run

    @pytest.mark.asyncio
    async def test_future_schedule_not_touched(self, db_session):
        future = datetime.now(timezone.utc) + timedelta(days=20)
        shop = make_shop(plan_tier="pro", plan_status="active", shopify_charge_id="c1")
        shop.scheduled_plan_tier = "growth"
        shop.scheduled_change_at = future
        db_session.add(shop)
        await db_session.commit()

        # Even if Shopify would answer, a not-yet-due schedule must be skipped.
        await self._run(db_session, self._mock_httpx_active_sub("GiftSense Growth Monthly Plan"))

        s = (await db_session.execute(select(Shop).where(Shop.id == shop.id))).scalar_one()
        assert s.plan_tier == "pro"
        assert s.scheduled_plan_tier == "growth"


class TestReconcileTrialConversions:
    """GiftSense addition: safety net for trials whose conversion/expiry webhook
    was missed (Commerce listed the missing reconcile as tech debt)."""

    def _mock_httpx_subs(self, subs: list[dict] | None, status_code: int = 200):
        payload = {"data": {"currentAppInstallation": {"activeSubscriptions": subs or []}}}
        resp = MagicMock(status_code=status_code, json=lambda: payload)
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.post = AsyncMock(return_value=resp)
        return client

    async def _run(self, db_session, client):
        from app.workers.main import reconcile_trial_conversions
        with patch("app.workers.main.AsyncSessionLocal") as mock_session_cls, \
             patch("app.workers.main.get_valid_access_token", AsyncMock(return_value="token")), \
             patch("httpx.AsyncClient", return_value=client):
            mock_session_cls.return_value.__aenter__.return_value = db_session
            mock_session_cls.return_value.__aexit__.return_value = False
            await reconcile_trial_conversions(ctx={})

    async def _trial_shop(self, db_session, *, ended_hours_ago: float, tier: str = "growth"):
        trial_end = datetime.now(timezone.utc) - timedelta(hours=ended_hours_ago)
        shop = make_shop(
            plan_tier=tier, plan_status="trial_active", shopify_charge_id="111",
            trial_used=True, trial_ends_at=trial_end,
            billing_cycle_start=trial_end - timedelta(days=7),
        )
        db_session.add(shop)
        await db_session.commit()
        return shop, trial_end

    async def _reload(self, db_session, shop):
        return (await db_session.execute(select(Shop).where(Shop.id == shop.id))).scalar_one()

    @pytest.mark.asyncio
    async def test_active_subscription_converts_trial(self, db_session, job_pool):
        from core.db.models import BillingEvent
        shop, trial_end = await self._trial_shop(db_session, ended_hours_ago=3)
        sub = {"id": "gid://shopify/AppSubscription/111", "name": "GiftSense Growth Monthly Plan", "status": "ACTIVE"}

        await self._run(db_session, self._mock_httpx_subs([sub]))

        s = await self._reload(db_session, shop)
        assert s.plan_status == "active"
        assert s.plan_tier == "growth"
        assert s.shopify_charge_id == "111"
        cycle = s.billing_cycle_start.replace(tzinfo=timezone.utc) if s.billing_cycle_start.tzinfo is None else s.billing_cycle_start
        assert cycle == trial_end  # paid cycle anchored at the real conversion time
        events = (await db_session.execute(select(BillingEvent).where(BillingEvent.shop_id == shop.id))).scalars().all()
        assert any(e.event_type == "trial_converted_reconciled" for e in events)
        assert job_pool.jobs == [("catalog_start_sync", str(shop.id), "trial_converted")]

    @pytest.mark.asyncio
    async def test_no_active_subscription_expires_trial_with_grace(self, db_session, job_pool):
        shop, _ = await self._trial_shop(db_session, ended_hours_ago=3)

        await self._run(db_session, self._mock_httpx_subs([]))

        s = await self._reload(db_session, shop)
        assert s.plan_status == "expired"
        assert job_pool.jobs == []  # nothing more is analyzed for an unpaid shop
        grace = s.grace_period_ends_at.replace(tzinfo=timezone.utc) if s.grace_period_ends_at.tzinfo is None else s.grace_period_ends_at
        assert grace > datetime.now(timezone.utc) + timedelta(days=6)

    @pytest.mark.asyncio
    async def test_api_error_leaves_trial_untouched(self, db_session):
        shop, _ = await self._trial_shop(db_session, ended_hours_ago=3)

        await self._run(db_session, self._mock_httpx_subs(None, status_code=500))

        s = await self._reload(db_session, shop)
        assert s.plan_status == "trial_active"

    @pytest.mark.asyncio
    async def test_trial_ended_under_an_hour_ago_not_touched(self, db_session):
        """Don't race the webhook that's probably arriving right at the boundary."""
        shop, _ = await self._trial_shop(db_session, ended_hours_ago=0.25)

        await self._run(db_session, self._mock_httpx_subs([]))

        s = await self._reload(db_session, shop)
        assert s.plan_status == "trial_active"

    @pytest.mark.asyncio
    async def test_running_trial_not_touched(self, db_session):
        trial_end = datetime.now(timezone.utc) + timedelta(days=3)
        shop = make_shop(plan_tier="growth", plan_status="trial_active", trial_ends_at=trial_end)
        db_session.add(shop)
        await db_session.commit()

        await self._run(db_session, self._mock_httpx_subs([]))

        s = await self._reload(db_session, shop)
        assert s.plan_status == "trial_active"
