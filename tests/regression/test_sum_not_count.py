"""
REGRESSION (from Prudix Commerce): generation usage uses SUM(generations_consumed),
not COUNT(rows).

Commerce's original code counted UsageLog rows. One row can consume several
generations (a model weight of 2 or 4, or a batch), and refund rows are
negative, so COUNT overstates or understates remaining budget. GiftSense
asserts this on `plan_guard`, where the monthly limit is enforced.
"""

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from fastapi import HTTPException

from app.config import PLANS
from app.plan_guard import check_generation_limit, get_generations_used
from tests.conftest import add_usage, make_shop


@pytest_asyncio.fixture
async def active_shop(db_session):
    shop = make_shop(
        plan_tier="growth",
        plan_status="active",
        billing_cycle_start=datetime.now(timezone.utc) - timedelta(days=5),
    )
    db_session.add(shop)
    await db_session.commit()
    return shop


class TestSumNotCountRegression:

    @pytest.mark.asyncio
    async def test_weighted_row_counts_by_weight_not_1(self, db_session, active_shop):
        """One row with generations_consumed=4 (a Sonnet 5 use) reports 4, not 1."""
        await add_usage(db_session, active_shop, generations=4)
        assert await get_generations_used(active_shop, db_session) == 4

    @pytest.mark.asyncio
    async def test_mixed_rows_sum_correctly(self, db_session, active_shop):
        """Rows of 10, 5, 1 total 16, not 3."""
        for n in (10, 5, 1):
            await add_usage(db_session, active_shop, generations=n)
        assert await get_generations_used(active_shop, db_session) == 16

    @pytest.mark.asyncio
    async def test_refund_row_cancels_upfront_charge(self, db_session, active_shop):
        """Refund pattern: +2 upfront then -2 on failure nets to 0 (COUNT would say 2)."""
        await add_usage(db_session, active_shop, generations=2)
        await add_usage(db_session, active_shop, generations=-2)
        assert await get_generations_used(active_shop, db_session) == 0

    @pytest.mark.asyncio
    async def test_limit_enforced_on_sum_not_row_count(self, db_session):
        """One row consuming the whole Starter limit must block the next use."""
        starter_limit = PLANS["starter"]["generation_limit"]
        shop = make_shop(
            plan_tier="starter",
            plan_status="active",
            billing_cycle_start=datetime.now(timezone.utc) - timedelta(days=5),
            domain="starter-regression.myshopify.com",
        )
        db_session.add(shop)
        await db_session.commit()
        await add_usage(db_session, shop, generations=starter_limit)

        with pytest.raises(HTTPException) as exc:
            await check_generation_limit(shop, db_session, count=1)
        assert exc.value.status_code == 429
