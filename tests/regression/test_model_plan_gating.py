"""
REGRESSION: Model selection must be validated server-side, not just in the UI.

The ModelPicker in SettingsPage.jsx grays out locked models — but a merchant
(or anyone with the API) could call PUT /api/settings with a model not in
their plan's models_available list and bypass the frontend gate.

Fixed in: app/routes/settings.py — PUT /api/settings validates selected_model
against PLANS[plan_tier]["models_available"] and raises 403 if unavailable.

Also tests: the model is NOT persisted if the 403 is raised.
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
    async def test_starter_cannot_select_gpt6_sol(self, db_session):
        """GPT-6 Sol is Growth+; not in Starter plan's models_available."""
        shop = make_shop(plan_tier="starter", plan_status="active", selected_model="gpt-6-luna")
        db_session.add(shop)
        await db_session.commit()

        for client in _make_client(db_session):
            resp = client.put(
                f"/api/settings?shop={TEST_SHOP_DOMAIN}",
                json={"selected_model": "gpt-6-sol"},
            )
        assert resp.status_code == 403
        detail = resp.json()["detail"]
        assert detail["code"] == "model_not_available"

    @pytest.mark.asyncio
    async def test_starter_cannot_select_claude_sonnet(self, db_session):
        shop = make_shop(plan_tier="starter", plan_status="active", selected_model="gpt-6-luna")
        db_session.add(shop)
        await db_session.commit()

        for client in _make_client(db_session):
            resp = client.put(
                f"/api/settings?shop={TEST_SHOP_DOMAIN}",
                json={"selected_model": "claude-sonnet-5"},
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_blocked_model_not_persisted(self, db_session):
        """After a 403, the shop's selected_model must be unchanged."""
        from sqlalchemy import select
        from core.db.models import Shop

        shop = make_shop(plan_tier="starter", plan_status="active", selected_model="gpt-6-luna")
        db_session.add(shop)
        await db_session.commit()

        for client in _make_client(db_session):
            client.put(
                f"/api/settings?shop={TEST_SHOP_DOMAIN}",
                json={"selected_model": "claude-sonnet-5"},
            )

        result = await db_session.execute(
            select(Shop).where(Shop.shop_domain == TEST_SHOP_DOMAIN)
        )
        refreshed = result.scalar_one()
        assert refreshed.selected_model == "gpt-6-luna"  # unchanged

    @pytest.mark.asyncio
    async def test_growth_can_select_gpt6_sol(self, db_session):
        shop = make_shop(plan_tier="growth", plan_status="active", selected_model="gpt-6-luna")
        db_session.add(shop)
        await db_session.commit()

        for client in _make_client(db_session):
            resp = client.put(
                f"/api/settings?shop={TEST_SHOP_DOMAIN}",
                json={"selected_model": "gpt-6-sol"},
            )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_growth_cannot_select_claude_sonnet(self, db_session):
        """Claude Sonnet 5 is Pro-only."""
        shop = make_shop(plan_tier="growth", plan_status="active", selected_model="gpt-6-luna")
        db_session.add(shop)
        await db_session.commit()

        for client in _make_client(db_session):
            resp = client.put(
                f"/api/settings?shop={TEST_SHOP_DOMAIN}",
                json={"selected_model": "claude-sonnet-5"},
            )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_pro_can_select_claude_sonnet(self, db_session):
        shop = make_shop(plan_tier="pro", plan_status="active", selected_model="gpt-6-luna")
        db_session.add(shop)
        await db_session.commit()

        for client in _make_client(db_session):
            resp = client.put(
                f"/api/settings?shop={TEST_SHOP_DOMAIN}",
                json={"selected_model": "claude-sonnet-5"},
            )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_all_plans_have_baseline_models(self):
        """GPT-4o mini and Claude Haiku 4.5 are available on every plan."""
        for tier, plan in PLANS.items():
            for model in ("gpt-6-luna", "claude-haiku-4-5"):
                assert model in plan["models_available"], (
                    f"Plan '{tier}' is missing {model} from models_available"
                )

    @pytest.mark.asyncio
    async def test_models_available_per_plan_matches_config(self):
        """Document the expected model availability for each plan tier."""
        assert set(PLANS["starter"]["models_available"]) == {"gpt-6-luna", "claude-haiku-4-5"}
        assert "gpt-6-sol" in PLANS["growth"]["models_available"]
        assert "claude-sonnet-5" not in PLANS["growth"]["models_available"]
        assert "claude-sonnet-5" in PLANS["pro"]["models_available"]


class TestSettingsPayload:

    @pytest.mark.asyncio
    async def test_get_settings_lists_plan_models_weights_and_features(self, db_session):
        shop = make_shop(plan_tier="growth", plan_status="active", selected_model="gpt-6-sol")
        db_session.add(shop)
        await db_session.commit()

        for client in _make_client(db_session):
            data = client.get(f"/api/settings?shop={TEST_SHOP_DOMAIN}").json()
        assert data["selected_model"] == "gpt-6-sol"
        assert data["models_available"] == PLANS["growth"]["models_available"]
        assert data["model_weights"]["gpt-6-sol"] == 4
        assert data["features"] == PLANS["growth"]["features"]
