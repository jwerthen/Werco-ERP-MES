from arq import create_pool
from arq.connections import RedisSettings, ArqRedis
from typing import Optional
import os


# Redis configuration
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "0"))


def get_redis_settings() -> RedisSettings:
    """Get Redis settings for ARQ"""
    return RedisSettings(
        host=REDIS_HOST,
        port=REDIS_PORT,
        database=REDIS_DB,
    )


# Global pool singleton
_redis_pool: Optional[ArqRedis] = None


async def get_redis_pool() -> ArqRedis:
    """Get or create Redis connection pool"""
    global _redis_pool
    if _redis_pool is None:
        _redis_pool = await create_pool(get_redis_settings())
    return _redis_pool


async def enqueue_job(
    job_function: str,
    *args,
    queue: str = "default",
    _job_id: Optional[str] = None,
    **kwargs
):
    """
    Enqueue a background job

    Args:
        job_function: Name of the job function to execute
        *args: Positional arguments for the job
        queue: Queue name (for prioritization)
        _job_id: Optional custom job ID
        **kwargs: Keyword arguments for the job

    Returns:
        Job instance
    """
    pool = await get_redis_pool()

    job = await pool.enqueue_job(
        job_function,
        *args,
        _job_id=_job_id,
        **kwargs
    )

    return job


async def close_redis_pool():
    """Close Redis connection pool"""
    global _redis_pool
    if _redis_pool is not None:
        await _redis_pool.close()
        _redis_pool = None
