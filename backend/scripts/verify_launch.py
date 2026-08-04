import os
import sys

from app.core.config import settings


def warn(msg: str) -> None:
    print(f"[WARN] {msg}")


def fail(msg: str) -> None:
    print(f"[FAIL] {msg}")


def ok(msg: str) -> None:
    print(f"[OK] {msg}")


def main() -> int:
    failures = 0

    if settings.ENVIRONMENT != "production":
        fail("ENVIRONMENT must be set to 'production'.")
        failures += 1
    else:
        ok("ENVIRONMENT=production")

    if settings.DEBUG:
        fail("DEBUG must be false in production.")
        failures += 1
    else:
        ok("DEBUG=false")

    if not settings.SECRET_KEY or len(settings.SECRET_KEY) < 32:
        fail("SECRET_KEY must be set and at least 32 characters.")
        failures += 1
    else:
        ok("SECRET_KEY looks set")

    if not settings.REFRESH_TOKEN_SECRET_KEY or len(settings.REFRESH_TOKEN_SECRET_KEY) < 32:
        fail("REFRESH_TOKEN_SECRET_KEY must be set and at least 32 characters.")
        failures += 1
    else:
        ok("REFRESH_TOKEN_SECRET_KEY looks set")

    if "localhost" in (settings.DATABASE_URL or "") or "user:pass" in (settings.DATABASE_URL or ""):
        fail("DATABASE_URL looks like a default/local value.")
        failures += 1
    else:
        ok("DATABASE_URL looks non-local")

    if settings.RATE_LIMIT_ENABLED:
        ok("Rate limiting enabled")
    else:
        warn("Rate limiting disabled")

    if settings.CORS_ORIGINS and "localhost" in settings.CORS_ORIGINS:
        warn("CORS_ORIGINS contains localhost; ensure production origins are included.")
    else:
        ok("CORS_ORIGINS looks production-ready")

    # Warn, not fail, on purpose: ALLOWED_HOSTS defaults to "*" and this script runs
    # against live prod after every deploy. Failing here would turn an unset env var
    # into a red deploy. This surfaces the answer in the deploy log instead.
    #
    # Test the SAME condition app/main.py uses (`"*" in settings.allowed_hosts_list`),
    # not the raw string. An empty ALLOWED_HOSTS falls back to ["*"] in that property,
    # so a raw-string check would report "restricted" while enforcement is off.
    allowed_hosts = settings.allowed_hosts_list
    if "*" in allowed_hosts:
        warn(
            "ALLOWED_HOSTS allows any Host ('*'): Host-header validation "
            "(TrustedHostMiddleware) is DISABLED. Set it to the API's real hostnames, "
            "e.g. ALLOWED_HOSTS='api.wercomfg.app,healthcheck.railway.app' — and include "
            "the health-check probe hosts or deploy health checks get a 400."
        )
    else:
        ok(f"ALLOWED_HOSTS restricted ({len(allowed_hosts)} host(s))")

    if not settings.SENTRY_DSN:
        warn("SENTRY_DSN not set (optional but recommended).")
    else:
        ok("SENTRY_DSN set")

    if not settings.REDIS_URL:
        warn("REDIS_URL not set (optional but recommended for caching/rate limiting).")
    else:
        ok("REDIS_URL set")

    # The job queue's Redis, which is NOT the same question as REDIS_URL being set. Until
    # 2026-08 the queue read only REDIS_HOST/PORT/DB, so a deployment with REDIS_URL set and
    # nothing else had a queue pointed at localhost: every enqueue failed, no background job
    # or cron ever ran, and /health/detailed still said "redis: healthy" because it pings
    # REDIS_URL. This check makes that state visible from CI instead of never.
    try:
        from app.core.queue import redis_config_warnings, resolve_redis_target

        queue_target = resolve_redis_target()
        if not queue_target.is_configured:
            warn(
                "Job queue has NO Redis configured (resolved "
                f"{queue_target.describe()}). Background jobs and crons will NOT run: every "
                "enqueue fails with ConnectionRefused. Set REDIS_URL."
            )
        else:
            ok(f"Job queue Redis resolved from {queue_target.source}")
        for queue_warning in redis_config_warnings():
            warn(queue_warning)
    except Exception as exc:  # a malformed REDIS_URL raises here
        warn(f"Job queue Redis could not be resolved: {exc}")

    if not os.getenv("RAILWAY_PROJECT_ID") and not os.getenv("RAILWAY_SERVICE_ID"):
        warn("Railway env vars not detected (OK locally).")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
