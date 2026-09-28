"""Enqueue ARQ jobs from web routes.

Best effort: if Redis is down the caller carries on (webhooks must still 200)
and the catalog crons pick the work up later.
"""
import structlog
from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from core.config import settings

logger = structlog.get_logger()

_pool: ArqRedis | None = None


async def enqueue(function: str, *args, _job_id: str | None = None, _defer_by: float | None = None) -> bool:
    global _pool
    try:
        if _pool is None:
            _pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
        await _pool.enqueue_job(function, *args, _job_id=_job_id, _defer_by=_defer_by)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("enqueue_failed", function=function, error=str(e))
        return False
