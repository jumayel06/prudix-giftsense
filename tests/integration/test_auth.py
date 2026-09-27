"""
Integration tests for GET /auth and GET /auth/callback.

Tests the full OAuth install flow — nonce generation, HMAC verification,
shop creation vs update, and billing redirect on success.
"""

import hashlib
import hmac as hmac_lib
import time
from datetime import datetime, timezone, timedelta

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from app.main import app
from core.db.session import get_db
from core.db.models import Shop
from core.shopify_auth import encrypt_token
from tests.conftest import make_shop, TEST_SHOP_DOMAIN, TEST_API_SECRET


def _make_client(db_session):
    from fastapi.testclient import TestClient

    async def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app, raise_server_exceptions=True)
    yield client
    app.dependency_overrides.clear()


def _valid_hmac(params: dict, secret: str = TEST_API_SECRET) -> str:
    """Generate a valid Shopify HMAC for the given params (excluding 'hmac' key)."""
    filtered = {k: v for k, v in params.items() if k != "hmac"}
    sorted_str = "&".join(f"{k}={v}" for k, v in sorted(filtered.items()))
    return hmac_lib.new(secret.encode(), sorted_str.encode(), hashlib.sha256).hexdigest()


# ── GET /auth ─────────────────────────────────────────────────────────────────

class TestAuthStart:

    def test_valid_shop_redirects(self, db_session):
        for client in _make_client(db_session):
            resp = client.get("/auth?shop=mystore.myshopify.com", follow_redirects=False)
        assert resp.status_code in (302, 307)

    def test_redirect_url_contains_shop(self, db_session):
        for client in _make_client(db_session):
            resp = client.get("/auth?shop=mystore.myshopify.com", follow_redirects=False)
        location = resp.headers.get("location", "")
        assert "mystore.myshopify.com" in location

    def test_redirect_url_contains_oauth_path(self, db_session):
        for client in _make_client(db_session):
            resp = client.get("/auth?shop=mystore.myshopify.com", follow_redirects=False)
        location = resp.headers.get("location", "")
        assert "oauth/authorize" in location

    def test_invalid_domain_returns_400(self, db_session):
        for client in _make_client(db_session):
            resp = client.get("/auth?shop=evil.com")
        assert resp.status_code == 400


# ── GET /auth/callback ────────────────────────────────────────────────────────

class TestAuthCallback:

    def _prime_nonce(self, client, shop: str = "newshop.myshopify.com") -> str:
        """Hit /auth to store a nonce, then return that nonce."""
        resp = client.get(f"/auth?shop={shop}", follow_redirects=False)
        location = resp.headers.get("location", "")
        # Extract state from redirect URL
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(location).query)
        return qs.get("state", [""])[0]

    @pytest.mark.asyncio
    async def test_invalid_state_returns_403(self, db_session):
        shop = "newshop.myshopify.com"
        params = {"shop": shop, "code": "auth_code", "state": "wrong_nonce", "timestamp": str(int(time.time()))}
        params["hmac"] = _valid_hmac(params)

        for client in _make_client(db_session):
            resp = client.get("/auth/callback", params=params)
        assert resp.status_code == 403
        assert "CSRF" in resp.json()["detail"] or "state" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_invalid_hmac_returns_403(self, db_session):
        shop = "newshop.myshopify.com"

        # Prime nonce by calling /auth first
        for client in _make_client(db_session):
            nonce = self._prime_nonce(client, shop)
            params = {"shop": shop, "code": "auth_code", "state": nonce, "timestamp": str(int(time.time())), "hmac": "badhmacsig"}
            resp = client.get("/auth/callback", params=params)
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_new_shop_created_on_successful_callback(self, db_session):
        """New install: all shop columns set correctly, redirected to embedded app."""
        shop = "brandnew.myshopify.com"

        with patch("app.routes.auth.exchange_code_for_token", AsyncMock(return_value={"access_token": "shpat_real_token", "refresh_token": None, "access_token_expires_at": None, "refresh_token_expires_at": None})):
            for client in _make_client(db_session):
                nonce = self._prime_nonce(client, shop)
                params = {"shop": shop, "code": "real_code", "state": nonce, "timestamp": str(int(time.time()))}
                params["hmac"] = _valid_hmac(params)
                resp = client.get("/auth/callback", params=params, follow_redirects=False)

        assert resp.status_code in (200, 302, 307)
        assert "admin/apps" in resp.headers.get("location", "")

        result = await db_session.execute(select(Shop).where(Shop.shop_domain == shop))
        s = result.scalar_one_or_none()
        assert s is not None

        # Identity
        assert s.shop_domain == shop

        # Billing state — nothing billed yet, merchant must choose plan
        assert s.plan_status == "pending"
        assert s.plan_tier == "none"
        assert s.shopify_charge_id is None
        assert s.billing_cycle_start is None

        # Trial — not started yet
        assert s.trial_used is False
        assert s.trial_started_at is None
        assert s.trial_ends_at is None
        assert s.grace_period_ends_at is None

        # Token — encrypted from the OAuth exchange; refresh is None (mock)
        assert s.access_token_encrypted != ""
        assert s.refresh_token_encrypted is None
        assert s.access_token_expires_at is None
        assert s.refresh_token_expires_at is None

        # Install/uninstall timestamps
        assert s.installed_at is not None
        assert s.uninstalled_at is None
        assert s.data_purge_at is None

        # Defaults
        assert s.selected_model == "claude-haiku-4-5"
        assert s.store_timezone == "UTC"
        assert s.review_prompt_shown is False

    @pytest.mark.asyncio
    async def test_existing_shop_token_updated_on_reinstall(self, db_session):
        """Reinstall: token refreshed, status reset to pending, trial_used preserved,
        uninstalled_at cleared, installed_at updated."""
        from core.shopify_auth import decrypt_token
        from datetime import timedelta
        shop_domain = "returning.myshopify.com"
        existing = make_shop(domain=shop_domain, plan_tier="growth", plan_status="uninstalled")
        existing.access_token_encrypted = encrypt_token("old_token")
        existing.refresh_token_encrypted = encrypt_token("old_refresh")
        existing.trial_used = True
        existing.uninstalled_at = datetime.now(timezone.utc) - timedelta(days=1)  # was uninstalled
        # Seed non-default values that reinstall must NOT reset
        existing.selected_model = "gpt-4o-mini"       # non-default
        existing.store_timezone = "America/New_York"  # non-default
        existing.review_prompt_shown = True            # non-default
        existing.judgeme_api_token_encrypted = encrypt_token("jm_tok_preserved")
        db_session.add(existing)
        await db_session.commit()

        with patch("app.routes.auth.exchange_code_for_token", AsyncMock(return_value={"access_token": "shpat_new_token", "refresh_token": None, "access_token_expires_at": None, "refresh_token_expires_at": None})):
            for client in _make_client(db_session):
                nonce = self._prime_nonce(client, shop_domain)
                params = {"shop": shop_domain, "code": "new_code", "state": nonce, "timestamp": str(int(time.time()))}
                params["hmac"] = _valid_hmac(params)
                resp = client.get("/auth/callback", params=params, follow_redirects=False)

        await db_session.refresh(existing)

        # Status reset for plan selection
        assert existing.plan_status == "pending"
        assert existing.plan_tier == "none"

        # trial_used preserved — no second free trial
        assert existing.trial_used is True

        # Token updated to new value
        assert decrypt_token(existing.access_token_encrypted) == "shpat_new_token"
        # refresh_token from mock is None → should be None now
        assert existing.refresh_token_encrypted is None

        # Reinstall timestamps
        assert existing.installed_at is not None
        assert existing.uninstalled_at is None  # cleared on reinstall

        # Billing state cleared — must choose plan again
        assert existing.shopify_charge_id is None
        assert existing.billing_cycle_start is None
        assert existing.trial_started_at is None
        assert existing.trial_ends_at is None
        assert existing.grace_period_ends_at is None
        assert existing.data_purge_at is None

        # Fields NOT reset on reinstall — preserved across the uninstall/reinstall cycle
        assert existing.selected_model == "gpt-4o-mini"        # merchant's preference kept
        assert existing.store_timezone == "America/New_York"   # timezone kept
        assert existing.review_prompt_shown is True            # prompt state kept
        # Judge.me integration token preserved — reinstall does not wipe configured integrations
        from core.shopify_auth import decrypt_token as _dt2
        assert _dt2(existing.judgeme_api_token_encrypted) == "jm_tok_preserved"
