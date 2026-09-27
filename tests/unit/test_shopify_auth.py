"""Unit tests for core/shopify_auth.py — encryption, HMAC, and OAuth helpers."""

import base64
import hashlib
import hmac as hmac_lib

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from core.shopify_auth import (
    build_oauth_url,
    decrypt_token,
    encrypt_token,
    exchange_code_for_token,
    verify_hmac,
)
from tests.conftest import TEST_API_SECRET


class TestEncryption:

    def test_round_trip(self):
        token = "shopify_offline_token_abc123"
        assert decrypt_token(encrypt_token(token)) == token

    def test_encrypted_differs_from_plaintext(self):
        token = "my_secret_token"
        encrypted = encrypt_token(token)
        assert token not in encrypted

    def test_different_calls_produce_different_ciphertexts(self):
        # Fernet uses a random IV each time
        token = "same_token"
        assert encrypt_token(token) != encrypt_token(token)

    def test_empty_string_round_trips(self):
        assert decrypt_token(encrypt_token("")) == ""


class TestBuildOauthUrl:

    def test_contains_shop_domain(self):
        url = build_oauth_url("mystore.myshopify.com", "nonce123")
        assert "mystore.myshopify.com" in url

    def test_contains_nonce_as_state(self):
        url = build_oauth_url("mystore.myshopify.com", "abc_nonce")
        assert "abc_nonce" in url

    def test_contains_read_products_scope(self):
        url = build_oauth_url("mystore.myshopify.com", "nonce")
        assert "read_products" in url

    def test_is_shopify_oauth_endpoint(self):
        url = build_oauth_url("mystore.myshopify.com", "nonce")
        assert "/admin/oauth/authorize" in url
        assert url.startswith("https://mystore.myshopify.com")

    def test_contains_redirect_uri(self):
        url = build_oauth_url("mystore.myshopify.com", "nonce")
        assert "redirect_uri" in url


class TestVerifyHmac:

    def _make_valid_params(self, extra: dict | None = None) -> dict:
        params = {"shop": "test.myshopify.com", "timestamp": "1700000000"}
        if extra:
            params.update(extra)
        sorted_str = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        digest = hmac_lib.new(
            TEST_API_SECRET.encode(), sorted_str.encode(), hashlib.sha256
        ).hexdigest()
        params["hmac"] = digest
        return params

    def test_valid_hmac_returns_true(self):
        params = self._make_valid_params()
        assert verify_hmac(params) is True

    def test_wrong_hmac_returns_false(self):
        params = {"shop": "test.myshopify.com", "timestamp": "1700000000", "hmac": "deadbeef"}
        assert verify_hmac(params) is False

    def test_missing_hmac_returns_false(self):
        params = {"shop": "test.myshopify.com", "timestamp": "1700000000"}
        assert verify_hmac(params) is False

    def test_tampered_param_returns_false(self):
        params = self._make_valid_params()
        params["shop"] = "evil.myshopify.com"  # tamper after signing
        assert verify_hmac(params) is False

    def test_hmac_key_consumed_from_params(self):
        # verify_hmac pops "hmac" from params — dict should not contain it after
        params = self._make_valid_params()
        verify_hmac(params)
        assert "hmac" not in params


class TestExchangeCodeForToken:

    @pytest.mark.asyncio
    async def test_successful_exchange_returns_token(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"access_token": "shpat_abc123"}

        with patch("core.shopify_auth.httpx.AsyncClient") as mock_cls:
            mock_http = AsyncMock()
            mock_http.__aenter__ = AsyncMock(return_value=mock_http)
            mock_http.__aexit__ = AsyncMock(return_value=False)
            mock_http.post = AsyncMock(return_value=mock_resp)
            mock_cls.return_value = mock_http

            token = await exchange_code_for_token("mystore.myshopify.com", "auth_code_xyz")

        assert token["access_token"] == "shpat_abc123"

    @pytest.mark.asyncio
    async def test_posts_to_shopify_access_token_endpoint(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"access_token": "tok"}

        with patch("core.shopify_auth.httpx.AsyncClient") as mock_cls:
            mock_http = AsyncMock()
            mock_http.__aenter__ = AsyncMock(return_value=mock_http)
            mock_http.__aexit__ = AsyncMock(return_value=False)
            mock_http.post = AsyncMock(return_value=mock_resp)
            mock_cls.return_value = mock_http

            await exchange_code_for_token("mystore.myshopify.com", "code")

        call_args = mock_http.post.call_args
        assert "mystore.myshopify.com" in call_args[0][0]
        assert "access_token" in call_args[0][0]
