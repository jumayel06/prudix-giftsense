"""
REGRESSION: AI tier selection must be validated server-side, not just in the UI.

The picker in SettingsPage.jsx grays out locked AI tiers, but a merchant (or
anyone with the API) could call PUT /api/settings with a tier outside their
plan and bypass the frontend gate.

Fixed in: app/routes/settings.py — PUT /api/settings validates ai_tier
against PLANS[plan_tier]["ai_tiers"] and raises 403 if unavailable.

Also tests: the choice is NOT persisted if the 403 is raised.
"""

from unittest.mock import AsyncMock, MagicMock
import pytest
import pytest_asyncio

from app.main import app
from core.db.session import get_db
from app.config import PLANS
from tests.conftest import make_shop, TEST_SHOP_DOMAIN


def _make_client(db_session):
    from fastapi.testclient import TestClient

    async def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app, raise_server_exceptions=True)
    yield client
    app.dependency_overrides.clear()


class TestModelPlanGating:

    @pytest.mark.asyncio
    async def test_starter_cannot_select_advanced(self, db_session):
        """Advanced is Growth+; not in the Starter plan."""
        shop = make_shop(plan_tier="starter", plan_status="active", selected_model="standard")
        db_session.add(shop)
        await db_session.commit()

        for client in _make_client(db_session):
            resp = client.put(
                f"/api/settings?shop={TEST_SHOP_DOMAIN}",
                json={"ai_tier": "advanced"},
            )
        assert resp.status_code == 403
        detail = resp.json()["detail"]
        assert detail["code"] == "ai_tier_not_available"

    @pytest.mark.asyncio
    async def test_starter_cannot_select_premium(self, db_session):
        shop = make_shop(plan_tier="starter", plan_status="active", selected_model="standard")
        db_session.add(shop)
        await db_session.commit()

        for client in _make_client(db_session):
            resp = client.put(
                f"/api/settings?shop={TEST_SHOP_DOMAIN}",
                json={"ai_tier": "premium"},
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_blocked_model_not_persisted(self, db_session):
        """After a 403, the shop's selected_model must be unchanged."""
        from sqlalchemy import select
        from core.db.models import Shop

        shop = make_shop(plan_tier="starter", plan_status="active", selected_model="standard")
        db_session.add(shop)
        await db_session.commit()

        for client in _make_client(db_session):
            client.put(
                f"/api/settings?shop={TEST_SHOP_DOMAIN}",
                json={"ai_tier": "premium"},
            )

        result = await db_session.execute(
            select(Shop).where(Shop.shop_domain == TEST_SHOP_DOMAIN)
        )
        refreshed = result.scalar_one()
        assert refreshed.selected_model == "standard"  # unchanged

    @pytest.mark.asyncio
    async def test_growth_can_select_advanced(self, db_session):
        shop = make_shop(plan_tier="growth", plan_status="active", selected_model="standard")
        db_session.add(shop)
        await db_session.commit()

        for client in _make_client(db_session):
            resp = client.put(
                f"/api/settings?shop={TEST_SHOP_DOMAIN}",
                json={"ai_tier": "advanced"},
            )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_growth_cannot_select_premium(self, db_session):
        """Premium is Pro-only."""
        shop = make_shop(plan_tier="growth", plan_status="active", selected_model="standard")
        db_session.add(shop)
        await db_session.commit()

        for client in _make_client(db_session):
            resp = client.put(
                f"/api/settings?shop={TEST_SHOP_DOMAIN}",
                json={"ai_tier": "premium"},
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_pro_can_select_premium(self, db_session):
        shop = make_shop(plan_tier="pro", plan_status="active", selected_model="standard")
        db_session.add(shop)
        await db_session.commit()

        for client in _make_client(db_session):
            resp = client.put(
                f"/api/settings?shop={TEST_SHOP_DOMAIN}",
                json={"ai_tier": "premium"},
            )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_all_plans_have_the_standard_tier(self):
        for tier, plan in PLANS.items():
            assert "standard" in plan["ai_tiers"], f"Plan '{tier}' is missing the Standard AI tier"

    @pytest.mark.asyncio
    async def test_models_available_per_plan_matches_config(self):
        """Document the expected AI tiers for each plan."""
        assert PLANS["starter"]["ai_tiers"] == ["standard"]
        assert "advanced" in PLANS["growth"]["ai_tiers"]
        assert "premium" not in PLANS["growth"]["ai_tiers"]
        assert "premium" in PLANS["pro"]["ai_tiers"]


class TestSettingsPayload:

    @pytest.mark.asyncio
    async def test_get_settings_lists_plan_models_weights_and_features(self, db_session):
        shop = make_shop(plan_tier="growth", plan_status="active", selected_model="advanced")
        db_session.add(shop)
        await db_session.commit()

        for client in _make_client(db_session):
            data = client.get(f"/api/settings?shop={TEST_SHOP_DOMAIN}").json()
        assert data["ai_tier"] == "advanced" and data["ai_tier_weight"] == 2
        assert data["ai_tiers_available"] == PLANS["growth"]["ai_tiers"]
        assert data["ai_tiers"]["advanced"]["label"] == "Advanced"
        # The store sees which model each of its options runs on right now (a
        # display label), but never raw model IDs it could send back.
        assert data["ai_tier_models"] == {"standard": "GPT-6 Luna", "advanced": "GPT-6 Sol"}
        assert "gpt-6" not in str(data) and "claude-" not in str(data)
        assert data["features"] == PLANS["growth"]["features"]
