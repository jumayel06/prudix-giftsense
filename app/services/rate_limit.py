"""Storefront abuse limits: fixed-window counters in Redis (DB /1).

Per widget session and per shopper-IP hash (docs/TECHNICAL_PLAN.md §8.3).
Both can be dodged by a determined bot (new session ids, spoofed
X-Forwarded-For), so they only stop casual abuse; spend is bounded by
metering's per-shop hourly cap and monthly limit (app/services/metering.py).

Fails open: if Redis is unreachable the request goes through (logged),
because the widget must not break and metering still bounds spend.
"""
import hashlib
import hmac
import time

import structlog

from core.config import settings

logger = structlog.get_logger()

SEARCHES_PER_SID_PER_HOUR = 10
SEARCHES_PER_IP_PER_HOUR = 30
HOUR = 3600


class _RedisStore:
    def __init__(self):
        self._client = None

    async def incr_window(self, key: str, ttl: int) -> int:
        if self._client is None:
            import redis.asyncio as redis_asyncio
            self._client = redis_asyncio.from_url(settings.redis_url, socket_timeout=0.5, socket_connect_timeout=0.5)
        async with self._client.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, ttl)
            count, _ = await pipe.execute()
        return int(count)


_store = _RedisStore()


async def hit(key: str, limit: int, window_secs: int) -> bool:
    """Count one request against `key`; True while within `limit` per window."""
    window = int(time.time() // window_secs)
    try:
        count = await _store.incr_window(f"rl:{key}:{window}", window_secs + 60)
    except Exception as e:  # noqa: BLE001 — fail open
        logger.warning("rate_limit_store_unavailable", error=str(e)[:200])
        return True
    return count <= limit


def shopper_ip(forwarded_for: str | None) -> str | None:
    """Shopify's App Proxy puts the shopper's IP first in X-Forwarded-For;
    our own proxies (Cloudflare tunnel, Railway) append after it."""
    if not forwarded_for:
        return None
    first = forwarded_for.split(",")[0].strip()
    return first or None


def ip_hash(ip: str) -> str:
    """Keyed hash: we never keep raw shopper IPs."""
    return hmac.new(settings.shopify_api_secret.encode(), ip.encode(), hashlib.sha256).hexdigest()[:16]
