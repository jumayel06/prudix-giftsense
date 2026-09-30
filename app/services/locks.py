"""Short-lived per-key locks in Redis (DB /1), for work that must not run twice
at once, e.g. analyzing a shop's catalog (a burst of product webhooks used to
start several analyses of the same products: 22 products → 106 AI calls,
2026-09-30).

Fails open like app/services/rate_limit.py: if Redis is unreachable, acquire
returns a dummy token and the caller proceeds unlocked (the old behavior).
"""
import secrets

import structlog

from core.config import settings

logger = structlog.get_logger()

NO_REDIS = "no-redis"
_RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) end
return 0
"""
_client = None


def _redis():
    global _client
    if _client is None:
        import redis.asyncio as redis_asyncio
        _client = redis_asyncio.from_url(settings.redis_url, socket_timeout=0.5, socket_connect_timeout=0.5)
    return _client


async def acquire(key: str, ttl_secs: int) -> str | None:
    """A token when the lock is ours, None when someone else holds it."""
    token = secrets.token_hex(8)
    try:
        ok = await _redis().set(f"lock:{key}", token, nx=True, ex=ttl_secs)
        return token if ok else None
    except Exception as e:  # noqa: BLE001 — fail open
        logger.warning("lock_redis_unavailable", key=key, error=str(e))
        return NO_REDIS


async def extend(key: str, token: str, ttl_secs: int) -> None:
    if token == NO_REDIS:
        return
    try:
        await _redis().expire(f"lock:{key}", ttl_secs)
    except Exception:  # noqa: BLE001
        pass


async def release(key: str, token: str) -> None:
    if token == NO_REDIS:
        return
    try:
        await _redis().eval(_RELEASE, 1, f"lock:{key}", token)
    except Exception:  # noqa: BLE001 — expires on its own
        pass
