"""
REGRESSION (from Prudix Commerce): billing_cycle_start must reset when a trial
converts to paid, and on every monthly renewal.

Without the reset, generations consumed during the trial count against the
merchant's first paid month. Commerce asserted this through its analytics
route; GiftSense asserts it on `plan_guard.get_generations_used`, the function
the monthly limit is enforced with.

Fixed in: app/routes/webhooks.py — _handle_subscription_update() sets
billing_cycle_start = now on trial conversion and renewal.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app.plan_guard import get_generations_used
from tests.conftest import add_usage, make_shop


class TestCycleResetOnTrialConvert:

    @pytest.mark.asyncio
    async def test_trial_usage_excluded_after_cycle_reset(self, db_session):
        """18 trial generations, then the trial converts → 0 used in the new cycle."""
        trial_start = datetime.now(timezone.utc) - timedelta(days=3)
        shop = make_shop(plan_tier="growth", plan_status="trial_active", billing_cycle_start=trial_start)
        db_session.add(shop)
        await db_session.commit()

        await add_usage(db_session, shop, generations=18, created_at=trial_start + timedelta(hours=6))

        shop.plan_status = "active"
        shop.billing_cycle_start = datetime.now(timezone.utc)
        await db_session.commit()

        used = await get_generations_used(shop, db_session)
        assert used == 0, (
            f"Expected 0 after cycle reset, got {used}. "
            "Trial generations are bleeding into the first paid month."
        )

    @pytest.mark.asyncio
    async def test_post_conversion_usage_counts_from_new_cycle(self, db_session):
        """Generations after trial conversion count against the new cycle."""
        now = datetime.now(timezone.utc)
        shop = make_shop(plan_tier="growth", plan_status="active", billing_cycle_start=now)
        db_session.add(shop)
        await db_session.commit()

        await add_usage(db_session, shop, generations=5, created_at=now + timedelta(minutes=1))

        assert await get_generations_used(shop, db_session) == 5

    @pytest.mark.asyncio
    async def test_renewal_also_resets_cycle(self, db_session):
        """Monthly renewal (not just trial conversion) also resets the cycle."""
        old_cycle = datetime.now(timezone.utc) - timedelta(days=30)
        shop = make_shop(plan_tier="growth", plan_status="active", billing_cycle_start=old_cycle)
        db_session.add(shop)
        await db_session.commit()

        await add_usage(db_session, shop, generations=300, created_at=old_cycle + timedelta(days=15))

        shop.billing_cycle_start = datetime.now(timezone.utc)
        await db_session.commit()

        assert await get_generations_used(shop, db_session) == 0, (
            "Previous cycle generations bleed into new cycle after renewal."
        )
