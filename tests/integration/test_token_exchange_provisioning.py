"""
Tests for managed-install / token-exchange shop provisioning in
`core.shopify_deps.get_current_shop`.

Under Shopify managed installation, Shopify performs the OAuth grant itself and
loads the embedded app — `/auth/callback` never runs, so the shop row isn't
created there. On the first authenticated API call, `get_current_shop` exchanges
the App Bridge session token for an offline access token and provisions the row
with `plan_status='pending'` (so the frontend routes to the plan picker).
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select

from core.db.models import Shop
from core.shopify_auth import decrypt_token, encrypt_token
from core.shopify_deps import _provision_shop_via_token_exchange, get_current_shop
from tests.conftest import make_shop

_DEPS = "core.shopify_deps"


def _creds(token: str = "fake.session.jwt") -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def _token_data(access: str = "shpat_offline_token", refresh: str = "shprt_refresh"):
    """Shape returned by exchange_session_token_for_offline_token (expiring offline token)."""
    now = datetime.now(timezone.utc)
    return {
        "access_token": access,
        "refresh_token": refresh,
        "access_token_expires_at": now + timedelta(hours=1),
        "refresh_token_expires_at": now + timedelta(days=90),
    }


@pytest.mark.asyncio
async def test_first_load_provisions_shop_via_token_exchange(db_session):
    """No row yet + valid session token → exchange → new row with pending status."""
    shop = "freshinstall.myshopify.com"

    with patch(f"{_DEPS}.get_shop_from_session_token", return_value=shop), \
         patch(f"{_DEPS}.exchange_session_token_for_offline_token",
               AsyncMock(return_value=_token_data("shpat_offline_token"))) as exch, \
         patch(f"{_DEPS}._fetch_and_store_shop_meta", AsyncMock()):
        result = await get_current_shop(credentials=_creds(), shop=None, db=db_session)

    exch.assert_awaited_once()
    assert result.shop_domain == shop
    assert result.plan_status == "pending"
    assert result.plan_tier == "none"
    # Expiring offline token stored WITH expiry + refresh (Shopify rejects
    # non-expiring tokens on the Admin API).
    assert decrypt_token(result.access_token_encrypted) == "shpat_offline_token"
    assert result.access_token_expires_at is not None
    assert decrypt_token(result.refresh_token_encrypted) == "shprt_refresh"

    row = (await db_session.execute(select(Shop).where(Shop.shop_domain == shop))).scalar_one()
    assert row.plan_status == "pending"


@pytest.mark.asyncio
async def test_existing_shop_returned_without_token_exchange(db_session):
    """Already-provisioned shop → returned as-is, no token exchange call."""
    existing = make_shop(domain="already.myshopify.com", plan_tier="pro", plan_status="active")
    db_session.add(existing)
    await db_session.commit()

    with patch(f"{_DEPS}.get_shop_from_session_token", return_value="already.myshopify.com"), \
         patch(f"{_DEPS}.exchange_session_token_for_offline_token", AsyncMock()) as exch:
        result = await get_current_shop(credentials=_creds(), shop=None, db=db_session)

    exch.assert_not_awaited()
    assert result.shop_domain == "already.myshopify.com"
    assert result.plan_status == "active"


@pytest.mark.asyncio
async def test_shop_param_fallback_without_row_still_404s(db_session):
    """?shop= dev fallback has no session token to exchange → still 404, never provisions."""
    with patch(f"{_DEPS}.exchange_session_token_for_offline_token", AsyncMock()) as exch:
        with pytest.raises(HTTPException) as exc:
            await get_current_shop(credentials=None, shop="unknown.myshopify.com", db=db_session)

    assert exc.value.status_code == 404
    exch.assert_not_awaited()


@pytest.mark.asyncio
async def test_token_exchange_failure_returns_502(db_session):
    """Shopify rejects the exchange (e.g. expired session token) → 502, no row created."""
    shop = "exchangefail.myshopify.com"
    boom = httpx.HTTPStatusError("bad", request=httpx.Request("POST", "https://x"), response=httpx.Response(400))

    with patch(f"{_DEPS}.get_shop_from_session_token", return_value=shop), \
         patch(f"{_DEPS}.exchange_session_token_for_offline_token", AsyncMock(side_effect=boom)):
        with pytest.raises(HTTPException) as exc:
            await get_current_shop(credentials=_creds(), shop=None, db=db_session)

    assert exc.value.status_code == 502
    row = (await db_session.execute(select(Shop).where(Shop.shop_domain == shop))).scalar_one_or_none()
    assert row is None


@pytest.mark.asyncio
async def test_reinstall_reactivates_uninstalled_row(db_session):
    """Reinstall of an uninstalled-but-not-purged row: fresh token + reset to
    pending + billing cleared, but trial_used + preferences preserved (no 2nd trial)."""
    shop = "reinstalled.myshopify.com"
    existing = make_shop(domain=shop, plan_tier="pro", plan_status="uninstalled")
    existing.access_token_encrypted = ""          # cleared by app/uninstalled
    existing.trial_used = True                     # must survive reinstall
    existing.selected_model = "gpt-6-luna"
    existing.store_timezone = "America/New_York"
    db_session.add(existing)
    await db_session.commit()
    existing_id = existing.id

    with patch(f"{_DEPS}.get_shop_from_session_token", return_value=shop), \
         patch(f"{_DEPS}.exchange_session_token_for_offline_token",
               AsyncMock(return_value=_token_data("shpat_reinstall_token"))) as exch, \
         patch(f"{_DEPS}._fetch_and_store_shop_meta", AsyncMock()):
        result = await get_current_shop(credentials=_creds(), shop=None, db=db_session)

    exch.assert_awaited_once()
    assert result.id == existing_id                # same row reused
    assert result.plan_status == "pending"         # back to plan picker
    assert result.plan_tier == "none"
    assert decrypt_token(result.access_token_encrypted) == "shpat_reinstall_token"
    assert result.access_token_expires_at is not None   # fresh expiring token
    assert result.uninstalled_at is None
    assert result.data_purge_at is None
    # Preserved — no second free trial, merchant prefs kept
    assert result.trial_used is True
    assert result.selected_model == "gpt-6-luna"
    assert result.store_timezone == "America/New_York"


@pytest.mark.asyncio
async def test_active_shop_is_never_reactivated(db_session):
    """An active shop with a live token is returned untouched — no token exchange,
    no reset (guards against wiping a paying merchant's billing state)."""
    existing = make_shop(domain="paying.myshopify.com", plan_tier="pro", plan_status="active")
    existing.access_token_encrypted = encrypt_token("live_token")
    db_session.add(existing)
    await db_session.commit()

    with patch(f"{_DEPS}.get_shop_from_session_token", return_value="paying.myshopify.com"), \
         patch(f"{_DEPS}.exchange_session_token_for_offline_token", AsyncMock()) as exch:
        result = await get_current_shop(credentials=_creds(), shop=None, db=db_session)

    exch.assert_not_awaited()
    assert result.plan_status == "active"
    assert decrypt_token(result.access_token_encrypted) == "live_token"


@pytest.mark.asyncio
async def test_concurrent_provision_is_idempotent(db_session):
    """The UNIQUE(shop_domain) fallback: if a row already exists when the INSERT
    fires (concurrent first-load), the existing row is returned, not a duplicate."""
    shop = "raced.myshopify.com"
    existing = make_shop(domain=shop, plan_tier="none", plan_status="pending")
    existing.access_token_encrypted = encrypt_token("first_writer_token")
    db_session.add(existing)
    await db_session.commit()
    existing_id = existing.id

    with patch(f"{_DEPS}.exchange_session_token_for_offline_token",
               AsyncMock(return_value=_token_data("second_writer_token"))), \
         patch(f"{_DEPS}._fetch_and_store_shop_meta", AsyncMock()):
        result = await _provision_shop_via_token_exchange(shop, "fake.session.jwt", db_session)

    assert result.id == existing_id  # same row, no duplicate
    rows = (await db_session.execute(select(Shop).where(Shop.shop_domain == shop))).scalars().all()
    assert len(rows) == 1
