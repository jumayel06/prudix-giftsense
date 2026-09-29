"""
Integration tests for the billing callback and install lifecycle.

/billing/callback is the most complex route — it calls the Shopify API twice
(fetch charge status, activate charge), then transitions shop state.
Both Shopify API calls are mocked with httpx.

Key scenarios:
  - Accepted charge → trial_active on first install, trial_used=True set
  - Accepted charge → active (not trial) on reinstall (trial_used=True already)
  - Declined charge → plan_status=declined
  - BillingEvent record created for each transition
  - /api/billing/create-charge validates plan tier
  - /api/plans returns all plan options
"""

import json
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.main import app  # import before any httpx patches are applied
from core.db.session import get_db
from core.db.models import BillingEvent, Shop
from core.shopify_auth import encrypt_token
from tests.conftest import make_shop, TEST_SHOP_DOMAIN


def _make_client(db_session):
    from fastapi.testclient import TestClient

    async def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app, raise_server_exceptions=True)
    yield client
    app.dependency_overrides.clear()


def _shopify_charge_response(status: str = "accepted", charge_id: str = "999"):
    """Legacy REST helper kept for currently-passing edge-case tests that mock client.get."""
    return MagicMock(
        status_code=200,
        json=lambda: {
            "recurring_application_charge": {
                "id": charge_id,
                "status": status,
                "confirmation_url": f"https://testshop.myshopify.com/admin/charges/{charge_id}/confirm",
            }
        },
    )


def _graphql_subscription_response(status: str = "ACTIVE", name: str | None = None, interval: str | None = None):
    """GraphQL response for the appSubscription query used by billing_callback.

    `name` / `interval`, when given, mirror the authoritative fields the real
    callback now reads to derive tier + is_annual (instead of trusting the URL
    query params). Left None → the legacy null-node fallback path.
    """
    node: dict = {"status": status}
    if name is not None:
        node["name"] = name
    if interval is not None:
        node["lineItems"] = [{
            "plan": {"pricingDetails": {"__typename": "AppRecurringPricing", "interval": interval}}
        }]
    return MagicMock(
        status_code=200,
        json=lambda: {"data": {"node": node}},
    )


# ── /billing/callback ─────────────────────────────────────────────────────────

class TestBillingCallback:

    @pytest.mark.asyncio
    async def test_first_install_accepted_activates_trial(self, db_session, job_pool):
        """Fresh install (trial_used=False) + accepted charge → trial_active."""
        shop = make_shop(plan_tier="none", plan_status="pending")
        shop.trial_used = False
        shop.access_token_encrypted = encrypt_token("access_token_abc")
        db_session.add(shop)
        await db_session.commit()

        with patch("app.routes.billing.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=_graphql_subscription_response("ACTIVE"))
            mock_client_cls.return_value = mock_client

            for client in _make_client(db_session):
                resp = client.get(
                    f"/billing/callback?shop={TEST_SHOP_DOMAIN}&charge_id=999&plan=growth",
                    follow_redirects=False,
                )

        assert resp.status_code in (200, 307, 302, 303)  # redirect to Shopify

        result = await db_session.execute(
            select(Shop).where(Shop.shop_domain == TEST_SHOP_DOMAIN)
        )
        s = result.scalar_one()
        # Plan state
        assert s.plan_status == "trial_active"
        assert s.plan_tier == "growth"
        assert s.trial_used is True
        # Trial timestamps
        assert s.trial_started_at is not None
        assert s.trial_ends_at is not None
        assert s.billing_cycle_start is not None
        # No grace period during active trial
        assert s.grace_period_ends_at is None
        # Charge ID stored
        assert s.shopify_charge_id == "999"
        # Active trial: no purge date, not uninstalled
        assert s.data_purge_at is None
        assert s.uninstalled_at is None
        # Catalog analysis starts right away
        assert job_pool.jobs == [("catalog_start_sync", str(s.id), "initial")]

    @pytest.mark.asyncio
    async def test_first_install_accepted_creates_trial_started_event(self, db_session):
        shop = make_shop(plan_tier="none", plan_status="pending")
        shop.trial_used = False
        shop.access_token_encrypted = encrypt_token("access_token_abc")
        db_session.add(shop)
        await db_session.commit()

        with patch("app.routes.billing.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=_graphql_subscription_response("ACTIVE"))
            mock_client_cls.return_value = mock_client

            for client in _make_client(db_session):
                client.get(f"/billing/callback?shop={TEST_SHOP_DOMAIN}&charge_id=999&plan=growth", follow_redirects=False)

        result = await db_session.execute(
            select(BillingEvent).where(BillingEvent.shop_id == shop.id)
        )
        event = result.scalar_one_or_none()
        assert event is not None
        assert event.event_type == "trial_started"
        assert event.plan_tier == "growth"
        assert event.shopify_charge_id == "999"
        assert event.shop_id == shop.id

    @pytest.mark.asyncio
    async def test_reinstall_accepted_activates_without_trial(self, db_session):
        """Reinstall (trial_used=True) + accepted charge → active, NOT trial_active."""
        shop = make_shop(plan_tier="none", plan_status="pending")
        shop.trial_used = True  # already used trial
        shop.access_token_encrypted = encrypt_token("access_token_xyz")
        db_session.add(shop)
        await db_session.commit()

        with patch("app.routes.billing.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=_graphql_subscription_response("ACTIVE"))
            mock_client_cls.return_value = mock_client

            for client in _make_client(db_session):
                client.get(f"/billing/callback?shop={TEST_SHOP_DOMAIN}&charge_id=888&plan=growth", follow_redirects=False)

        result = await db_session.execute(
            select(Shop).where(Shop.shop_domain == TEST_SHOP_DOMAIN)
        )
        s = result.scalar_one()
        # Reinstall goes straight to active — no second trial
        assert s.plan_status == "active"
        assert s.plan_tier == "growth"
        assert s.trial_used is True        # still True — never changes
        assert s.trial_started_at is None  # no trial started
        assert s.trial_ends_at is None     # no trial end date
        assert s.grace_period_ends_at is None
        assert s.billing_cycle_start is not None
        assert s.shopify_charge_id == "888"
        # Active paid plan: no purge date, not uninstalled
        assert s.data_purge_at is None
        assert s.uninstalled_at is None

    @pytest.mark.asyncio
    async def test_declined_charge_sets_declined_status(self, db_session):
        shop = make_shop(plan_tier="none", plan_status="pending")
        shop.billing_cycle_start = None  # pending install — never billed yet
        shop.access_token_encrypted = encrypt_token("access_token_abc")
        db_session.add(shop)
        await db_session.commit()

        with patch("app.routes.billing.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=_graphql_subscription_response("DECLINED"))
            mock_client_cls.return_value = mock_client

            for client in _make_client(db_session):
                client.get(f"/billing/callback?shop={TEST_SHOP_DOMAIN}&charge_id=777&plan=growth", follow_redirects=False)

        result = await db_session.execute(
            select(Shop).where(Shop.shop_domain == TEST_SHOP_DOMAIN)
        )
        s = result.scalar_one()
        assert s.plan_status == "declined"
        # Declined charge must not set billing fields
        assert s.plan_tier == "none"          # unchanged from fixture
        assert s.trial_used is False          # unchanged
        assert s.billing_cycle_start is None
        assert s.trial_started_at is None
        assert s.trial_ends_at is None
        assert s.shopify_charge_id is None
        # Declined does NOT get a grace period (only cancelled/expired does)
        assert s.grace_period_ends_at is None
        # Declined is not an uninstall — no purge date
        assert s.data_purge_at is None
        assert s.uninstalled_at is None

    @pytest.mark.asyncio
    async def test_monthly_billing_fresh_merchant_gets_trial_active(self, db_session):
        """Monthly interval with trial_used=False activates trial (positive counterpart to annual test)."""
        shop = make_shop(plan_tier="none", plan_status="pending")
        shop.trial_used = False
        shop.access_token_encrypted = encrypt_token("access_token_monthly")
        db_session.add(shop)
        await db_session.commit()

        with patch("app.routes.billing.httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=_graphql_subscription_response("ACTIVE"))
            mock_client_cls.return_value = mock_client

            for client in _make_client(db_session):
                client.get(
                    f"/billing/callback?shop={TEST_SHOP_DOMAIN}&charge_id=999&plan=growth&interval=monthly",
                    follow_redirects=False,
                )

        result = await db_session.execute(
            select(Shop).where(Shop.shop_domain == TEST_SHOP_DOMAIN)
        )
        s = result.scalar_one()
        assert s.plan_status == "trial_active"
        assert s.plan_tier == "growth"
        assert s.trial_used is True
        assert s.trial_started_at is not None
        assert s.trial_ends_at is not None
        assert s.billing_cycle_start is not None
        assert s.shopify_charge_id == "999"
        assert s.grace_period_ends_at is None
        assert s.data_purge_at is None
        assert s.uninstalled_at is None


# ── /api/billing/create-charge ────────────────────────────────────────────────

class TestCreateCharge:

    @pytest.mark.asyncio
    async def test_unknown_plan_returns_400(self, db_session):
        shop = make_shop(plan_tier="growth", plan_status="active")
        db_session.add(shop)
        await db_session.commit()

        for client in _make_client(db_session):
            resp = client.post(
                f"/api/billing/create-charge?shop={TEST_SHOP_DOMAIN}&plan=enterprise"
            )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_unknown_shop_returns_404(self, db_session):
        with patch("app.routes.billing.httpx.AsyncClient"):
            for client in _make_client(db_session):
                resp = client.post(
                    "/api/billing/create-charge?shop=nobody.myshopify.com&plan=growth"
                )
        assert resp.status_code == 404


# ── /api/plans ────────────────────────────────────────────────────────────────

class TestListPlans:

    def test_returns_all_three_plans(self, db_session):
        for client in _make_client(db_session):
            resp = client.get("/api/plans")
        assert resp.status_code == 200
        plans = resp.json()["plans"]
        tiers = {p["tier"] for p in plans}
        assert tiers == {"starter", "growth", "pro"}

    def test_plan_fields_present(self, db_session):
        for client in _make_client(db_session):
            resp = client.get("/api/plans")
        growth = next(p for p in resp.json()["plans"] if p["tier"] == "growth")
        assert growth["price_usd"] == PLANS["growth"]["price_usd"]
        assert growth["generation_limit"] == PLANS["growth"]["generation_limit"]
        assert growth["trial_days"] == 7
        assert "gpt-6-sol" in growth["models_available"]

    def test_starter_plan_fields(self, db_session):
        for client in _make_client(db_session):
            resp = client.get("/api/plans")
        starter = next(p for p in resp.json()["plans"] if p["tier"] == "starter")
        assert starter["price_usd"] > 0
        assert starter["generation_limit"] > 0
        assert "gpt-4o" not in starter["models_available"]  # starter can't use gpt-4o
        assert starter["price_usd"] < 30  # starter must be cheapest tier

    def test_pro_plan_fields(self, db_session):
        for client in _make_client(db_session):
            resp = client.get("/api/plans")
        pro = next(p for p in resp.json()["plans"] if p["tier"] == "pro")
        assert pro["price_usd"] > 0
        assert pro["generation_limit"] > 500   # pro has more than growth
        assert "gpt-6-sol" in pro["models_available"]


# ── /api/billing/create-charge — success path ─────────────────────────────────

class TestCreateChargeSuccess:

    @pytest.mark.asyncio
    async def test_create_charge_success_returns_confirmation_url(self, db_session):
        """Happy path: shop exists, Shopify returns 201 → confirmation_url returned."""
        shop = make_shop(plan_tier="growth", plan_status="active")
        shop.trial_used = False
        shop.access_token_encrypted = encrypt_token("test_tok")
        db_session.add(shop)
        await db_session.commit()

        with patch("app.routes.billing.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=MagicMock(
                status_code=200,
                json=lambda: {"data": {"appSubscriptionCreate": {
                    "confirmationUrl": "https://testshop.myshopify.com/admin/confirm",
                    "appSubscription": {"id": "gid://shopify/AppSubscription/123"},
                    "userErrors": [],
                }}},
            ))
            mock_cls.return_value = mock_client

            for client in _make_client(db_session):
                resp = client.post(
                    f"/api/billing/create-charge?shop={TEST_SHOP_DOMAIN}&plan=growth"
                )

        assert resp.status_code == 200
        assert resp.json()["confirmation_url"] == "https://testshop.myshopify.com/admin/confirm"

    @pytest.mark.asyncio
    async def test_shopify_returns_non_201_raises_502(self, db_session):
        """If Shopify returns non-201, the endpoint raises 502."""
        shop = make_shop(plan_tier="growth", plan_status="active")
        shop.access_token_encrypted = encrypt_token("test_tok")
        db_session.add(shop)
        await db_session.commit()

        with patch("app.routes.billing.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=MagicMock(
                status_code=422, text="Unprocessable"
            ))
            mock_cls.return_value = mock_client

            for client in _make_client(db_session):
                resp = client.post(
                    f"/api/billing/create-charge?shop={TEST_SHOP_DOMAIN}&plan=growth"
                )

        assert resp.status_code == 502


# ── /billing/callback — edge cases ────────────────────────────────────────────

class TestBillingCallbackEdgeCases:

    @pytest.mark.asyncio
    async def test_shop_not_found_returns_404(self, db_session):
        """billing_callback with an unknown shop domain returns 404."""
        for client in _make_client(db_session):
            resp = client.get(
                "/billing/callback?shop=notexist.myshopify.com&charge_id=999&plan=growth"
            )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_charge_fetch_fails_redirects_to_shopify(self, db_session):
        """When Shopify returns non-200 for charge fetch, still redirect (not crash)."""
        shop = make_shop(plan_tier="none", plan_status="pending")
        shop.access_token_encrypted = encrypt_token("test_tok")
        db_session.add(shop)
        await db_session.commit()

        with patch("app.routes.billing.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(return_value=MagicMock(
                status_code=500, text="server error"
            ))
            mock_cls.return_value = mock_client

            for client in _make_client(db_session):
                resp = client.get(
                    f"/billing/callback?shop={TEST_SHOP_DOMAIN}&charge_id=999&plan=growth",
                    follow_redirects=False,
                )

        assert resp.status_code in (302, 303, 307)

    @pytest.mark.asyncio
    async def test_activation_fails_still_redirects_without_updating_plan(self, db_session):
        """When the activation POST to Shopify fails, we still redirect and leave plan_status unchanged."""
        shop = make_shop(plan_tier="none", plan_status="pending")
        shop.access_token_encrypted = encrypt_token("test_tok")
        db_session.add(shop)
        await db_session.commit()

        with patch("app.routes.billing.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.get = AsyncMock(
                return_value=_shopify_charge_response("accepted", "999")
            )
            mock_client.post = AsyncMock(
                return_value=MagicMock(status_code=500)  # activation fails
            )
            mock_cls.return_value = mock_client

            for client in _make_client(db_session):
                resp = client.get(
                    f"/billing/callback?shop={TEST_SHOP_DOMAIN}&charge_id=999&plan=growth",
                    follow_redirects=False,
                )

        assert resp.status_code in (302, 303, 307)
        result = await db_session.execute(
            select(Shop).where(Shop.shop_domain == TEST_SHOP_DOMAIN)
        )
        assert result.scalar_one().plan_status == "pending"


# ── BillingCallback timing precision ─────────────────────────────────────────

class TestBillingCallbackTiming:

    @pytest.mark.asyncio
    async def test_billing_cycle_start_set_within_5_seconds_of_now(self, db_session):
        """billing_cycle_start must be ≈ now (within 5s) after activation."""
        shop = make_shop(plan_tier="none", plan_status="pending")
        shop.trial_used = True   # skip trial branch
        shop.access_token_encrypted = encrypt_token("tok")
        db_session.add(shop)
        await db_session.commit()

        before = datetime.now(timezone.utc)

        with patch("app.routes.billing.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=_graphql_subscription_response("ACTIVE"))
            mock_cls.return_value = mock_client

            for client in _make_client(db_session):
                client.get(
                    f"/billing/callback?shop={TEST_SHOP_DOMAIN}&charge_id=1&plan=growth",
                    follow_redirects=False,
                )

        after = datetime.now(timezone.utc)

        result = await db_session.execute(select(Shop).where(Shop.shop_domain == TEST_SHOP_DOMAIN))
        s = result.scalar_one()
        cycle = s.billing_cycle_start
        if cycle.tzinfo is None:
            cycle = cycle.replace(tzinfo=timezone.utc)
        assert before <= cycle <= after

    @pytest.mark.asyncio
    async def test_trial_duration_is_approximately_7_days(self, db_session):
        """trial_ends_at - trial_started_at must be ≈ 7 days for Growth plan."""
        shop = make_shop(plan_tier="none", plan_status="pending")
        shop.trial_used = False
        shop.access_token_encrypted = encrypt_token("tok")
        db_session.add(shop)
        await db_session.commit()

        with patch("app.routes.billing.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=_graphql_subscription_response("ACTIVE"))
            mock_cls.return_value = mock_client

            for client in _make_client(db_session):
                client.get(
                    f"/billing/callback?shop={TEST_SHOP_DOMAIN}&charge_id=1&plan=growth",
                    follow_redirects=False,
                )

        result = await db_session.execute(select(Shop).where(Shop.shop_domain == TEST_SHOP_DOMAIN))
        s = result.scalar_one()
        assert s.trial_started_at is not None
        assert s.trial_ends_at is not None
        started = s.trial_started_at
        ends = s.trial_ends_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        if ends.tzinfo is None:
            ends = ends.replace(tzinfo=timezone.utc)
        duration_days = (ends - started).total_seconds() / 86400
        # Growth trial_days = 7 — allow ±1s of timing drift
        assert abs(duration_days - 7) < 0.01

    @pytest.mark.asyncio
    async def test_declined_creates_no_billing_event(self, db_session):
        """Declined charge must NOT create a BillingEvent."""
        shop = make_shop(plan_tier="none", plan_status="pending")
        shop.billing_cycle_start = None
        shop.access_token_encrypted = encrypt_token("tok")
        db_session.add(shop)
        await db_session.commit()

        with patch("app.routes.billing.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=_graphql_subscription_response("DECLINED"))
            mock_cls.return_value = mock_client

            for client in _make_client(db_session):
                client.get(
                    f"/billing/callback?shop={TEST_SHOP_DOMAIN}&charge_id=1&plan=growth",
                    follow_redirects=False,
                )

        result = await db_session.execute(
            select(BillingEvent).where(BillingEvent.shop_id == shop.id)
        )
        events = result.scalars().all()
        assert len(events) == 0


# ── create_charge GraphQL variable validation ─────────────────────────────────

class TestCreateChargeGraphqlVariables:

    @pytest.mark.asyncio
    async def test_trial_already_used_sends_none_trial_days(self, db_session):
        """When trial_used=True, the GraphQL request must send trialDays=None."""
        shop = make_shop(plan_tier="growth", plan_status="active")
        shop.trial_used = True
        shop.access_token_encrypted = encrypt_token("tok")
        db_session.add(shop)
        await db_session.commit()

        captured_vars = []

        async def capture_post(*args, **kwargs):
            captured_vars.append(kwargs.get("json", {}).get("variables", {}))
            return MagicMock(
                status_code=200,
                json=lambda: {"data": {"appSubscriptionCreate": {
                    "confirmationUrl": "https://testshop.myshopify.com/confirm",
                    "appSubscription": {"id": "gid://shopify/AppSubscription/1"},
                    "userErrors": [],
                }}},
            )

        with patch("app.routes.billing.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(side_effect=capture_post)
            mock_cls.return_value = mock_client

            for client in _make_client(db_session):
                client.post(f"/api/billing/create-charge?shop={TEST_SHOP_DOMAIN}&plan=growth")

        assert len(captured_vars) == 1
        # trial_used=True means no trial — trialDays must be None in the mutation
        assert captured_vars[0].get("trialDays") is None

# ── Deferred plan changes (downgrades / same-tier switches) ───────────────────

from app.config import PLANS
from app.routes.billing import _should_defer_change, create_recurring_charge  # noqa: E402
from app.routes.webhooks import _handle_subscription_update  # noqa: E402


class TestShouldDeferChange:
    """The rule: only a strict upgrade (more gens) is immediate; everything else
    on an active paid plan defers to the next cycle."""

    def test_downgrade_defers(self):
        assert _should_defer_change("pro", "active", "growth") is True
        assert _should_defer_change("pro", "active", "starter") is True
        assert _should_defer_change("growth", "active", "starter") is True

    def test_upgrade_is_immediate(self):
        assert _should_defer_change("growth", "active", "pro") is False
        assert _should_defer_change("starter", "active", "pro") is False
        assert _should_defer_change("starter", "active", "growth") is False

    def test_same_tier_defers(self):
        # Same generation limit (e.g. interval switch) → not a strict upgrade → defer.
        assert _should_defer_change("pro", "active", "pro") is True

    def test_non_active_is_immediate(self):
        assert _should_defer_change("growth", "trial_active", "starter") is False
        assert _should_defer_change("none", "pending", "pro") is False
        assert _should_defer_change(None, None, "pro") is False
        assert _should_defer_change("bogus", "active", "pro") is False


def _graphql_create_response():
    return MagicMock(
        status_code=200,
        json=lambda: {"data": {"appSubscriptionCreate": {
            "confirmationUrl": "https://shop.myshopify.com/confirm", "userErrors": [],
        }}},
    )


class TestCreateChargeReplacementBehavior:

    @pytest.mark.asyncio
    async def test_downgrade_sends_defer_behavior(self):
        with patch("app.routes.billing.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=_graphql_create_response())
            mock_cls.return_value = mock_client
            await create_recurring_charge(
                "shop.myshopify.com", "tok", plan_tier="growth", with_trial=False,
                current_plan_tier="pro", current_plan_status="active",
            )
        variables = mock_client.post.call_args.kwargs["json"]["variables"]
        assert variables["replacementBehavior"] == "APPLY_ON_NEXT_BILLING_CYCLE"
        assert "deferred=1" in variables["returnUrl"]

    @pytest.mark.asyncio
    async def test_upgrade_sends_immediate_behavior(self):
        with patch("app.routes.billing.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=_graphql_create_response())
            mock_cls.return_value = mock_client
            await create_recurring_charge(
                "shop.myshopify.com", "tok", plan_tier="pro", with_trial=False,
                current_plan_tier="growth", current_plan_status="active",
            )
        variables = mock_client.post.call_args.kwargs["json"]["variables"]
        assert variables["replacementBehavior"] == "APPLY_IMMEDIATELY"
        assert "deferred=0" in variables["returnUrl"]


class TestDeferredCallback:

    @pytest.mark.asyncio
    async def test_deferred_downgrade_leaves_plan_untouched(self, db_session):
        shop = make_shop(plan_tier="pro", plan_status="active")
        shop.access_token_encrypted = encrypt_token("tok")
        shop.trial_used = True
        shop.billing_cycle_start = datetime(2026, 9, 1, tzinfo=timezone.utc)
        db_session.add(shop)
        await db_session.commit()

        with patch("app.routes.billing.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=_graphql_subscription_response("ACTIVE"))
            mock_cls.return_value = mock_client
            for client in _make_client(db_session):
                resp = client.get(
                    f"/billing/callback?shop={TEST_SHOP_DOMAIN}&charge_id=555&plan=growth&deferred=1",
                    follow_redirects=False,
                )
        assert resp.status_code in (200, 302, 303, 307)

        s = (await db_session.execute(select(Shop).where(Shop.shop_domain == TEST_SHOP_DOMAIN))).scalar_one()
        # Plan + cycle are UNCHANGED — merchant stays on Pro until cycle end.
        assert s.plan_tier == "pro"
        assert s.plan_status == "active"
        assert s.billing_cycle_start.month == 9 and s.billing_cycle_start.day == 1
        # Schedule recorded.
        assert s.scheduled_plan_tier == "growth"
        assert s.scheduled_change_at is not None
        cycle = s.billing_cycle_start
        change = s.scheduled_change_at
        if cycle.tzinfo is None:
            cycle = cycle.replace(tzinfo=timezone.utc)
        if change.tzinfo is None:
            change = change.replace(tzinfo=timezone.utc)
        assert (change - cycle).days == 30
        events = (await db_session.execute(select(BillingEvent).where(BillingEvent.shop_id == shop.id))).scalars().all()
        assert any(e.event_type == "change_scheduled" and e.plan_tier == "growth" for e in events)

    @pytest.mark.asyncio
    async def test_deferred_callback_skips_stale_schedule_if_already_applied(self, db_session):
        # Accelerated-store race: the activation webhook already applied Growth
        # before the deferred callback runs. The callback must NOT write a stale
        # "changes on X" schedule — it should detect the change already happened.
        shop = make_shop(plan_tier="growth", plan_status="active")  # webhook already flipped it
        shop.access_token_encrypted = encrypt_token("tok")
        shop.trial_used = True
        shop.billing_cycle_start = datetime(2026, 9, 1, tzinfo=timezone.utc)
        db_session.add(shop)
        await db_session.commit()

        with patch("app.routes.billing.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=_graphql_subscription_response("ACTIVE"))
            mock_cls.return_value = mock_client
            for client in _make_client(db_session):
                client.get(
                    f"/billing/callback?shop={TEST_SHOP_DOMAIN}&charge_id=557&plan=growth&deferred=1",
                    follow_redirects=False,
                )

        s = (await db_session.execute(select(Shop).where(Shop.shop_domain == TEST_SHOP_DOMAIN))).scalar_one()
        assert s.plan_tier == "growth"
        assert s.scheduled_plan_tier is None      # no stale schedule recorded
        assert s.scheduled_change_at is None

    @pytest.mark.asyncio
    async def test_immediate_change_clears_scheduled(self, db_session):
        shop = make_shop(plan_tier="growth", plan_status="active")
        shop.access_token_encrypted = encrypt_token("tok")
        shop.trial_used = True
        shop.billing_cycle_start = datetime(2026, 9, 1, tzinfo=timezone.utc)
        shop.scheduled_plan_tier = "starter"  # a stale pending downgrade
        shop.scheduled_change_at = datetime(2026, 10, 1, tzinfo=timezone.utc)
        db_session.add(shop)
        await db_session.commit()

        with patch("app.routes.billing.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=_graphql_subscription_response("ACTIVE"))
            mock_cls.return_value = mock_client
            for client in _make_client(db_session):
                client.get(
                    f"/billing/callback?shop={TEST_SHOP_DOMAIN}&charge_id=556&plan=pro&deferred=0",
                    follow_redirects=False,
                )

        s = (await db_session.execute(select(Shop).where(Shop.shop_domain == TEST_SHOP_DOMAIN))).scalar_one()
        assert s.plan_tier == "pro"
        assert s.plan_status == "active"
        assert s.scheduled_plan_tier is None
        assert s.scheduled_change_at is None


class TestWebhookClearsSchedule:

    @pytest.mark.asyncio
    async def test_activation_applies_plan_and_clears_schedule(self, db_session):
        shop = make_shop(plan_tier="pro", plan_status="active")
        shop.access_token_encrypted = encrypt_token("tok")
        shop.billing_cycle_start = datetime(2026, 9, 1, tzinfo=timezone.utc)
        shop.scheduled_plan_tier = "growth"
        shop.scheduled_change_at = datetime(2026, 10, 1, tzinfo=timezone.utc)
        db_session.add(shop)
        await db_session.commit()

        payload = {"app_subscription": {
            "status": "ACTIVE",
            "name": "GiftSense Growth Monthly Plan",
            "admin_graphql_api_id": "gid://shopify/AppSubscription/777",
        }}
        await _handle_subscription_update(TEST_SHOP_DOMAIN, payload, db_session)

        s = (await db_session.execute(select(Shop).where(Shop.shop_domain == TEST_SHOP_DOMAIN))).scalar_one()
        assert s.plan_tier == "growth"          # deferred downgrade took effect
        assert s.scheduled_plan_tier is None     # schedule cleared
        assert s.scheduled_change_at is None


# ── Issue 1: callback replay + query-param trust hardening ───────────────────

class TestCallbackReplayGuard:
    """The callback URL is unauthenticated and replayable. Re-opening it must
    NOT reset billing_cycle_start (a free monthly-quota reset), and the plan /
    interval must come from Shopify, not the editable query params."""

    @pytest.mark.asyncio
    async def test_replay_same_charge_does_not_reset_cycle(self, db_session):
        original_cycle = datetime(2026, 9, 1, tzinfo=timezone.utc)
        shop = make_shop(plan_tier="growth", plan_status="active", shopify_charge_id="999")
        shop.access_token_encrypted = encrypt_token("tok")
        shop.trial_used = True
        shop.billing_cycle_start = original_cycle
        db_session.add(shop)
        await db_session.commit()

        with patch("app.routes.billing.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(
                return_value=_graphql_subscription_response("ACTIVE", name="GiftSense Growth Monthly Plan")
            )
            mock_cls.return_value = mock_client
            for client in _make_client(db_session):
                resp = client.get(
                    f"/billing/callback?shop={TEST_SHOP_DOMAIN}&charge_id=999&plan=growth",
                    follow_redirects=False,
                )
        assert resp.status_code in (302, 303, 307)

        s = (await db_session.execute(select(Shop).where(Shop.shop_domain == TEST_SHOP_DOMAIN))).scalar_one()
        cycle = s.billing_cycle_start
        if cycle.tzinfo is None:
            cycle = cycle.replace(tzinfo=timezone.utc)
        # Cycle unchanged → quota NOT reset.
        assert cycle == original_cycle
        # No spurious BillingEvent from the replay.
        events = (await db_session.execute(select(BillingEvent).where(BillingEvent.shop_id == shop.id))).scalars().all()
        assert len(events) == 0

    @pytest.mark.asyncio
    async def test_plan_param_spoof_ignored_uses_shopify_name(self, db_session):
        """Merchant approves a Starter charge but hits the callback with plan=pro.
        The tier must come from the Shopify subscription NAME (starter), not the param."""
        shop = make_shop(plan_tier="none", plan_status="pending", shopify_charge_id=None)
        shop.access_token_encrypted = encrypt_token("tok")
        shop.trial_used = True  # skip trial branch → straight to active
        db_session.add(shop)
        await db_session.commit()

        with patch("app.routes.billing.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(
                return_value=_graphql_subscription_response("ACTIVE", name="GiftSense Starter Monthly Plan")
            )
            mock_cls.return_value = mock_client
            for client in _make_client(db_session):
                client.get(
                    f"/billing/callback?shop={TEST_SHOP_DOMAIN}&charge_id=100&plan=pro",
                    follow_redirects=False,
                )

        s = (await db_session.execute(select(Shop).where(Shop.shop_domain == TEST_SHOP_DOMAIN))).scalar_one()
        assert s.plan_tier == "starter"     # from Shopify name, NOT plan=pro param

    @pytest.mark.asyncio
    async def test_deferred_param_zero_still_defers_downgrade(self, db_session):
        """Merchant flips deferred=0 to force an immediate downgrade + quota reset.
        Deferral is recomputed server-side, so the downgrade still defers."""
        original_cycle = datetime(2026, 9, 1, tzinfo=timezone.utc)
        shop = make_shop(plan_tier="pro", plan_status="active", shopify_charge_id="pro-charge")
        shop.access_token_encrypted = encrypt_token("tok")
        shop.trial_used = True
        shop.billing_cycle_start = original_cycle
        db_session.add(shop)
        await db_session.commit()

        with patch("app.routes.billing.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(
                return_value=_graphql_subscription_response("ACTIVE", name="GiftSense Starter Monthly Plan")
            )
            mock_cls.return_value = mock_client
            for client in _make_client(db_session):
                client.get(
                    f"/billing/callback?shop={TEST_SHOP_DOMAIN}&charge_id=200&plan=starter&deferred=0",
                    follow_redirects=False,
                )

        s = (await db_session.execute(select(Shop).where(Shop.shop_domain == TEST_SHOP_DOMAIN))).scalar_one()
        # Still on Pro, cycle unchanged — the forced-immediate attempt was ignored.
        assert s.plan_tier == "pro"
        assert s.plan_status == "active"
        cycle = s.billing_cycle_start
        if cycle.tzinfo is None:
            cycle = cycle.replace(tzinfo=timezone.utc)
        assert cycle == original_cycle
        assert s.scheduled_plan_tier == "starter"   # deferred instead

    @pytest.mark.asyncio
    async def test_new_charge_upgrade_still_resets_cycle(self, db_session):
        """Regression: a genuine upgrade (new charge_id) must still reset the cycle."""
        shop = make_shop(plan_tier="growth", plan_status="active", shopify_charge_id="old-999")
        shop.access_token_encrypted = encrypt_token("tok")
        shop.trial_used = True
        shop.billing_cycle_start = datetime(2026, 9, 1, tzinfo=timezone.utc)
        db_session.add(shop)
        await db_session.commit()

        before = datetime.now(timezone.utc) - timedelta(seconds=1)
        with patch("app.routes.billing.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(
                return_value=_graphql_subscription_response("ACTIVE", name="GiftSense Pro Monthly Plan")
            )
            mock_cls.return_value = mock_client
            for client in _make_client(db_session):
                client.get(
                    f"/billing/callback?shop={TEST_SHOP_DOMAIN}&charge_id=new-123&plan=pro",
                    follow_redirects=False,
                )

        s = (await db_session.execute(select(Shop).where(Shop.shop_domain == TEST_SHOP_DOMAIN))).scalar_one()
        assert s.plan_tier == "pro"
        assert s.shopify_charge_id == "new-123"
        cycle = s.billing_cycle_start
        if cycle.tzinfo is None:
            cycle = cycle.replace(tzinfo=timezone.utc)
        assert cycle >= before   # cycle reset on genuine upgrade


class TestMonthlyOnlyCharge:
    """GiftSense is monthly-only: every charge is EVERY_30_DAYS at the plan's
    monthly price, named so the tier can be recovered from Shopify."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("tier", ["starter", "growth", "pro"])
    async def test_charge_is_monthly_with_recoverable_name(self, tier):
        with patch("app.routes.billing.httpx.AsyncClient") as mock_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=_graphql_create_response())
            mock_cls.return_value = mock_client
            await create_recurring_charge(
                "shop.myshopify.com", "tok", plan_tier=tier, with_trial=True,
            )
        variables = mock_client.post.call_args.kwargs["json"]["variables"]
        pricing = variables["lineItems"][0]["plan"]["appRecurringPricingDetails"]
        assert pricing["interval"] == "EVERY_30_DAYS"
        assert float(pricing["price"]["amount"]) == PLANS[tier]["price_usd"]
        assert variables["name"] == f"GiftSense {PLANS[tier]['name']} Monthly Plan"
        assert variables["trialDays"] == PLANS[tier]["trial_days"]
        assert "interval=" not in variables["returnUrl"]


class TestPlansCatalogForDashboard:
    """/api/plans also serves feature labels, categories and model weights from
    app/config.py, so the dashboard never hardcodes them (Commerce duplicated
    MODEL_WEIGHTS/PLAN_FEATURES in the frontend and they drifted)."""

    def test_plans_response_includes_display_catalog(self, db_session):
        from app.config import FEATURE_CATEGORIES, FEATURE_LABELS, MODEL_WEIGHTS
        for client in _make_client(db_session):
            data = client.get("/api/plans").json()
        assert data["feature_labels"] == FEATURE_LABELS
        assert data["feature_categories"] == FEATURE_CATEGORIES
        assert data["model_weights"] == MODEL_WEIGHTS
        growth = next(p for p in data["plans"] if p["tier"] == "growth")
        assert "arrive_by" in growth["features"]
        assert growth["max_products"] == PLANS["growth"]["max_products"]

    def test_every_plan_feature_has_a_label_and_category(self):
        from app.config import FEATURE_CATEGORIES, FEATURE_LABELS
        categorized = {f for feats in FEATURE_CATEGORIES.values() for f in feats}
        for tier, plan in PLANS.items():
            for f in plan["features"]:
                assert f in FEATURE_LABELS, f"{tier}: {f} has no label"
                assert f in categorized, f"{tier}: {f} has no category"
