"""Tests for the /health endpoint.

Validates the dependency-pinging behavior: 200 when all components reachable,
503 when any is down. The endpoint is what Railway/Cloudflare hit to decide
whether to route traffic to this instance.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.main import app


async def _client():
    from httpx import ASGITransport
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


def _ok_db_conn():
    """Mock an `engine.connect()` context manager that yields a conn whose
    `execute(SELECT 1)` succeeds."""
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=MagicMock())
    cm = AsyncMock()
    cm.__aenter__ = AsyncMock(return_value=conn)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _ok_redis_client():
    client = AsyncMock()
    client.ping = AsyncMock(return_value=True)
    client.close = AsyncMock()
    return client


class TestHealth:

    @pytest.mark.asyncio
    async def test_returns_200_when_all_components_ok(self):
        engine_ok = MagicMock()
        engine_ok.connect = MagicMock(return_value=_ok_db_conn())
        with patch("core.db.session.engine", engine_ok), \
             patch("redis.asyncio.from_url", return_value=_ok_redis_client()):
            async for c in _client():
                resp = await c.get("/health")

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["components"]["database"]["status"] == "ok"
        assert body["components"]["redis"]["status"] == "ok"
        assert body["app"] == "giftsense"

    @pytest.mark.asyncio
    async def test_returns_503_when_db_down(self):
        broken_conn = AsyncMock()
        broken_conn.__aenter__ = AsyncMock(side_effect=Exception("db down"))
        broken_conn.__aexit__ = AsyncMock(return_value=False)
        engine_broken = MagicMock()
        engine_broken.connect = MagicMock(return_value=broken_conn)

        with patch("core.db.session.engine", engine_broken), \
             patch("redis.asyncio.from_url", return_value=_ok_redis_client()):
            async for c in _client():
                resp = await c.get("/health")

        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "degraded"
        assert body["components"]["database"]["status"] == "error"
        assert "db down" in body["components"]["database"]["error"]
        # Redis still reports ok independently — multi-component visibility
        assert body["components"]["redis"]["status"] == "ok"

    @pytest.mark.asyncio
    async def test_returns_503_when_redis_down(self):
        broken_redis = AsyncMock()
        broken_redis.ping = AsyncMock(side_effect=Exception("redis down"))
        broken_redis.close = AsyncMock()

        engine_ok = MagicMock()
        engine_ok.connect = MagicMock(return_value=_ok_db_conn())
        with patch("core.db.session.engine", engine_ok), \
             patch("redis.asyncio.from_url", return_value=broken_redis):
            async for c in _client():
                resp = await c.get("/health")

        assert resp.status_code == 503
        body = resp.json()
        assert body["status"] == "degraded"
        assert body["components"]["database"]["status"] == "ok"
        assert body["components"]["redis"]["status"] == "error"
        assert "redis down" in body["components"]["redis"]["error"]

    @pytest.mark.asyncio
    async def test_debug_endpoint_removed(self):
        """/debug was deleted (leaked app_host + truncated client_id). It must
        not exist in ANY environment now — not even dev, which is exposed on a
        public tunnel."""
        from core.config import settings
        original = settings.app_env
        settings.app_env = "development"
        try:
            async for c in _client():
                resp = await c.get("/debug")
            assert resp.status_code == 404
        finally:
            settings.app_env = original

    @pytest.mark.asyncio
    async def test_docs_not_public(self):
        """Swagger UI is admin-gated in every environment — anonymous access is
        rejected (401 with creds configured, 503 when they aren't). Never 200."""
        async for c in _client():
            resp = await c.get("/docs")
        assert resp.status_code in (401, 503)

    @pytest.mark.asyncio
    async def test_openapi_schema_not_public(self):
        """The OpenAPI schema is admin-gated too — the browser reuses admin creds
        to load it from the Swagger page. Anonymous access never returns 200."""
        async for c in _client():
            resp = await c.get("/openapi.json")
        assert resp.status_code in (401, 503)

    @pytest.mark.asyncio
    async def test_returns_503_when_both_down(self):
        """Both components down → still 503, both errors surfaced."""
        broken_db = AsyncMock()
        broken_db.__aenter__ = AsyncMock(side_effect=Exception("db down"))
        broken_db.__aexit__ = AsyncMock(return_value=False)
        engine_broken = MagicMock()
        engine_broken.connect = MagicMock(return_value=broken_db)
        broken_redis = AsyncMock()
        broken_redis.ping = AsyncMock(side_effect=Exception("redis down"))
        broken_redis.close = AsyncMock()

        with patch("core.db.session.engine", engine_broken), \
             patch("redis.asyncio.from_url", return_value=broken_redis):
            async for c in _client():
                resp = await c.get("/health")

        assert resp.status_code == 503
        body = resp.json()
        assert body["components"]["database"]["status"] == "error"
        assert body["components"]["redis"]["status"] == "error"
