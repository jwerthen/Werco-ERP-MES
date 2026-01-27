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

    if not settings.SENTRY_DSN:
        warn("SENTRY_DSN not set (optional but recommended).")
    else:
        ok("SENTRY_DSN set")

    if not settings.REDIS_URL:
        warn("REDIS_URL not set (optional but recommended for caching/rate limiting).")
    else:
        ok("REDIS_URL set")

    if not os.getenv("RAILWAY_PROJECT_ID") and not os.getenv("RAILWAY_SERVICE_ID"):
        warn("Railway env vars not detected (OK locally).")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
