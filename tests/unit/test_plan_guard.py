"""
Unit tests for app/plan_guard.py.

Each test mocks the DB session so no real DB is needed.
The key behaviors verified:
  - Inactive plan → 403
  - Feature not on plan → 403 with upgrade hint
  - Trial cap enforced (trial_generations < generation_limit)
  - Paid plan limit enforced
  - Limit exactly at cap → 429
  - Under limit → returns remaining correctly
  - require_generation orchestrates both guards in order
"""

from datetime import datetime, timedelta, timezone

# Billing cycle start far enough in the past that the 30-day paid period has expired.
# Use this for cancelled-shop tests that expect the shop to be blocked.
EXPIRED_CYCLE = datetime.now(timezone.utc) - timedelta(days=35)
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.plan_guard import (
    check_generation_limit,
    effective_cycle_start,
    get_generations_used,
    require_feature,
    require_generation,
)
from app.config import PLANS
from tests.conftest import make_shop


# ── require_feature ────────────────────────────────────────────────────────────

class TestRequireFeature:

    @pytest.mark.asyncio
    async def test_inactive_plan_raises_403(self, mock_db):
        shop = make_shop(plan_tier="growth", plan_status="cancelled", billing_cycle_start=EXPIRED_CYCLE)
        mock_db.execute.return_value = _scalar(shop)

        with pytest.raises(Exception) as exc:
            await require_feature("gift_finder", shop.shop_domain, mock_db)
        assert exc.value.status_code == 403
        detail = exc.value.detail
        detail_str = detail if isinstance(detail, str) else detail.get("message", detail.get("code", ""))
        assert "grace" in detail_str.lower() or "not active" in detail_str.lower()

    @pytest.mark.asyncio
    async def test_feature_on_plan_passes(self, mock_db):
        shop = make_shop(plan_tier="growth", plan_status="active")
        mock_db.execute.return_value = _scalar(shop)

        result_shop, result_plan = await require_feature("gift_finder", shop.shop_domain, mock_db)
        assert result_shop.shop_domain == shop.shop_domain
        assert "gift_finder" in result_plan["features"]

    @pytest.mark.asyncio
    async def test_feature_not_on_starter_raises_403_with_upgrade(self, mock_db):
        # Starter has the gift finder but not "arrive_by" (Growth+ only)
        shop = make_shop(plan_tier="starter", plan_status="active")
        mock_db.execute.return_value = _scalar(shop)

        with pytest.raises(Exception) as exc:
            await require_feature("arrive_by", shop.shop_domain, mock_db)
        assert exc.value.status_code == 403
        detail = exc.value.detail
        assert detail["code"] == "feature_not_available"
        assert detail["upgrade_to"] in ("growth", "pro")

    @pytest.mark.asyncio
    async def test_trial_active_plan_allowed(self, mock_db):
        shop = make_shop(plan_tier="growth", plan_status="trial_active")
        mock_db.execute.return_value = _scalar(shop)

        result_shop, _ = await require_feature("gift_finder", shop.shop_domain, mock_db)
        assert result_shop is shop

    @pytest.mark.asyncio
    async def test_unknown_shop_raises_404(self, mock_db):
        mock_db.execute.return_value = _scalar(None)

        with pytest.raises(Exception) as exc:
            await require_feature("gift_finder", "unknown.myshopify.com", mock_db)
        assert exc.value.status_code == 404


# ── check_generation_limit ─────────────────────────────────────────────────────

GROWTH_LIMIT = PLANS["growth"]["generation_limit"]
GROWTH_TRIAL = PLANS["growth"]["trial_generations"]
STARTER_LIMIT = PLANS["starter"]["generation_limit"]
PRO_LIMIT = PLANS["pro"]["generation_limit"]
PRO_TRIAL = PLANS["pro"]["trial_generations"]


class TestCheckGenerationLimit:
    """Limits are read from PLANS so these tests follow config changes."""

    @pytest.mark.asyncio
    async def test_under_limit_returns_remaining(self, mock_db):
        shop = make_shop(plan_tier="growth", plan_status="active")
        _mock_usage(mock_db, 100)

        remaining = await check_generation_limit(shop, mock_db, count=1)
        assert remaining == GROWTH_LIMIT - 100 - 1

    @pytest.mark.asyncio
    async def test_exactly_at_limit_raises_429(self, mock_db):
        shop = make_shop(plan_tier="growth", plan_status="active")
        _mock_usage(mock_db, GROWTH_LIMIT)  # fully consumed

        with pytest.raises(Exception) as exc:
            await check_generation_limit(shop, mock_db, count=1)
        assert exc.value.status_code == 429
        detail = exc.value.detail
        assert detail["code"] == "generation_limit_exceeded"
        assert detail["used"] == GROWTH_LIMIT
        assert detail["limit"] == GROWTH_LIMIT
        assert detail["remaining"] == 0

    @pytest.mark.asyncio
    async def test_starter_plan_limit(self, mock_db):
        shop = make_shop(plan_tier="starter", plan_status="active")
        _mock_usage(mock_db, STARTER_LIMIT - 1)

        remaining = await check_generation_limit(shop, mock_db, count=1)
        assert remaining == 0

    @pytest.mark.asyncio
    async def test_starter_plan_over_limit(self, mock_db):
        shop = make_shop(plan_tier="starter", plan_status="active")
        _mock_usage(mock_db, STARTER_LIMIT)

        with pytest.raises(Exception) as exc:
            await check_generation_limit(shop, mock_db, count=1)
        assert exc.value.status_code == 429

    @pytest.mark.asyncio
    async def test_trial_enforces_trial_cap_not_full_limit(self, mock_db):
        # Trial blocks at trial_generations, not generation_limit.
        trial_ends = datetime.now(timezone.utc) + timedelta(days=2)
        shop = make_shop(plan_tier="growth", plan_status="trial_active", trial_ends_at=trial_ends)
        _mock_usage(mock_db, GROWTH_TRIAL)

        with pytest.raises(Exception) as exc:
            await check_generation_limit(shop, mock_db, count=1)
        assert exc.value.status_code == 429
        detail = exc.value.detail
        assert detail["is_trial"] is True
        assert detail["limit"] == GROWTH_TRIAL
        assert detail["full_plan_limit"] == GROWTH_LIMIT

    @pytest.mark.asyncio
    async def test_trial_under_cap_passes(self, mock_db):
        trial_ends = datetime.now(timezone.utc) + timedelta(days=2)
        shop = make_shop(plan_tier="growth", plan_status="trial_active", trial_ends_at=trial_ends)
        _mock_usage(mock_db, 10)

        remaining = await check_generation_limit(shop, mock_db, count=1)
        assert remaining == GROWTH_TRIAL - 10 - 1

    @pytest.mark.asyncio
    async def test_pro_trial_cap(self, mock_db):
        trial_ends = datetime.now(timezone.utc) + timedelta(days=1)
        shop = make_shop(plan_tier="pro", plan_status="trial_active", trial_ends_at=trial_ends)
        _mock_usage(mock_db, PRO_TRIAL)

        with pytest.raises(Exception) as exc:
            await check_generation_limit(shop, mock_db, count=1)
        detail = exc.value.detail
        assert detail["limit"] == PRO_TRIAL
        assert detail["full_plan_limit"] == PRO_LIMIT

    @pytest.mark.asyncio
    async def test_weighted_count_check(self, mock_db):
        # A weight-4 model use (GPT-6 Sol / Sonnet 5) when only 3 generations remain
        shop = make_shop(plan_tier="growth", plan_status="active")
        _mock_usage(mock_db, GROWTH_LIMIT - 3)

        with pytest.raises(Exception) as exc:
            await check_generation_limit(shop, mock_db, count=4)
        assert exc.value.status_code == 429
        assert exc.value.detail["requested"] == 4

    @pytest.mark.asyncio
    async def test_trial_error_message_includes_days(self, mock_db):
        trial_ends = datetime.now(timezone.utc) + timedelta(days=3)
        shop = make_shop(plan_tier="growth", plan_status="trial_active", trial_ends_at=trial_ends)
        _mock_usage(mock_db, GROWTH_TRIAL)

        with pytest.raises(Exception) as exc:
            await check_generation_limit(shop, mock_db)
        msg = exc.value.detail["message"]
        assert "trial" in msg.lower()

    @pytest.mark.asyncio
    async def test_trial_with_naive_trial_ends_at(self, mock_db):
        # trial_ends_at stored without tzinfo (legacy rows) — must not crash on tz comparison
        naive_ends = datetime.now() + timedelta(days=2)  # no tzinfo
        shop = make_shop(plan_tier="growth", plan_status="trial_active", trial_ends_at=naive_ends)
        _mock_usage(mock_db, GROWTH_TRIAL)

        with pytest.raises(Exception) as exc:
            await check_generation_limit(shop, mock_db)
        assert exc.value.status_code == 429
        assert exc.value.detail["is_trial"] is True

    @pytest.mark.asyncio
    async def test_trial_without_trial_ends_at(self, mock_db):
        # trial_ends_at = None — message falls back to generic "when trial converts" copy
        shop = make_shop(plan_tier="growth", plan_status="trial_active", trial_ends_at=None)
        _mock_usage(mock_db, GROWTH_TRIAL)

        with pytest.raises(Exception) as exc:
            await check_generation_limit(shop, mock_db)
        assert exc.value.status_code == 429
        msg = exc.value.detail["message"]
        assert "trial converts" in msg


# ── require_generation ─────────────────────────────────────────────────────────

class TestRequireGeneration:

    @pytest.mark.asyncio
    async def test_inactive_plan_blocked_before_limit_check(self, mock_db):
        """Inactive plan should raise 403 from require_feature, never reaching limit check."""
        shop = make_shop(plan_tier="growth", plan_status="cancelled", billing_cycle_start=EXPIRED_CYCLE)
        mock_db.execute.return_value = _scalar(shop)

        with pytest.raises(Exception) as exc:
            await require_generation("gift_finder", shop.shop_domain, mock_db)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_wrong_feature_blocked(self, mock_db):
        # Starter has FAQ/descriptions/ad_copy but not catalog_audit (Pro-only)
        shop = make_shop(plan_tier="starter", plan_status="active")
        mock_db.execute.return_value = _scalar(shop)

        with pytest.raises(Exception) as exc:
            await require_generation("registries", shop.shop_domain, mock_db)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_passes_with_active_plan_and_budget(self, mock_db):
        shop = make_shop(plan_tier="growth", plan_status="active")
        mock_db.execute.side_effect = [
            _scalar(shop),         # get_shop_plan
            _scalar_value(50),     # get_generations_used → 50 used
            _scalar_value(0.5),    # get_daily_cost_usd → $0.50 today
        ]

        result_shop, result_plan = await require_generation("gift_finder", shop.shop_domain, mock_db)
        assert result_shop is shop


class TestDailyCostCap:
    """`require_generation` blocks shops that have hit the per-shop daily LLM
    cost cap, regardless of remaining plan generations. Margin protection."""

    @pytest.mark.asyncio
    async def test_under_cap_passes(self, mock_db):
        """Growth plan's per-tier cap is $3.00. $2.00 spent should pass."""
        from app.plan_guard import check_daily_cost_cap

        shop = make_shop(plan_tier="growth", plan_status="active")
        mock_db.execute.return_value = _scalar_value(2.00)

        await check_daily_cost_cap(shop, mock_db)

    @pytest.mark.asyncio
    async def test_at_cap_raises_429(self, mock_db):
        """Growth plan's per-tier cap is $3.00 — at exactly $3.00 should raise."""
        from app.plan_guard import check_daily_cost_cap
        from app.config import PLANS

        shop = make_shop(plan_tier="growth", plan_status="active")
        growth_cap = PLANS["growth"]["daily_cost_cap_usd"]
        mock_db.execute.return_value = _scalar_value(growth_cap)

        with pytest.raises(Exception) as exc:
            await check_daily_cost_cap(shop, mock_db)
        assert exc.value.status_code == 429
        detail = exc.value.detail
        assert detail["code"] == "daily_cost_cap_exceeded"
        assert detail["spent_usd_today"] == growth_cap
        # `resumes_at` should be in the future (next UTC midnight)
        from datetime import datetime
        resumes_at = datetime.fromisoformat(detail["resumes_at"])
        assert resumes_at > datetime.now(resumes_at.tzinfo)
        delta_hours = (resumes_at - datetime.now(resumes_at.tzinfo)).total_seconds() / 3600
        assert 0 < delta_hours <= 24

    @pytest.mark.asyncio
    async def test_over_cap_raises_429(self, mock_db):
        """Growth cap is $3.00 — $5.42 spent should raise."""
        from app.plan_guard import check_daily_cost_cap

        shop = make_shop(plan_tier="growth", plan_status="active")
        mock_db.execute.return_value = _scalar_value(5.42)

        with pytest.raises(Exception) as exc:
            await check_daily_cost_cap(shop, mock_db)
        assert exc.value.detail["spent_usd_today"] == 5.42

    @pytest.mark.asyncio
    async def test_disabled_when_cap_is_zero(self, mock_db):
        """Setting the plan's daily_cost_cap_usd to 0 disables the check entirely."""
        from app.plan_guard import check_daily_cost_cap
        from app.config import PLANS

        shop = make_shop(plan_tier="growth", plan_status="active")
        mock_db.execute.return_value = _scalar_value(999_999.0)

        # Override the per-tier cap to 0 for this test
        original = PLANS["growth"]["daily_cost_cap_usd"]
        PLANS["growth"]["daily_cost_cap_usd"] = 0
        try:
            await check_daily_cost_cap(shop, mock_db)
        finally:
            PLANS["growth"]["daily_cost_cap_usd"] = original
        # db.execute should not have been called when disabled
        mock_db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_require_generation_blocks_when_over_cap(self, mock_db):
        """End-to-end: even with plan capacity remaining, hitting the daily
        cost cap blocks generation."""
        shop = make_shop(plan_tier="growth", plan_status="active")
        shop.first_generation_at = None
        mock_db.execute.side_effect = [
            _scalar(shop),         # get_shop_plan
            _scalar_value(50),     # plenty of plan budget left
            _scalar_value(50.0),   # but $50 spent today — way over $3 Growth cap
        ]

        with pytest.raises(Exception) as exc:
            await require_generation("gift_finder", shop.shop_domain, mock_db)
        assert exc.value.status_code == 429
        assert exc.value.detail["code"] == "daily_cost_cap_exceeded"
        # Activation must NOT be stamped — they didn't actually generate
        assert shop.first_generation_at is None


class TestActivationTracking:
    """`require_generation` stamps `first_generation_at` the first time a shop
    passes every gate. Subsequent calls leave it unchanged."""

    @pytest.mark.asyncio
    async def test_first_generation_at_stamped_on_first_pass(self, mock_db):
        """require_generation calls execute three times in order:
          1) get_shop_plan → returns the Shop
          2) get_generations_used → returns int (already-consumed count)
          3) get_daily_cost_usd → returns float (today's spend)
        """
        from datetime import datetime, timezone

        shop = make_shop(plan_tier="growth", plan_status="active")
        shop.first_generation_at = None
        mock_db.execute.side_effect = [_scalar(shop), _scalar_value(0), _scalar_value(0.0)]

        before = datetime.now(timezone.utc)
        await require_generation("gift_finder", shop.shop_domain, mock_db)
        after = datetime.now(timezone.utc)

        assert shop.first_generation_at is not None
        assert before <= shop.first_generation_at <= after

    @pytest.mark.asyncio
    async def test_existing_first_generation_at_preserved(self, mock_db):
        """If already activated, the timestamp must NOT update on subsequent generations.
        Activation is a one-time event."""
        from datetime import datetime, timezone, timedelta

        original = datetime.now(timezone.utc) - timedelta(days=14)
        shop = make_shop(plan_tier="growth", plan_status="active")
        shop.first_generation_at = original
        mock_db.execute.side_effect = [_scalar(shop), _scalar_value(50), _scalar_value(0.5)]

        await require_generation("gift_finder", shop.shop_domain, mock_db)

        assert shop.first_generation_at == original

    @pytest.mark.asyncio
    async def test_blocked_generation_does_not_stamp(self, mock_db):
        """If require_generation raises (over budget, wrong plan, etc.), the
        activation timestamp must remain null — the merchant didn't activate."""
        from app.config import PLANS

        shop = make_shop(plan_tier="growth", plan_status="active")
        shop.first_generation_at = None
        plan_limit = PLANS["growth"]["generation_limit"]
        mock_db.execute.side_effect = [_scalar(shop), _scalar_value(plan_limit)]

        with pytest.raises(Exception) as exc:
            await require_generation("gift_finder", shop.shop_domain, mock_db, count=1)
        assert exc.value.status_code == 429
        assert shop.first_generation_at is None


# ── Helpers ────────────────────────────────────────────────────────────────────

def _scalar(value):
    """Mock a SQLAlchemy result that returns a single ORM object via scalar_one_or_none()."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


def _scalar_value(value):
    """Mock a SQLAlchemy result that returns a scalar (int/float) via scalar()."""
    result = MagicMock()
    result.scalar.return_value = value
    result.scalar_one_or_none.return_value = value
    return result


def _mock_usage(mock_db, used: int):
    """
    Configure mock_db.execute to return the shop on the first call,
    then return the usage sum on the second call.

    check_generation_limit calls get_generations_used which does one execute().
    """
    mock_db.execute.return_value = _scalar_value(used)


# ── require_feature — grace period asymmetry ──────────────────────────────────

class TestRequireFeatureGracePeriod:

    @pytest.mark.asyncio
    async def test_cancelled_in_grace_period_allows_view(self, mock_db):
        """Cancelled shop past paid period but still inside grace period can VIEW."""
        grace_ends = datetime.now(timezone.utc) + timedelta(hours=12)
        shop = make_shop(plan_tier="growth", plan_status="cancelled",
                         billing_cycle_start=EXPIRED_CYCLE, grace_period_ends_at=grace_ends)
        mock_db.execute.return_value = _scalar(shop)

        result_shop, result_plan = await require_feature("gift_finder", shop.shop_domain, mock_db)
        assert result_shop is shop

    @pytest.mark.asyncio
    async def test_cancelled_past_grace_period_blocks_view(self, mock_db):
        """Cancelled shop whose paid period and grace period have both expired must get 403."""
        grace_ends = datetime.now(timezone.utc) - timedelta(hours=1)
        shop = make_shop(plan_tier="growth", plan_status="cancelled",
                         billing_cycle_start=EXPIRED_CYCLE, grace_period_ends_at=grace_ends)
        mock_db.execute.return_value = _scalar(shop)

        with pytest.raises(Exception) as exc:
            await require_feature("gift_finder", shop.shop_domain, mock_db)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_cancelled_in_grace_period_blocks_generation(self, mock_db):
        """Cancelled shop inside grace period can VIEW but cannot GENERATE.

        require_feature allows it, but require_generation must raise 403
        because plan_status is not in GENERATE_STATUSES.
        """
        grace_ends = datetime.now(timezone.utc) + timedelta(hours=12)
        shop = make_shop(plan_tier="growth", plan_status="cancelled",
                         billing_cycle_start=EXPIRED_CYCLE, grace_period_ends_at=grace_ends)
        mock_db.execute.side_effect = [
            _scalar(shop),       # require_feature shop lookup
            _scalar(shop),       # (second lookup if require_generation re-fetches)
            _scalar_value(50),   # usage check
        ]

        with pytest.raises(Exception) as exc:
            await require_generation("gift_finder", shop.shop_domain, mock_db)
        assert exc.value.status_code == 403


# ── effective_cycle_start — monthly only ───────────────────────────────────────

class TestEffectiveCycleStart:
    """GiftSense is monthly-only: the cycle start is billing_cycle_start as-is
    (Commerce's annual rolling anchor was dropped)."""

    def test_returns_raw_billing_cycle_start(self):
        from app.plan_guard import effective_cycle_start
        bcs = datetime.now(timezone.utc) - timedelta(days=10)
        shop = make_shop(billing_cycle_start=bcs)
        assert effective_cycle_start(shop) == bcs

    def test_no_billing_cycle_start_returns_none(self):
        from app.plan_guard import effective_cycle_start
        shop = make_shop()
        shop.billing_cycle_start = None
        assert effective_cycle_start(shop) is None

    def test_naive_datetime_gets_utc_tz(self):
        from app.plan_guard import effective_cycle_start
        naive = datetime(2026, 1, 1, 12, 0, 0)
        shop = make_shop(billing_cycle_start=naive)
        assert effective_cycle_start(shop).tzinfo is not None
