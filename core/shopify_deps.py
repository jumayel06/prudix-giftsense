"""FastAPI dependency for authenticated shop resolution.

Priority order for every embedded-app API request:
  1. Authorization: Bearer <session_token>  — App Bridge (production + staging)
  2. ?shop= query param                      — local dev fallback only

Route handlers replace `shop: str = Query(...)` with:
    shop_record: Shop = Depends(get_current_shop)

They then use shop_record.shop_domain and call get_valid_access_token(shop_record, db)
exactly as before.
"""

import asyncio
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

import structlog
from fastapi import Depends, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.db.models import Shop
from core.db import session as db_session_module
from core.db.session import get_db
from core.shopify_auth import (
    encrypt_token,
    exchange_session_token_for_offline_token,
    get_shop_from_session_token,
)
from core.shopify_graphql import shopify_graphql_post

logger = structlog.get_logger()

_bearer = HTTPBearer(auto_error=False)

# Environments where the unauthenticated ?shop= dev fallback is honoured.
# Allowlist, so it fails closed: production, a future staging, or a typo all
# require a session token.
_SHOP_PARAM_FALLBACK_ENVS = ("development", "test")


_SHOP_META_QUERY = """
query ShopMeta {
  shop {
    ianaTimezone
    email
  }
}
"""


async def _fetch_and_store_shop_meta(shop_record: Shop, access_token: str, db: AsyncSession) -> None:
    """Best-effort: cache store timezone + owner email (the digest workers target
    shop_owner_email). Mirrors the fetch in the legacy /auth/callback path."""
    try:
        resp = await shopify_graphql_post(shop_record.shop_domain, access_token, _SHOP_META_QUERY)
        if resp.status_code == 200:
            shop_json = (resp.json().get("data") or {}).get("shop") or {}
            shop_record.store_timezone = shop_json.get("ianaTimezone") or "UTC"
            shop_record.shop_owner_email = (shop_json.get("email") or "").strip() or None
            await db.commit()
    except Exception as e:
        logger.warning("shop_meta_fetch_failed", shop=shop_record.shop_domain, error=str(e))


# Set False in tests (conftest) so the meta fetch runs inline against the test
# DB session; test_first_load_speed exercises the background path explicitly.
META_IN_BACKGROUND = True
_meta_tasks: set[asyncio.Task] = set()  # strong refs so tasks aren't GC'd mid-flight


async def _fetch_shop_meta_after_response(shop_id: uuid.UUID, access_token: str) -> None:
    """Background version of `_fetch_and_store_shop_meta`, so the first response
    (and the plan picker) doesn't wait on a second Shopify call. Nothing on the
    first load needs timezone / owner email (digests and the billing-approval
    email read them much later). Own DB session — the request's session may
    already be closed. Ported from Prudix Commerce 084b679."""
    try:
        async with db_session_module.AsyncSessionLocal() as db:
            shop = (await db.execute(select(Shop).where(Shop.id == shop_id))).scalar_one_or_none()
            if shop is not None:
                await _fetch_and_store_shop_meta(shop, access_token, db)
    except Exception as e:  # noqa: BLE001 — best-effort, never surfaces
        logger.warning("shop_meta_background_failed", shop_id=str(shop_id), error=str(e))


async def _store_shop_meta(shop_record: Shop, access_token: str, db: AsyncSession) -> None:
    """Schedule the meta fetch as an independent task. Not a FastAPI
    BackgroundTask on purpose: those are dropped when the route raises, and this
    must run however the first request ends (the row is already committed)."""
    if not META_IN_BACKGROUND:
        await _fetch_and_store_shop_meta(shop_record, access_token, db)
        return
    task = asyncio.get_running_loop().create_task(
        _fetch_shop_meta_after_response(shop_record.id, access_token))
    _meta_tasks.add(task)
    task.add_done_callback(_meta_tasks.discard)


async def _provision_shop_via_token_exchange(shop_domain: str, session_token: str, db: AsyncSession) -> Shop:
    """Provision a freshly installed shop under Shopify managed install.

    Shopify handled the OAuth grant itself and loaded the embedded app, so
    /auth/callback never ran. We exchange the session token for an offline
    access token, create the shops row with plan_status='pending' (so the
    frontend routes to the plan picker), and cache store meta.
    """
    started = time.monotonic()
    token_data = await exchange_session_token_for_offline_token(shop_domain, session_token)
    exchange_ms = int((time.monotonic() - started) * 1000)

    shop_record = Shop(
        id=uuid.uuid4(),
        shop_domain=shop_domain,
        access_token_encrypted=encrypt_token(token_data["access_token"]),
        refresh_token_encrypted=encrypt_token(token_data["refresh_token"]) if token_data["refresh_token"] else None,
        access_token_expires_at=token_data["access_token_expires_at"],
        refresh_token_expires_at=token_data["refresh_token_expires_at"],
        plan_tier="none",
        plan_status="pending",
        installed_at=datetime.now(timezone.utc),
    )
    db.add(shop_record)
    try:
        await db.commit()
    except IntegrityError:
        # A concurrent first-load request already provisioned this shop
        # (shop_domain is UNIQUE). Roll back and use the existing row.
        await db.rollback()
        result = await db.execute(select(Shop).where(Shop.shop_domain == shop_domain))
        return result.scalar_one()

    await db.refresh(shop_record)
    await _store_shop_meta(shop_record, token_data["access_token"], db)
    logger.info("shop_provisioned_via_token_exchange", shop=shop_domain,
                exchange_ms=exchange_ms, total_ms=int((time.monotonic() - started) * 1000))
    return shop_record


async def _reactivate_shop_on_reinstall(shop_record: Shop, session_token: str, db: AsyncSession) -> Shop:
    """Re-provision an existing row on reinstall under managed install.

    `app/uninstalled` cleared the token + billing and set plan_status='uninstalled'
    but kept the row (trial_used preserved) until the purge cron deletes it. On
    reinstall Shopify re-grants and loads the app, so we mint a fresh offline
    token and reset billing state — mirroring the legacy /auth/callback reinstall
    branch. Crucially we DO NOT touch `trial_used` (no second free trial) or the
    merchant's preferences / integration tokens.
    """
    started = time.monotonic()
    token_data = await exchange_session_token_for_offline_token(shop_record.shop_domain, session_token)
    exchange_ms = int((time.monotonic() - started) * 1000)

    shop_record.access_token_encrypted = encrypt_token(token_data["access_token"])
    shop_record.refresh_token_encrypted = encrypt_token(token_data["refresh_token"]) if token_data["refresh_token"] else None
    shop_record.access_token_expires_at = token_data["access_token_expires_at"]
    shop_record.refresh_token_expires_at = token_data["refresh_token_expires_at"]
    shop_record.plan_status = "pending"
    shop_record.plan_tier = "none"
    shop_record.scheduled_plan_tier = None
    shop_record.scheduled_change_at = None
    shop_record.shopify_charge_id = None
    shop_record.billing_cycle_start = None
    shop_record.trial_started_at = None
    shop_record.trial_ends_at = None
    shop_record.grace_period_ends_at = None
    shop_record.data_purge_at = None
    shop_record.uninstalled_at = None
    shop_record.installed_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(shop_record)
    await _store_shop_meta(shop_record, token_data["access_token"], db)
    logger.info("shop_reactivated_on_reinstall", shop=shop_record.shop_domain,
                exchange_ms=exchange_ms, total_ms=int((time.monotonic() - started) * 1000))
    return shop_record


async def get_current_shop(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    shop: Optional[str] = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> Shop:
    """Resolve the authenticated Shop from either an App Bridge session token or ?shop= param."""

    shop_domain: Optional[str] = None
    session_token: Optional[str] = None

    # --- 1. Session token (production path) ---
    if credentials and credentials.credentials:
        try:
            shop_domain = get_shop_from_session_token(credentials.credentials)
            session_token = credentials.credentials
        except Exception as e:
            # An EXPIRED token is the benign 60s-lifetime edge, not a bug: the
            # frontend (shopifyFetch) transparently retries once with a fresh
            # token and succeeds, so the merchant never sees it. Log it at info
            # so it stays out of Sentry — otherwise burst actions (e.g. approving
            # KB pairs rapidly) spam warnings. Genuine problems keep warning level
            # for triage: InvalidAudienceError → wrong client_id,
            # InvalidSignatureError → wrong API secret, etc.
            if type(e).__name__ == "ExpiredSignatureError":
                logger.info("session_token_expired_retryable", error_type=type(e).__name__)
            else:
                logger.warning(
                    "session_token_invalid",
                    error_type=type(e).__name__,
                    error=str(e),
                )
            raise HTTPException(status_code=401, detail="Invalid session token")

    # --- 2. ?shop= fallback (local dev ONLY) ---
    # SECURITY: this resolves a shop from an unauthenticated query param, so it
    # is honoured only in development/test — anyone could otherwise act as any
    # installed store. Real embedded requests always carry a session token.
    # See tests/integration/test_shop_param_fallback.py.
    if not shop_domain:
        if shop and settings.app_env in _SHOP_PARAM_FALLBACK_ENVS:
            shop_domain = shop
        else:
            raise HTTPException(
                status_code=401,
                detail="Missing authentication — provide Authorization: Bearer <session_token>",
            )

    # Validate domain format
    if not shop_domain.endswith(".myshopify.com"):
        raise HTTPException(status_code=400, detail="Invalid shop domain")

    result = await db.execute(select(Shop).where(Shop.shop_domain == shop_domain))
    shop_record = result.scalar_one_or_none()

    # Under managed install Shopify performs the OAuth grant + loads the embedded
    # app itself, so /auth/callback never runs. We provision (new install) or
    # reactivate (reinstall of an uninstalled-but-not-yet-purged row) here on the
    # first authenticated call, via session-token exchange. An active shop with a
    # live token is left untouched.
    needs_provision = shop_record is None
    needs_reactivation = shop_record is not None and (
        shop_record.plan_status in ("uninstalled", "purged")
        or not shop_record.access_token_encrypted
    )

    if needs_provision or needs_reactivation:
        # The ?shop= dev fallback has no session token to exchange. Return an
        # existing row as-is; only a truly-missing shop 404s.
        if not session_token:
            if shop_record is not None:
                return shop_record
            raise HTTPException(status_code=404, detail="Shop not found — complete OAuth first")
        try:
            if needs_provision:
                shop_record = await _provision_shop_via_token_exchange(shop_domain, session_token, db)
            else:
                shop_record = await _reactivate_shop_on_reinstall(shop_record, session_token, db)
        except Exception as e:
            logger.error(
                "shop_provision_failed",
                shop=shop_domain,
                error_type=type(e).__name__,
                error=str(e),
            )
            raise HTTPException(status_code=502, detail="Could not complete installation — please retry")

    return shop_record
