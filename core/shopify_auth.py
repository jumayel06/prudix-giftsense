import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
import jwt
import structlog
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings

logger = structlog.get_logger()

# Must match [access_scopes] in BOTH shopify.app.dev.toml and shopify.app.prod.toml.
# Every change forces merchants to re-authorize (see docs/SHOPIFY_PLAYBOOK.md §10).
SCOPES = "read_products,write_products,read_orders,write_orders,write_order_edits,write_merchant_managed_fulfillment_orders,read_themes,write_publications"

# Buffer: refresh the token this many seconds before actual expiry
_TOKEN_REFRESH_BUFFER_SECS = 300


def encrypt_token(token: str) -> str:
    return Fernet(settings.token_encryption_key.encode()).encrypt(token.encode()).decode()


def decrypt_token(encrypted: str) -> str:
    return Fernet(settings.token_encryption_key.encode()).decrypt(encrypted.encode()).decode()


def build_oauth_url(shop: str, nonce: str) -> str:
    params = {
        "client_id": settings.shopify_api_key,
        "scope": SCOPES,
        "redirect_uri": f"https://{settings.get_app_host()}/auth/callback",
        "state": nonce,
    }
    return f"https://{shop}/admin/oauth/authorize?{urlencode(params)}"


def verify_hmac(params: dict) -> bool:
    hmac_value = params.pop("hmac", None)
    if not hmac_value:
        return False
    sorted_params = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    digest = hmac.new(
        settings.shopify_api_secret.encode(),
        sorted_params.encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(digest, hmac_value)


async def exchange_code_for_token(shop: str, code: str) -> dict:
    """Exchange OAuth code for access token. Returns dict with token data."""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"https://{shop}/admin/oauth/access_token",
            json={
                "client_id": settings.shopify_api_key,
                "client_secret": settings.shopify_api_secret,
                "code": code,
                "expiring": 1,
            },
        )
        response.raise_for_status()
        data = response.json()

    now = datetime.now(timezone.utc)
    expires_in = data.get("expires_in")
    refresh_expires_in = data.get("refresh_token_expires_in")

    logger.info(
        "token_exchange_response",
        shop=shop,
        has_expires_in=expires_in is not None,
        has_refresh_token="refresh_token" in data,
        expires_in=expires_in,
        scope=data.get("scope"),
    )

    return {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token"),
        "access_token_expires_at": now + timedelta(seconds=expires_in) if expires_in else None,
        "refresh_token_expires_at": now + timedelta(seconds=refresh_expires_in) if refresh_expires_in else None,
    }


# Shopify managed-install / token-exchange grant (RFC 8693). Used to turn an
# App Bridge session token into a long-lived OFFLINE access token on the first
# authenticated load of a freshly installed shop — Shopify performs the OAuth
# grant itself under managed install, so /auth/callback is never hit.
_TOKEN_EXCHANGE_GRANT = "urn:ietf:params:oauth:grant-type:token-exchange"
_ID_TOKEN_TYPE = "urn:ietf:params:oauth:token-type:id_token"
_OFFLINE_TOKEN_TYPE = "urn:shopify:params:oauth:token-type:offline-access-token"


async def exchange_session_token_for_offline_token(shop_domain: str, session_token: str) -> dict:
    """Exchange an App Bridge session token for an EXPIRING offline access token.

    `expiring: 1` is REQUIRED — Shopify no longer accepts non-expiring offline
    tokens on the Admin API (403 "Non-expiring access tokens are no longer
    accepted"). The response carries expires_in (~1h) + a refresh_token (~90d),
    so we return the same dict shape as exchange_code_for_token and the existing
    get_valid_access_token refresh path takes over from there.
    Raises httpx.HTTPStatusError on failure (e.g. expired session token).
    """
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(
            f"https://{shop_domain}/admin/oauth/access_token",
            json={
                "client_id": settings.shopify_api_key,
                "client_secret": settings.shopify_api_secret,
                "grant_type": _TOKEN_EXCHANGE_GRANT,
                "subject_token": session_token,
                "subject_token_type": _ID_TOKEN_TYPE,
                "requested_token_type": _OFFLINE_TOKEN_TYPE,
                "expiring": 1,
            },
        )
        response.raise_for_status()
        data = response.json()

    now = datetime.now(timezone.utc)
    expires_in = data.get("expires_in")
    refresh_expires_in = data.get("refresh_token_expires_in")
    logger.info(
        "token_exchange_offline",
        shop=shop_domain,
        scope=data.get("scope"),
        has_refresh_token="refresh_token" in data,
        expires_in=expires_in,
    )
    return {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token"),
        "access_token_expires_at": now + timedelta(seconds=expires_in) if expires_in else None,
        "refresh_token_expires_at": now + timedelta(seconds=refresh_expires_in) if refresh_expires_in else None,
    }


async def _do_token_refresh(shop_domain: str, refresh_token: str) -> dict:
    """Use refresh token to get a new access token from Shopify."""
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"https://{shop_domain}/admin/oauth/access_token",
            json={
                "client_id": settings.shopify_api_key,
                "client_secret": settings.shopify_api_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
        )
        response.raise_for_status()
        data = response.json()

    now = datetime.now(timezone.utc)
    expires_in = data.get("expires_in")
    refresh_expires_in = data.get("refresh_token_expires_in")

    logger.info("token_refreshed", shop=shop_domain, expires_in=expires_in)
    return {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", refresh_token),
        "access_token_expires_at": now + timedelta(seconds=expires_in) if expires_in else None,
        "refresh_token_expires_at": now + timedelta(seconds=refresh_expires_in) if refresh_expires_in else None,
    }


async def get_valid_access_token(shop_record, db: AsyncSession) -> str:
    """Return a valid Shopify access token, refreshing it if expired or about to expire."""
    now = datetime.now(timezone.utc)

    # No expiry stored → old non-expiring token or freshly migrated shop — use as-is
    if shop_record.access_token_expires_at is None:
        return decrypt_token(shop_record.access_token_encrypted)

    expires_at = shop_record.access_token_expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)

    # Token still valid with buffer
    if expires_at > now + timedelta(seconds=_TOKEN_REFRESH_BUFFER_SECS):
        return decrypt_token(shop_record.access_token_encrypted)

    # Token expired or expiring soon — refresh
    if not shop_record.refresh_token_encrypted:
        logger.warning("token_expired_no_refresh_token", shop=shop_record.shop_domain)
        return decrypt_token(shop_record.access_token_encrypted)

    refresh_token = decrypt_token(shop_record.refresh_token_encrypted)
    token_data = await _do_token_refresh(shop_record.shop_domain, refresh_token)

    shop_record.access_token_encrypted = encrypt_token(token_data["access_token"])
    shop_record.refresh_token_encrypted = encrypt_token(token_data["refresh_token"])
    shop_record.access_token_expires_at = token_data["access_token_expires_at"]
    shop_record.refresh_token_expires_at = token_data["refresh_token_expires_at"]
    await db.commit()

    return token_data["access_token"]


def verify_session_token(token: str) -> dict:
    """Verify an App Bridge session token (short-lived HS256 JWT signed with API secret).

    Returns the decoded payload. Raises jwt.exceptions.* on invalid/expired tokens.
    The 'dest' claim contains the shop origin, e.g. 'https://store.myshopify.com'.
    """
    # Session tokens live for only 60s. Without leeway, minor clock drift
    # between Shopify's issuing server and this backend causes intermittent
    # "Invalid session token" 401s at the edge of the exp window. 10s is the
    # value Shopify's own examples use.
    return jwt.decode(
        token,
        settings.shopify_api_secret,
        algorithms=["HS256"],
        audience=settings.shopify_api_key,
        leeway=10,
        options={"verify_iat": False},  # Shopify iat may skew slightly
    )


def get_shop_from_session_token(token: str) -> str:
    """Extract the myshopify domain from a verified session token."""
    payload = verify_session_token(token)
    dest: str = payload.get("dest", "")
    # dest = "https://store.myshopify.com" → "store.myshopify.com"
    return dest.replace("https://", "").replace("http://", "").rstrip("/")
