"""GET /api/theme/status (dashboard): app embed state for the Storefront page."""
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from core.db.session import get_db
from tests.conftest import TEST_SHOP_DOMAIN, make_shop


@pytest.mark.asyncio
async def test_theme_status_route(db_session):
    db_session.add(make_shop())
    await db_session.commit()

    async def override_db():
        yield db_session
    app.dependency_overrides[get_db] = override_db
    try:
        with patch("app.routes.theme.get_valid_access_token", AsyncMock(return_value="tok")), \
             patch("app.routes.theme.fetch_embed_status", AsyncMock(return_value={"embed": "off", "theme_name": "Dawn"})):
            data = TestClient(app).get(f"/api/theme/status?shop={TEST_SHOP_DOMAIN}").json()
    finally:
        app.dependency_overrides.clear()
    assert data == {"embed": "off", "theme_name": "Dawn"}
