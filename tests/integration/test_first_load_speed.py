"""Faster first load at install, across every path that reaches it.

- The 2nd Shopify call (timezone + owner email) runs as an independent task, so
  the first response doesn't wait for it, and it still runs if that first
  request fails. Covers new installs AND reinstalls (uninstalled / purged rows).
- /api/stats for a shop that hasn't picked a plan (`pending`) skips the usage
  queries but keeps the response shape; every other status (trial, active,
  cancelled, declined) is unchanged.
"""
import asyncio
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from sqlalchemy import select

import core.shopify_deps as deps
from app.main import app
from core.db.models import Shop
from core.db.session import get_db
from tests.conftest import TEST_SHOP_DOMAIN, add_usage, make_shop

_DEPS = "core.shopify_deps"


def _token_data():
    now = datetime.now(timezone.utc)
    return {"access_token": "shpat_x", "refresh_token": "r",
            "access_token_expires_at": now + timedelta(hours=1),
            "refresh_token_expires_at": now + timedelta(days=90)}


def _client(db_session):
    async def override_db():
        yield db_session
    app.dependency_overrides[get_db] = override_db
    yield TestClient(app, raise_server_exceptions=True)
    app.dependency_overrides.clear()


def _session_factory(db_session):
    @asynccontextmanager
    async def factory():
        yield db_session
    return factory


@pytest.fixture
def background(monkeypatch, db_session):
    """Real background path, with the task's DB session pointed at the test DB."""
    monkeypatch.setattr(deps, "META_IN_BACKGROUND", True)
    monkeypatch.setattr(deps.db_session_module, "AsyncSessionLocal", _session_factory(db_session))


async def _drain():
    while deps._meta_tasks:
        await asyncio.gather(*list(deps._meta_tasks), return_exceptions=True)


async def _first_load(db_session, domain):
    with patch(f"{_DEPS}.get_shop_from_session_token", return_value=domain), \
         patch(f"{_DEPS}.exchange_session_token_for_offline_token", AsyncMock(return_value=_token_data())):
        return await deps.get_current_shop(
            credentials=HTTPAuthorizationCredentials(scheme="Bearer", credentials="jwt"),
            shop=None, db=db_session)


async def _fake_meta(shop, token, db):
    shop.store_timezone = "Asia/Dhaka"
    shop.shop_owner_email = "owner@example.com"
    await db.commit()


# ── Shop meta off the critical path ──────────────────────────────────────────

def test_first_response_does_not_wait_for_the_meta_call(db_session, monkeypatch):
    """A slow Shopify meta call no longer delays /api/stats on install."""
    monkeypatch.setattr(deps, "META_IN_BACKGROUND", True)

    async def slow_meta(*_a, **_k):
        await asyncio.sleep(5)

    with patch(f"{_DEPS}.get_shop_from_session_token", return_value="newstore.myshopify.com"), \
         patch(f"{_DEPS}.exchange_session_token_for_offline_token", AsyncMock(return_value=_token_data())), \
         patch(f"{_DEPS}._fetch_shop_meta_after_response", side_effect=slow_meta) as bg:
        for client in _client(db_session):
            started = time.monotonic()
            resp = client.get("/api/stats", headers={"Authorization": "Bearer jwt"})
            elapsed = time.monotonic() - started
    assert resp.status_code == 200 and resp.json()["plan_status"] == "pending"
    assert elapsed < 2
    bg.assert_called_once()


@pytest.mark.asyncio
async def test_new_install_stores_meta_in_the_background(db_session, background):
    with patch(f"{_DEPS}._fetch_and_store_shop_meta", side_effect=_fake_meta):
        shop = await _first_load(db_session, "fresh.myshopify.com")
        assert shop.plan_status == "pending"
        await _drain()
    row = (await db_session.execute(select(Shop).where(Shop.shop_domain == "fresh.myshopify.com"))).scalar_one()
    assert (row.store_timezone, row.shop_owner_email) == ("Asia/Dhaka", "owner@example.com")


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_status,token", [("uninstalled", ""), ("purged", ""), ("active", "")])
async def test_reinstall_stores_meta_in_the_background_and_keeps_trial_used(db_session, background, prior_status, token):
    """Uninstalled / purged rows, and a live row whose token was cleared, are
    reactivated → pending; trial_used is preserved; meta refills (purge clears
    the owner email)."""
    old = make_shop(domain="back.myshopify.com", plan_status=prior_status, plan_tier="pro")
    old.trial_used = True
    old.access_token_encrypted = token
    old.shop_owner_email = None
    db_session.add(old)
    await db_session.commit()

    with patch(f"{_DEPS}._fetch_and_store_shop_meta", side_effect=_fake_meta) as meta:
        shop = await _first_load(db_session, "back.myshopify.com")
        await _drain()
    meta.assert_called_once()
    assert shop.plan_status == "pending" and shop.plan_tier == "none" and shop.trial_used is True
    assert shop.shop_owner_email == "owner@example.com"


@pytest.mark.asyncio
async def test_meta_still_runs_when_the_first_request_fails_afterwards(db_session, background):
    """Independent task, not a FastAPI BackgroundTask: even if the route that
    triggered provisioning then errors, the meta fetch completes."""
    with patch(f"{_DEPS}._fetch_and_store_shop_meta", side_effect=_fake_meta) as meta:
        await _first_load(db_session, "unlucky.myshopify.com")
        try:
            raise RuntimeError("route failed after provisioning")
        except RuntimeError:
            pass
        await _drain()
    meta.assert_called_once()


@pytest.mark.asyncio
async def test_background_meta_failure_is_swallowed(db_session, background):
    shop = make_shop(domain="flaky.myshopify.com")
    db_session.add(shop)
    await db_session.commit()
    with patch(f"{_DEPS}._fetch_and_store_shop_meta", AsyncMock(side_effect=RuntimeError("shopify down"))):
        await deps._fetch_shop_meta_after_response(shop.id, "tok")  # must not raise


@pytest.mark.asyncio
async def test_existing_shop_never_triggers_exchange_or_meta(db_session, background):
    for status in ("active", "trial_active", "pending", "cancelled", "declined"):
        db_session.add(make_shop(domain=f"{status.replace('_', '-')}.myshopify.com",
                                 plan_status=status, plan_tier="growth"))
    await db_session.commit()
    with patch(f"{_DEPS}.exchange_session_token_for_offline_token", AsyncMock()) as exch, \
         patch(f"{_DEPS}._fetch_and_store_shop_meta", AsyncMock()) as meta:
        for status in ("active", "trial_active", "pending", "cancelled", "declined"):
            with patch(f"{_DEPS}.get_shop_from_session_token",
                       return_value=f"{status.replace('_', '-')}.myshopify.com"):
                await deps.get_current_shop(
                    credentials=HTTPAuthorizationCredentials(scheme="Bearer", credentials="jwt"),
                    shop=None, db=db_session)
        await _drain()
    exch.assert_not_awaited()
    meta.assert_not_awaited()


# ── /api/stats: lean only while pending ──────────────────────────────────────

_SHAPE = {"generations_used", "generation_limit", "usage_pct", "days_elapsed", "days_remaining",
          "days_in_cycle", "plan_status", "plan_tier", "plan_name", "trial_used", "show_review_prompt",
          "review_prompt_url", "access_until"}


async def _stats(db_session):
    for client in _client(db_session):
        return client.get(f"/api/stats?shop={TEST_SHOP_DOMAIN}").json()


@pytest.mark.asyncio
async def test_pending_reinstall_stats_are_zeroed_with_the_same_shape(db_session):
    shop = make_shop(plan_status="pending", plan_tier="none")
    shop.trial_used = True                      # reinstall: old usage rows exist
    db_session.add(shop)
    await db_session.commit()
    await add_usage(db_session, shop, 5)

    data = await _stats(db_session)
    assert _SHAPE <= data.keys()
    assert data["plan_status"] == "pending" and data["trial_used"] is True
    assert data["generations_used"] == 0 and data["usage_pct"] == 0
    assert data["show_review_prompt"] is False


@pytest.mark.asyncio
async def test_active_stats_still_count_usage(db_session):
    shop = make_shop(plan_status="active", plan_tier="growth",
                     billing_cycle_start=datetime.now(timezone.utc) - timedelta(days=2))
    db_session.add(shop)
    await db_session.commit()
    await add_usage(db_session, shop, 5)

    data = await _stats(db_session)
    assert _SHAPE <= data.keys()
    assert data["generations_used"] == 5 and data["usage_pct"] > 0


@pytest.mark.asyncio
async def test_trial_stats_still_count_usage_and_trial_fields(db_session):
    now = datetime.now(timezone.utc)
    shop = make_shop(plan_status="trial_active", plan_tier="pro", billing_cycle_start=now - timedelta(days=1))
    shop.trial_started_at = now - timedelta(days=1)
    shop.trial_ends_at = now + timedelta(days=6)
    db_session.add(shop)
    await db_session.commit()
    await add_usage(db_session, shop, 6)

    data = await _stats(db_session)
    assert data["generations_used"] == 6
    assert data["trial_generations_used"] == 6 and data["trial_days_remaining"] >= 6


@pytest.mark.asyncio
async def test_cancelled_and_declined_stats_are_unchanged(db_session):
    now = datetime.now(timezone.utc)
    shop = make_shop(plan_status="cancelled", plan_tier="growth", billing_cycle_start=now - timedelta(days=3))
    db_session.add(shop)
    await db_session.commit()
    await add_usage(db_session, shop, 2)

    data = await _stats(db_session)
    assert data["generations_used"] == 2 and data["access_until"] is not None

    shop.plan_status = "declined"
    await db_session.commit()
    assert (await _stats(db_session))["generations_used"] == 2
