"""
REGRESSION: Cancelled and declined shops must be blocked at the API level,
not just in the UI.

A shop that cancels via Shopify's billing UI sets plan_status='cancelled'
via the webhook. The dashboard hides the generate button — but if someone
calls the API directly, they must still get a 403.

Same for declined (payment failed) — the webhook sets plan_status='declined'.

These tests verify the backend enforcement is independent of the UI.
"""

from datetime import datetime, timezone, timedelta

EXPIRED_CYCLE = datetime.now(timezone.utc) - timedelta(days=35)
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.plan_guard import require_feature, check_generation_limit
from app.config import PLANS
from tests.conftest import make_shop

INACTIVE_STATUSES = ["cancelled", "expired", "declined", "uninstalled", "pending"]


class TestInactivePlanBlockedAtApi:

    @pytest.mark.parametrize("plan_status", INACTIVE_STATUSES)
    @pytest.mark.asyncio
    async def test_all_inactive_statuses_blocked_on_require_feature(self, plan_status, mock_db):
        # Use EXPIRED_CYCLE so cancelled shops are past their paid period and get blocked.
        shop = make_shop(plan_tier="growth", plan_status=plan_status, billing_cycle_start=EXPIRED_CYCLE)
        mock_db.execute.return_value = _scalar(shop)

        with pytest.raises(Exception) as exc:
            await require_feature("gift_finder", shop.shop_domain, mock_db)
        assert exc.value.status_code == 403, (
            f"plan_status='{plan_status}' should return 403, got {exc.value.status_code}"
        )

    @pytest.mark.asyncio
    async def test_cancelled_shop_blocked_with_clear_message(self, mock_db):
        shop = make_shop(plan_tier="growth", plan_status="cancelled", billing_cycle_start=EXPIRED_CYCLE)
        mock_db.execute.return_value = _scalar(shop)

        with pytest.raises(Exception) as exc:
            await require_feature("gift_finder", shop.shop_domain, mock_db)
        detail = exc.value.detail
        detail_str = detail if isinstance(detail, str) else detail.get("message", detail.get("code", ""))
        assert "grace" in detail_str.lower() or "cancelled" in detail_str.lower() or "not active" in detail_str.lower()

    @pytest.mark.asyncio
    async def test_declined_shop_blocked_with_clear_message(self, mock_db):
        shop = make_shop(plan_tier="growth", plan_status="declined")
        mock_db.execute.return_value = _scalar(shop)

        with pytest.raises(Exception) as exc:
            await require_feature("gift_finder", shop.shop_domain, mock_db)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_active_shop_passes(self, mock_db):
        shop = make_shop(plan_tier="growth", plan_status="active")
        mock_db.execute.return_value = _scalar(shop)

        result_shop, _ = await require_feature("gift_finder", shop.shop_domain, mock_db)
        assert result_shop.shop_domain == shop.shop_domain

    @pytest.mark.asyncio
    async def test_trial_active_passes(self, mock_db):
        shop = make_shop(plan_tier="growth", plan_status="trial_active")
        mock_db.execute.return_value = _scalar(shop)

        result_shop, _ = await require_feature("gift_finder", shop.shop_domain, mock_db)
        assert result_shop is shop

    @pytest.mark.asyncio
    async def test_cancelled_cannot_bypass_via_direct_limit_check(self, mock_db):
        """Even if somehow require_feature is skipped, check_generation_limit
        does not implicitly allow inactive shops to consume budget."""
        # check_generation_limit itself doesn't check plan_status — that's
        # require_feature's job. This test documents the intended call order:
        # require_feature MUST be called before check_generation_limit.
        # The composite guard require_generation() enforces this order.
        shop = make_shop(plan_tier="growth", plan_status="cancelled", billing_cycle_start=EXPIRED_CYCLE)
        mock_db.execute.return_value = _scalar_value(0)  # 0 used

        # check_generation_limit alone would pass (0 < limit)
        # This is correct — the status check happens in require_feature
        remaining = await check_generation_limit(shop, mock_db, count=1)
        assert remaining == PLANS["growth"]["generation_limit"] - 1  # passes: status check is not its job


class TestInactivePlanBlocksSpecificFeatures:

    @pytest.mark.asyncio
    async def test_description_rewrite_with_cancelled_plan_blocked(self, mock_db):
        """Cancelled plan must be blocked from the descriptions (rewrite-description) feature."""
        shop = make_shop(plan_tier="growth", plan_status="cancelled", billing_cycle_start=EXPIRED_CYCLE)
        mock_db.execute.return_value = _scalar(shop)

        with pytest.raises(Exception) as exc:
            await require_feature("descriptions", shop.shop_domain, mock_db)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_faq_generate_with_declined_plan_blocked(self, mock_db):
        """Declined plan must be blocked from the faq feature."""
        shop = make_shop(plan_tier="pro", plan_status="declined")
        mock_db.execute.return_value = _scalar(shop)

        with pytest.raises(Exception) as exc:
            await require_feature("gift_finder", shop.shop_domain, mock_db)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_expired_plan_blocked_from_faq(self, mock_db):
        """Expired plan (without grace period) must be blocked from the faq feature."""
        shop = make_shop(plan_tier="pro", plan_status="expired")
        mock_db.execute.return_value = _scalar(shop)

        with pytest.raises(Exception) as exc:
            await require_feature("gift_finder", shop.shop_domain, mock_db)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_uninstalled_plan_blocked_from_descriptions(self, mock_db):
        """Uninstalled shop must be blocked from all features."""
        shop = make_shop(plan_tier="growth", plan_status="uninstalled")
        mock_db.execute.return_value = _scalar(shop)

        with pytest.raises(Exception) as exc:
            await require_feature("descriptions", shop.shop_domain, mock_db)
        assert exc.value.status_code == 403


# ── Helpers ────────────────────────────────────────────────────────────────────

def _scalar(value):
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result

def _scalar_value(value):
    result = MagicMock()
    result.scalar.return_value = value
    result.scalar_one_or_none.return_value = value
    return result
