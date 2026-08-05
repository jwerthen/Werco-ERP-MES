"""ARQ job-queue wiring: the ONE place the Redis target is resolved.

ROOT CAUSE (fixed 2026-08). This module used to read only ``REDIS_HOST`` / ``REDIS_PORT``
/ ``REDIS_DB`` and never looked at ``REDIS_URL`` -- while every other Redis consumer in the
backend (the response cache and the slowapi limiter storage in ``app/main.py``, the login
throttle in ``app/core/login_throttle.py``, and the ``/health/ready`` Redis probe) reads
``REDIS_URL``, and both ``docs/ENVIRONMENT_VARIABLES.md`` and ``backend/.env.example`` told
operators that ``REDIS_URL`` "takes precedence over individual settings".

The consequence on any deployment provisioned from those docs (Railway included, whose
managed Redis is handed to you as a single ``redis://default:PASS@host:6379`` URL) was that
the queue resolved to ``localhost:6379``: every enqueue failed with ConnectionRefused and
NOTHING was ever queued -- while ``/health/ready`` cheerfully reported ``redis: healthy``
because it pings ``REDIS_URL``. The old settings object also had no ``password`` field at
all, so even a correct ``REDIS_HOST`` could not authenticate against a managed Redis.

Resolution order is now, in both the enqueueing process and the worker process:

1. ``REDIS_URL`` when set -- the single source of truth. It carries user, password, db and
   TLS (``rediss://``), which the host/port/db trio structurally cannot.
2. otherwise ``REDIS_HOST`` / ``REDIS_PORT`` / ``REDIS_DB`` -- kept so an existing correct
   deployment (docker-compose sets only the trio on the worker container) is unchanged.
3. ``REDIS_PASSWORD``, when set, fills in a password that neither source supplied. This is
   what makes the docker-compose worker -- which is handed ``REDIS_PASSWORD`` but no URL --
   able to authenticate against ``redis-server --requirepass``.

Both the API and the worker call ``get_redis_settings()``, so the two sides cannot drift in
WHICH REDIS THEY REACH. They may differ in how patiently they wait for it -- see
``RedisProfile`` below -- and that divergence is itself confined to ``TRANSPORT_FIELDS`` and
machine-checked. ``tests/test_worker_redis_parity.py`` is the regression guard for both.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Tuple

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialBackoff

from app.core.config import settings

logger = logging.getLogger(__name__)

# Hosts that mean "nothing was configured". Inside a container these are the failure mode
# this module exists to prevent, not a legitimate target.
_LOOPBACK_HOSTS = {"", "localhost", "127.0.0.1", "::1"}

# Environments where an unconfigured Redis is a deploy defect rather than a dev convenience.
_DEPLOYED_ENVIRONMENTS = {"production", "staging"}

SOURCE_URL = "REDIS_URL"
SOURCE_PARTS = "REDIS_HOST/REDIS_PORT/REDIS_DB"
SOURCE_DEFAULTS = "defaults(localhost)"

# ---------------------------------------------------------------------------
# Target vs transport: which Redis, versus how patiently we wait for it.
#
# The root-cause defect above was a TARGET divergence -- the enqueue side reached
# localhost while the worker (and /health/ready) reached the real Redis. Any disagreement
# in these fields means the system is silently broken, so parity over them is absolute.
#
# TRANSPORT fields structurally CANNOT change which Redis is reached; they only decide how
# long a process waits before giving up. A worker that dies rather than waiting is a
# different (and worse) failure than a request handler that waits rather than failing fast,
# so these legitimately differ per profile.
#
# Together these must cover every field arq's RedisSettings has -- a field in neither bucket
# would be silently unguarded. test_the_field_partition_is_exhaustive holds that, as a TEST
# rather than an import-time assert: an arq upgrade that adds a field should fail CI, not
# take the API down at boot.
# ---------------------------------------------------------------------------
TARGET_FIELDS = (
    "host",
    "port",
    "unix_socket_path",
    "database",
    "username",
    "password",
    "ssl",
    "ssl_keyfile",
    "ssl_certfile",
    "ssl_cert_reqs",
    "ssl_ca_certs",
    "ssl_ca_data",
    "ssl_check_hostname",
    "sentinel",
    "sentinel_master",
)
TRANSPORT_FIELDS = (
    "conn_timeout",
    "conn_retries",
    "conn_retry_delay",
    "max_connections",
    "retry_on_timeout",
    "retry_on_error",
    "retry",
)


class RedisProfile(str, Enum):
    """How patiently a given process waits for Redis. Never which Redis."""

    #: Today's default, unchanged: arq's conn_timeout=1, conn_retries=5, conn_retry_delay=1.
    REQUEST = "request"
    #: Enqueues inside a request's commit path -- fail fast, the relay sweeper backstops.
    COMMIT_PATH = "commit_path"
    #: The arq worker process. See WORKER below for why it needs its own profile.
    WORKER = "worker"


# WHY THE WORKER NEEDS ITS OWN PROFILE (2026-08-05 incident).
#
# The worker's first-ever production boot crashed 22 seconds in:
#
#     redis.exceptions.TimeoutError: Timeout connecting to server
#       redis/asyncio/connection.py:296  connect()
#       redis/asyncio/client.py:1567     pipeline execute -> NEW pooled connection
#       arq/worker.py:488                run_job's FIRST pipeline
#       arq/worker.py:404                t.result()  <- bare, no filter -> process exit
#
# That pipeline sits OUTSIDE run_job's broad ``except`` (which does not begin until
# arq/worker.py:518), and _poll_iteration re-raises unconditionally, so a Redis blip during
# arq's own job bookkeeping is an unconditional process kill by arq's design. The worker
# cannot rely on arq tolerating a blip; it has to not see one.
#
# ``retry_on_timeout=True`` IS LOAD-BEARING AND NOT OPTIONAL. Passing ``retry=`` alone is a
# measured no-op: redis-py raises its OWN ``redis.exceptions.TimeoutError`` at
# connection.py:296, OUTSIDE ``call_with_retry``, and that class is NOT a subclass of the
# builtin ``TimeoutError`` the retry policy knows about. Measured against a blackholed IP on
# a connection built exactly as arq builds it:
#
#     arq defaults (conn_timeout=1)                 0 retries    1.00s  -> dead
#     retry=Retry(ExponentialBackoff, 3) alone      3 retries    5.00s  -> NO-OP, never fires
#     retry_on_timeout=True alone                   1 retry     10.00s
#     retry=Retry(...) + retry_on_timeout=True      3 retries   27.01s  <- this profile
#
# This converts "dies at 1s" into "dies at 27s". It is not a guarantee: an outage longer
# than ~27s still kills the process, which is the correct posture -- Railway's
# restartPolicyMaxRetries stays at 3 so a genuinely broken Redis still surfaces as a loud
# Crashed deployment rather than being absorbed silently.
#
# conn_retries=1 (down from arq's 5) CAPS the widening rather than shortening anything:
# each outer attempt is now internally ~27s, so 1 retry gives ~55s of startup budget where
# leaving it at 5 would give ~167s.
#
# Module-level, not per-call: ``Retry`` defines __slots__ and no ``__eq__``, so a fresh
# instance per call would compare by identity.
_WORKER_CONNECT_RETRY = Retry(ExponentialBackoff(cap=5, base=0.5), 3)


def redis_target_divergence(a: RedisSettings, b: RedisSettings) -> List[str]:
    """Target fields on which two resolutions disagree. Empty list == the same Redis.

    This is the parity property, stated over the fields that actually carry it. Whole-object
    equality conflates it with transport tuning, which is allowed to differ per profile.
    """
    return [name for name in TARGET_FIELDS if getattr(a, name) != getattr(b, name)]


class RedisConfigurationError(RuntimeError):
    """The queue's Redis target is unusable for the environment this process runs in."""


@dataclass(frozen=True)
class RedisTarget:
    """Where the queue resolved its Redis to, in a form that is SAFE TO LOG.

    Deliberately carries ``has_password`` rather than the password: this is written to the
    worker's startup log and to ``/health/ready``, neither of which may ever contain a
    credential.
    """

    source: str
    host: str
    port: int
    db: int
    ssl: bool
    has_password: bool

    @property
    def is_configured(self) -> bool:
        return self.source != SOURCE_DEFAULTS

    def describe(self) -> str:
        scheme = "rediss" if self.ssl else "redis"
        auth = "password" if self.has_password else "none"
        return f"{scheme}://{self.host}:{self.port}/{self.db} [source={self.source}, auth={auth}]"


def _resolve() -> Tuple[RedisSettings, str]:
    """Resolve (RedisSettings, source-label) from settings. See the module docstring."""
    url = (settings.REDIS_URL or "").strip()
    if url:
        try:
            resolved = RedisSettings.from_dsn(url)
        except Exception as exc:  # invalid scheme, unparseable port, ...
            raise RedisConfigurationError(
                f"REDIS_URL is set but is not a usable Redis DSN ({exc}). Expected "
                "redis://[[user]:password@]host:port/db (or rediss:// for TLS)."
            ) from exc
        source = SOURCE_URL
    else:
        resolved = RedisSettings(
            host=settings.REDIS_HOST,
            port=settings.REDIS_PORT,
            database=settings.REDIS_DB,
        )
        source = SOURCE_PARTS if str(settings.REDIS_HOST).lower() not in _LOOPBACK_HOSTS else SOURCE_DEFAULTS

    # REDIS_PASSWORD only fills a gap; a password already carried by the DSN always wins.
    if not resolved.password and settings.REDIS_PASSWORD:
        resolved = dataclasses.replace(resolved, password=settings.REDIS_PASSWORD)
    return resolved, source


def get_redis_settings(*, profile: Optional[RedisProfile] = None, fast_fail: bool = False) -> RedisSettings:
    """Get Redis settings for ARQ. Used by BOTH the enqueue side and ``WorkerSettings``.

    All profiles resolve the SAME target; they differ only in ``TRANSPORT_FIELDS``.

    ``profile=RedisProfile.COMMIT_PATH`` (and its retained alias ``fast_fail=True``)
    collapses arq's default reconnect budget to a single ~1s attempt. Used by enqueues that
    run inside a request's commit path (the notification transactional outbox, §3.1): a
    Redis outage there must fail fast rather than stall the committing thread. Delivery is
    not lost -- the 5-min relay sweeper re-enqueues any event still ``notified_at IS NULL``.

    That default budget is **~11s**, not the "~5s" this docstring claimed until 2026-08-05:
    ``arq/connections.py:271-299`` retries ``conn_retries`` times *after* the first attempt,
    so it is 6 attempts x 1s + 5 sleeps x 1s. Decisions citing ~5s were made against a
    number 2.2x low.

    ``profile=RedisProfile.WORKER`` is the arq worker's transport -- see ``_WORKER_CONNECT_RETRY``
    above for the incident that required it and the measurements behind each value.

    ``profile`` is keyword-only on purpose: positionally, ``get_redis_settings(True)`` would
    silently mean ``profile=True`` and return the REQUEST profile.
    """
    if profile is RedisProfile.WORKER and fast_fail:
        raise ValueError(
            "fast_fail is meaningless for RedisProfile.WORKER: one wants a wide connect "
            "budget, the other the narrowest possible. Pick one."
        )

    resolved, _source = _resolve()

    if profile is RedisProfile.WORKER:
        return dataclasses.replace(
            resolved,
            conn_timeout=5,
            conn_retries=1,
            retry_on_timeout=True,
            retry=_WORKER_CONNECT_RETRY,
        )

    if fast_fail or profile is RedisProfile.COMMIT_PATH:
        resolved = dataclasses.replace(
            resolved,
            conn_retries=0,
            conn_retry_delay=0.2,  # type: ignore[arg-type]  # arq annotates int, accepts float
            conn_timeout=1,
        )
    return resolved


def resolve_redis_target() -> RedisTarget:
    """Describe the resolved Redis target without exposing the credential."""
    resolved, source = _resolve()
    host = resolved.host if isinstance(resolved.host, str) else str(resolved.host)
    return RedisTarget(
        source=source,
        host=host,
        port=resolved.port,
        db=resolved.database,
        ssl=bool(resolved.ssl),
        has_password=bool(resolved.password),
    )


def redis_config_warnings() -> List[str]:
    """Non-fatal misconfigurations worth shouting about at startup.

    Two of them:

    1. ``REDIS_URL`` set but pointing at LOOPBACK in a deployed environment. ``is_configured``
       is source-based, so such a URL reports ``configured`` -- it *is* an explicit choice, and
       a single-box self-host with Redis on localhost is legitimate, which is why this is a
       warning and not a refusal. But ``backend/.env.example`` ships
       ``REDIS_URL=redis://localhost:6379/0``, so copying it into Railway is the one way left
       to get "configured" and still have every enqueue fail. Say so out loud.
    2. ``REDIS_URL`` and the host/port/db trio both set, but pointing at DIFFERENT instances.
       Before this module read ``REDIS_URL`` that combination silently meant "cache here,
       queue there"; now the URL wins, so the trio is dead config that will mislead the next
       person reading the Railway variables.
    """
    warnings: List[str] = []
    url = (settings.REDIS_URL or "").strip()

    if url and settings.ENVIRONMENT in _DEPLOYED_ENVIRONMENTS:
        try:
            target = resolve_redis_target()
        except RedisConfigurationError:
            target = None  # _resolve() already raises on this; nothing to add here
        if target is not None and target.host.lower() in _LOOPBACK_HOSTS:
            warnings.append(
                f"REDIS_URL points at LOOPBACK ({target.host}:{target.port}) with "
                f"ENVIRONMENT={settings.ENVIRONMENT}. Inside a container nothing listens there, so "
                "every enqueue will fail with ConnectionRefused even though the queue reports "
                "'configured'. Point REDIS_URL at the managed Redis, not localhost."
            )

    if not url or str(settings.REDIS_HOST).lower() in _LOOPBACK_HOSTS:
        return warnings
    try:
        from_url = RedisSettings.from_dsn(url)
    except Exception:
        return warnings  # _resolve() already raises on this; nothing to add here
    if (from_url.host, from_url.port, from_url.database) != (
        settings.REDIS_HOST,
        settings.REDIS_PORT,
        settings.REDIS_DB,
    ):
        warnings.append(
            "REDIS_URL and REDIS_HOST/REDIS_PORT/REDIS_DB name DIFFERENT Redis instances "
            f"({from_url.host}:{from_url.port}/{from_url.database} vs "
            f"{settings.REDIS_HOST}:{settings.REDIS_PORT}/{settings.REDIS_DB}). REDIS_URL wins; "
            "the individual settings are ignored. Unset one of them."
        )
    return warnings


def assert_redis_configured(component: str) -> RedisTarget:
    """Fail LOUDLY when a deployed process has no real Redis target.

    A worker that falls back to ``localhost:6379`` inside its own container either dies on
    connect (noisy, but the deploy is marked healthy elsewhere) or -- if anything at all is
    listening -- silently drains an empty queue forever while the API enqueues somewhere
    else. For a process whose entire job is to consume a queue, "started successfully and
    consumed nothing" is the worst possible outcome, so refuse to start instead.

    Left as a warning outside production/staging so local dev and the test suite are
    unaffected.
    """
    target = resolve_redis_target()
    if not target.is_configured and settings.ENVIRONMENT in _DEPLOYED_ENVIRONMENTS:
        raise RedisConfigurationError(
            f"{component}: no Redis is configured -- resolved {target.describe()} with "
            f"ENVIRONMENT={settings.ENVIRONMENT}. Set REDIS_URL (preferred: it carries the "
            "password and TLS) to the SAME Redis the API enqueues to, or set "
            "REDIS_HOST/REDIS_PORT/REDIS_DB. Refusing to start rather than consume an "
            "empty queue in silence."
        )
    return target


def log_redis_target(component: str, log: Optional[logging.Logger] = None) -> RedisTarget:
    """Log the resolved target (never the password) plus any config warnings."""
    log = log or logger
    target = resolve_redis_target()
    log.info("%s Redis target: %s", component, target.describe())
    if not target.is_configured:
        log.warning(
            "%s has NO Redis configured; falling back to %s. Background jobs will not run.",
            component,
            target.describe(),
        )
    for warning in redis_config_warnings():
        log.warning("%s Redis config: %s", component, warning)
    return target


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
