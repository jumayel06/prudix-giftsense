import json
import secrets
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.db.models import Shop
from core.db.session import get_db
from core.shopify_auth import build_oauth_url, encrypt_token, exchange_code_for_token, get_valid_access_token, verify_hmac
from core.shopify_graphql import shopify_graphql_post
from app.routes.webhooks import _handle_uninstalled

logger = structlog.get_logger()
router = APIRouter()

# File-backed nonce store — survives uvicorn --reload between /auth and /auth/callback
_NONCE_FILE = Path("/tmp/.shopify_oauth_nonces.json")
_NONCE_TTL = 300  # seconds


def _load_nonces() -> dict:
    if not _NONCE_FILE.exists():
        return {}
    try:
        now = time.time()
        data = json.loads(_NONCE_FILE.read_text())
        return {k: v for k, v in data.items() if v.get("exp", 0) > now}
    except Exception:
        return {}


def _store_nonce(shop: str, nonce: str) -> None:
    nonces = _load_nonces()
    nonces[shop] = {"nonce": nonce, "exp": time.time() + _NONCE_TTL}
    _NONCE_FILE.write_text(json.dumps(nonces))


def _pop_nonce(shop: str) -> str | None:
    nonces = _load_nonces()
    entry = nonces.pop(shop, None)
    if entry:
        _NONCE_FILE.write_text(json.dumps(nonces))
        return entry["nonce"]
    return None


# Webhook subscriptions are declared in `shopify.app.{dev,prod}.toml`
# under `[[webhooks.subscriptions]]` and registered automatically by the
# Shopify CLI when the app version is pushed. We previously also called
# `webhookSubscriptionCreate` here at OAuth time as a belt-and-suspenders
# backup, but that created DUPLICATE subscriptions (toml + runtime) so every
# event fired twice — confirmed via the dev store on 2026-05-20 where a
# single `products/create` arrived on two different IPs with two different
# X-Shopify-Webhook-Id values. The toml is now the sole source of truth.
# If you need to re-register subscriptions for an existing shop, uninstall
# + reinstall the app — clean OAuth cycle re-syncs from the toml manifest.

@router.get("/auth")
async def auth_start(
    request: Request,
    shop: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    if not shop.endswith(".myshopify.com"):
        raise HTTPException(status_code=400, detail="Invalid shop domain")

    # Already installed and active — verify token is still valid before short-circuiting.
    # If the merchant uninstalled and the webhook was missed, the token will 401 here;
    # treat that as uninstalled so OAuth can proceed rather than bouncing them forever.
    result = await db.execute(select(Shop).where(Shop.shop_domain == shop))
    existing = result.scalar_one_or_none()
    if existing and existing.access_token_encrypted and existing.plan_status not in ("pending", "uninstalled", "declined", "none"):
        try:
            token = await get_valid_access_token(existing, db)
            resp = await shopify_graphql_post(shop, token, "query { shop { id } }", timeout=5)
            if resp.status_code == 401:
                logger.info("auth_token_revoked_running_oauth", shop=shop)
                await _handle_uninstalled(shop, db, authoritative=True)
            else:
                return RedirectResponse(f"https://{shop}/admin/apps/{settings.shopify_api_key}")
        except Exception:
            return RedirectResponse(f"https://{shop}/admin/apps/{settings.shopify_api_key}")

    nonce = secrets.token_urlsafe(32)
    _store_nonce(shop, nonce)
    oauth_url = build_oauth_url(shop, nonce)
    logger.info("oauth_redirect", shop=shop)
    return RedirectResponse(oauth_url)


@router.get("/auth/callback")
async def auth_callback(
    request: Request,
    shop: str = Query(...),
    code: str = Query(...),
    state: str = Query(...),
    db: AsyncSession = Depends(get_db),
):
    params = dict(request.query_params)
    logger.info("auth_callback_received", shop=shop)

    # Reject stale callbacks (replay attack protection — Shopify recommendation)
    timestamp = params.get("timestamp")
    if timestamp:
        try:
            if abs(time.time() - int(timestamp)) > 300:
                logger.error("timestamp_expired", shop=shop, timestamp=timestamp)
                raise HTTPException(status_code=403, detail="Request timestamp expired")
        except (ValueError, TypeError):
            raise HTTPException(status_code=403, detail="Invalid timestamp")

    stored_nonce = _pop_nonce(shop)
    if not stored_nonce or stored_nonce != state:
        logger.error("nonce_mismatch", shop=shop, stored=bool(stored_nonce))
        raise HTTPException(status_code=403, detail="Invalid state — possible CSRF")

    params_copy = dict(params)
    if not verify_hmac(params_copy):
        logger.error("hmac_failed", shop=shop)
        raise HTTPException(status_code=403, detail="HMAC verification failed")

    token_data = await exchange_code_for_token(shop, code)
    access_token = token_data["access_token"]

    result = await db.execute(select(Shop).where(Shop.shop_domain == shop))
    existing_shop = result.scalar_one_or_none()

    now = datetime.now(timezone.utc)
    if existing_shop:
        existing_shop.access_token_encrypted = encrypt_token(access_token)
        existing_shop.refresh_token_encrypted = encrypt_token(token_data["refresh_token"]) if token_data["refresh_token"] else None
        existing_shop.access_token_expires_at = token_data["access_token_expires_at"]
        existing_shop.refresh_token_expires_at = token_data["refresh_token_expires_at"]
        existing_shop.plan_status = "pending"
        existing_shop.plan_tier = "none"
        existing_shop.scheduled_plan_tier = None
        existing_shop.scheduled_change_at = None
        existing_shop.uninstalled_at = None
        existing_shop.installed_at = now
        # Clear billing state defensively — uninstall webhook should have done this,
        # but handle edge cases where it was missed or delayed
        existing_shop.shopify_charge_id = None
        existing_shop.billing_cycle_start = None
        existing_shop.trial_started_at = None
        existing_shop.trial_ends_at = None
        existing_shop.grace_period_ends_at = None
        existing_shop.data_purge_at = None
    else:
        existing_shop = Shop(
            id=uuid.uuid4(),
            shop_domain=shop,
            access_token_encrypted=encrypt_token(access_token),
            refresh_token_encrypted=encrypt_token(token_data["refresh_token"]) if token_data["refresh_token"] else None,
            access_token_expires_at=token_data["access_token_expires_at"],
            refresh_token_expires_at=token_data["refresh_token_expires_at"],
            plan_tier="none",
            plan_status="pending",
        )
        db.add(existing_shop)

    await db.commit()
    logger.info("shop_saved", shop=shop)

    # Fetch store timezone + owner email from Shopify and persist them.
    # One GraphQL call for both fields; `shop.email` is the merchant contact
    # address that the 4 weekly digest workers will target.
    try:
        resp = await shopify_graphql_post(
            shop, access_token, "query { shop { ianaTimezone email } }",
        )
        if resp.status_code == 200:
            shop_json = (resp.json().get("data") or {}).get("shop") or {}
            iana_tz = shop_json.get("ianaTimezone", "UTC")
            owner_email = (shop_json.get("email") or "").strip() or None
            existing_shop.store_timezone = iana_tz or "UTC"
            existing_shop.shop_owner_email = owner_email
            await db.commit()
            logger.info("shop_meta_saved", shop=shop, tz=iana_tz,
                        has_owner_email=bool(owner_email))
    except Exception as e:
        logger.warning("shop_meta_fetch_failed", shop=shop, error=str(e))

    # Redirect into the embedded app — merchant will choose their plan from there
    logger.info("install_complete", shop=shop)
    return RedirectResponse(f"https://{shop}/admin/apps/{settings.shopify_api_key}")
