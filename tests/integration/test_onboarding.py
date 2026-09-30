"""Home onboarding checklist: GET /api/onboarding, POST /api/onboarding/dismiss."""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from core.db.models import GiftSession, UsageLog
from core.db.session import get_db
from tests.conftest import TEST_SHOP_DOMAIN, make_shop
from tests.integration.test_playground import row


def call(db_session, method, path, embed="missing"):
    async def override_db():
        yield db_session
    app.dependency_overrides[get_db] = override_db
    try:
        with patch("app.routes.onboarding.get_valid_access_token", AsyncMock(return_value="tok")), \
             patch("app.routes.onboarding.fetch_embed_status", AsyncMock(return_value={"embed": embed, "theme_name": "Dawn"})):
            return TestClient(app).request(method, f"{path}?shop={TEST_SHOP_DOMAIN}")
    finally:
        app.dependency_overrides.clear()


def steps(data):
    return {s["id"]: s["done"] for s in data["steps"]}


@pytest.mark.asyncio
async def test_new_shop_has_only_the_plan_step_done(db_session):
    db_session.add(make_shop())
    await db_session.commit()
    data = call(db_session, "GET", "/api/onboarding").json()
    assert steps(data) == {"plan": True, "catalog": False, "try_it": False, "storefront": False, "first_search": False}
    assert data["show"] is True and data["done_count"] == 1


@pytest.mark.asyncio
async def test_steps_complete_from_real_activity(db_session):
    shop = make_shop()
    db_session.add(shop)
    await db_session.flush()
    db_session.add(row(shop, "1"))
    db_session.add(UsageLog(id=uuid.uuid4(), shop_id=shop.id, action_type="playground", generations_consumed=0,
                            model_used="gpt-6-luna"))
    db_session.add(GiftSession(shop_id=shop.id, sid=uuid.uuid4(), intake={}, last_picks=[], searches=1))
    await db_session.commit()
    data = call(db_session, "GET", "/api/onboarding", embed="on").json()
    assert all(steps(data).values()) and data["show"] is False


@pytest.mark.asyncio
async def test_dismiss_hides_the_checklist(db_session):
    db_session.add(make_shop())
    await db_session.commit()
    assert call(db_session, "POST", "/api/onboarding/dismiss").status_code == 200
    assert call(db_session, "GET", "/api/onboarding").json()["show"] is False
