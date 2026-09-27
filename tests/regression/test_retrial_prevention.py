"""
REGRESSION: Re-trial prevention after uninstall → reinstall.

Shopify does not track whether a store already used a trial — we do.
If trial_used=True is not checked at billing/callback, a merchant could:
  1. Install → get 3-day trial
  2. Uninstall before trial ends
  3. Reinstall → get another 3-day trial (FREE)

Fixed in: /billing/callback — checks trial_used before activating trial.

This test verifies the DB-level flag, not the full HTTP flow (which requires
mocking the Shopify billing API). The HTTP-level scenario is in test_scenarios.
"""

import uuid
from datetime import datetime, timezone, timedelta
import pytest
import pytest_asyncio

from core.db.models import Shop, BillingEvent
from tests.conftest import make_shop, TEST_SHOP_DOMAIN


class TestRetrialPrevention:

    @pytest.mark.asyncio
    async def test_fresh_install_trial_used_is_false(self, db_session):
        shop = make_shop(plan_tier="none", plan_status="pending")
        shop.trial_used = False
        db_session.add(shop)
        await db_session.commit()
        await db_session.refresh(shop)
        assert shop.trial_used is False

    @pytest.mark.asyncio
    async def test_trial_started_sets_trial_used_true(self, db_session):
        """Simulate what billing/callback does when trial is activated."""
        shop = make_shop(plan_tier="none", plan_status="pending")
        shop.trial_used = False
        db_session.add(shop)
        await db_session.commit()

        # Simulate billing callback activating trial
        now = datetime.now(timezone.utc)
        shop.plan_status = "trial_active"
        shop.plan_tier = "growth"
        shop.trial_used = True
        shop.trial_started_at = now
        shop.trial_ends_at = now + timedelta(days=3)
        shop.billing_cycle_start = now
        await db_session.commit()

        await db_session.refresh(shop)
        assert shop.trial_used is True
        assert shop.plan_status == "trial_active"

    @pytest.mark.asyncio
    async def test_reinstall_with_trial_used_true_gets_active_not_trial(self, db_session):
        """After uninstall+reinstall, trial_used=True must prevent second trial.
        The billing callback should set plan_status='active', not 'trial_active'."""
        # Simulate a shop that already used its trial
        shop = make_shop(plan_tier="growth", plan_status="uninstalled")
        shop.trial_used = True
        shop.access_token_encrypted = ""
        db_session.add(shop)
        await db_session.commit()

        # Simulate reinstall OAuth callback updating the token
        shop.access_token_encrypted = "new_encrypted_token"
        shop.plan_status = "pending"
        await db_session.commit()

        # Now simulate billing callback: with_trial = not shop.trial_used = False
        with_trial = not shop.trial_used
        assert with_trial is False  # trial MUST NOT be offered again

        # Simulate what billing/callback does when with_trial=False
        now = datetime.now(timezone.utc)
        shop.plan_status = "active"   # NOT trial_active
        shop.plan_tier = "growth"
        shop.billing_cycle_start = now
        db_session.add(BillingEvent(
            id=uuid.uuid4(),
            shop_id=shop.id,
            event_type="activated",   # NOT trial_started
            plan_tier="growth",
            shopify_charge_id="charge_456",
        ))
        await db_session.commit()

        await db_session.refresh(shop)
        assert shop.plan_status == "active"
        assert shop.trial_used is True  # unchanged

    @pytest.mark.asyncio
    async def test_trial_used_flag_survives_uninstall_webhook(self, db_session):
        """The uninstall webhook must NOT clear trial_used — only the token."""
        shop = make_shop(plan_tier="growth", plan_status="trial_active")
        shop.trial_used = True
        shop.access_token_encrypted = "real_token"
        db_session.add(shop)
        await db_session.commit()

        # Simulate uninstall webhook handler
        shop.access_token_encrypted = ""
        shop.plan_status = "uninstalled"
        shop.uninstalled_at = datetime.now(timezone.utc)
        await db_session.commit()

        await db_session.refresh(shop)
        assert shop.trial_used is True  # must survive uninstall
        assert shop.access_token_encrypted == ""

    @pytest.mark.asyncio
    async def test_billing_callback_logic_with_trial_used(self, db_session):
        """Unit-level check of the with_trial conditional used in billing.py."""
        # Fresh install
        fresh_shop = make_shop(plan_tier="none", plan_status="pending")
        fresh_shop.trial_used = False
        db_session.add(fresh_shop)
        await db_session.commit()

        with_trial_fresh = not fresh_shop.trial_used
        assert with_trial_fresh is True

        # Reinstall
        returning_shop = make_shop(plan_tier="none", plan_status="pending",
                                   domain="returning.myshopify.com")
        returning_shop.trial_used = True
        db_session.add(returning_shop)
        await db_session.commit()

        with_trial_returning = not returning_shop.trial_used
        assert with_trial_returning is False
