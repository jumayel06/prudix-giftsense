"""Shopify App Proxy signature check (from Prudix Commerce, hardened).

The storefront widget calls /apps/giftsense/* on the shop's own domain;
Shopify forwards to /api/storefront/* with signed query params. `signature`
is the hex HMAC-SHA256 (app secret) of every other param, sorted by key and
joined as `k1=v1k2=v2…` with NO separator; a repeated key's values are joined
with commas. (The OAuth callback HMAC is different: `&`-joined.)

Added over Commerce: `timestamp` must be within PROXY_MAX_AGE_SECS, so a
captured signed URL can't be replayed later, and repeated params are handled.
Reference: https://shopify.dev/docs/apps/build/online-store/display-dynamic-data#calculate-a-digital-signature
"""
import hashlib
import hmac
import time
from collections.abc import Iterable, Mapping

import structlog

from core.config import settings

logger = structlog.get_logger()

PROXY_MAX_AGE_SECS = 300


def verify_proxy_signature(params, now: float | None = None) -> bool:
    """`params`: a mapping, Starlette QueryParams, or (key, value) pairs.
    Returns False (never raises) on a missing/bad signature or stale timestamp."""
    if hasattr(params, "multi_items"):
        pairs: Iterable = params.multi_items()
    elif isinstance(params, Mapping):
        pairs = params.items()
    else:
        pairs = params
    grouped: dict[str, list[str]] = {}
    signature = ""
    for key, value in pairs:
        if key == "signature":
            signature = value
        else:
            grouped.setdefault(key, []).append(value)

    secret = settings.shopify_api_secret
    if not signature or not secret:
        return False
    message = "".join(f"{k}={','.join(v)}" for k, v in sorted(grouped.items()))
    expected = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return False

    try:
        ts = int(grouped["timestamp"][0])
    except (KeyError, ValueError, IndexError):
        return False
    if abs((now or time.time()) - ts) > PROXY_MAX_AGE_SECS:
        logger.warning("app_proxy_stale_timestamp", shop=grouped.get("shop", ["?"])[0], age=int((now or time.time()) - ts))
        return False
    return True
