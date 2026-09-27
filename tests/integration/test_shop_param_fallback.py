"""SECURITY: the `?shop=` fallback in get_current_shop is for local dev only.

It resolves a shop from an unauthenticated query param. In production that
would let anyone act as any installed store (read settings, change the model,
start billing), so it must be refused there. Every real embedded request
carries an App Bridge session token instead.

Found 2026-09-27 while porting from Prudix Commerce, where the fallback is not
gated on the environment.
"""
import pytest
from fastapi.testclient import TestClient

from app.main import app
from core.db.session import get_db
from tests.conftest import TEST_SHOP_DOMAIN, make_shop


def _client(db_session):
    async def override_db():
        yield db_session
    app.dependency_overrides[get_db] = override_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_shop_param_rejected_in_production(db_session, monkeypatch):
    from core import config as core_config
    monkeypatch.setattr(core_config.settings, "app_env", "production")
    db_session.add(make_shop(plan_status="active", plan_tier="growth"))
    await db_session.commit()

    for client in _client(db_session):
        resp = client.get(f"/api/settings?shop={TEST_SHOP_DOMAIN}")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_shop_param_allowed_outside_production(db_session):
    db_session.add(make_shop(plan_status="active", plan_tier="growth"))
    await db_session.commit()

    for client in _client(db_session):
        resp = client.get(f"/api/settings?shop={TEST_SHOP_DOMAIN}")
    assert resp.status_code == 200
