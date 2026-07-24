import asyncio
import logging
import os
from typing import Optional

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

logger = logging.getLogger(__name__)

# Redis configuration
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "0"))


def get_redis_settings(fast_fail: bool = False) -> RedisSettings:
    """Get Redis settings for ARQ.

    ``fast_fail=True`` collapses arq's default reconnect budget (5 retries with a
    1s delay + 1s connect timeout each, ~5s worst case) to a single ~1s attempt.
    Used by enqueues that run inside a request's commit path (the notification
    transactional outbox, §3.1): a Redis outage there must fail fast rather than
    stall the committing thread for ~5s. Delivery is not lost -- the 5-min relay
    sweeper re-enqueues any event still marked ``notified_at IS NULL``.
    """
    if fast_fail:
        return RedisSettings(
            host=REDIS_HOST,
            port=REDIS_PORT,
            database=REDIS_DB,
            conn_retries=0,
            conn_retry_delay=0.2,
            conn_timeout=1,
        )
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


async def enqueue_job(job_function: str, *args, queue: str = "default", _job_id: Optional[str] = None, **kwargs):
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

    job = await pool.enqueue_job(job_function, *args, _job_id=_job_id, **kwargs)

    return job


def enqueue_job_best_effort(job_function: str, *args, fast_fail: bool = False, **kwargs) -> bool:
    """Enqueue a background job from a SYNC request handler, never raising.

    The completion endpoints are synchronous (``def``), so they cannot ``await``
    ``enqueue_job`` directly. This opens a short-lived event loop, enqueues the
    job, and returns. COMPLIANCE/correctness (Batch 5): outbound completion
    signals must NEVER fail the completion -- a Redis outage or enqueue error is
    swallowed (logged) so the already-committed completion still returns 200.

    ``fast_fail=True`` uses a single ~1s connect attempt instead of arq's default
    ~5s retry storm -- used by the notification outbox's after-commit enqueue,
    which runs in the request's commit path and must not block it during a Redis
    outage (the relay sweeper is the delivery backstop).

    Returns ``True`` when the job was enqueued, ``False`` when it was swallowed.
    Must only be called from a thread WITHOUT a running event loop (FastAPI runs
    sync ``def`` endpoints in a threadpool worker, which satisfies this).
    """

    async def _runner() -> None:
        # Use a fresh pool bound to THIS loop rather than the module-level
        # singleton (which may be bound to the app's main loop) so a short-lived
        # loop created here doesn't reuse a connection from another loop.
        pool = await create_pool(get_redis_settings(fast_fail=fast_fail))
        try:
            await pool.enqueue_job(job_function, *args, **kwargs)
        finally:
            pool.close()
            await pool.wait_closed()

    try:
        asyncio.run(_runner())
        return True
    except Exception:
        logger.exception("Failed to enqueue background job %s; continuing without it", job_function)
        return False


async def enqueue_job_fire_and_forget_fastfail(job_function: str, *args, **kwargs) -> None:
    """Fire-and-forget enqueue on the RUNNING loop with a fast-fail Redis pool.

    The async counterpart of ``enqueue_job_best_effort(fast_fail=True)`` for the
    notification outbox's after-commit path when a request handler is async: a
    running loop rules out ``asyncio.run``, so this is scheduled via
    ``loop.create_task``. It builds a short-lived fast-fail pool bound to the
    running loop and swallows every error (the relay sweeper re-enqueues), so a
    Redis outage can never surface as an unhandled task exception or fail the
    just-committed request.
    """
    try:
        pool = await create_pool(get_redis_settings(fast_fail=True))
        try:
            await pool.enqueue_job(job_function, *args, **kwargs)
        finally:
            pool.close()
            await pool.wait_closed()
    except Exception:
        logger.warning("Fast-fail enqueue of %s failed; the relay sweeper will retry", job_function, exc_info=True)
