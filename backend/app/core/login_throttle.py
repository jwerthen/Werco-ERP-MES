"""Per-IP failed-attempt throttle for the unauthenticated employee/badge login.

Compensating control for the employee-login rate-limit raise (3/min -> 10/min,
Kiosk Foundry redesign): ``POST /auth/employee-login`` mints a full unscoped
token from a bare employee/badge ID, and the slowapi per-path limit is the only
online-guessing control on it. 10/min alone would let a 4-digit badge space be
swept from one IP in under a day, so the endpoint ADDS this throttle: after
``EMPLOYEE_LOGIN_MAX_FAILURES`` FAILED attempts from the same client IP within
``EMPLOYEE_LOGIN_FAILURE_WINDOW_SECONDS``, further attempts from that IP are
rejected with 429 for ``EMPLOYEE_LOGIN_COOLDOWN_SECONDS``. Successful logins
never count toward the window, so shift-change badge cycling stays fast; the
blocked check runs BEFORE any user lookup, so a throttled IP does zero account
probing.

Storage mirrors the slowapi limiter configuration in ``app/main.py``
(``storage_uri=REDIS_URL or "memory://"``): Redis when ``settings.REDIS_URL``
is configured — the cross-worker deployment reality — else a process-local
in-memory counter (dev/test single-process).

FAIL-OPEN IS DELIBERATE: on a Redis outage the throttle logs a warning and
allows the attempt. A Redis blip must never brick a shift change, and the
slowapi 10/min per-path limit (which shares the same fail-open posture and is
backstopped by the app-wide default limit) still bounds request volume. A
sustained fail-open during an attack is itself a security event, hence the
SIEM-greppable ``employee_login_throttle_fail_open`` marker on the warning.
"""

import logging
import threading
import time
from typing import Dict, List, Optional

from fastapi import Request

from app.core.config import settings

logger = logging.getLogger(__name__)

# 8 failures / 15 min, then a 15-min cooldown. The window is generous enough
# that a fat-fingered crew at one station never trips it (a failure is an
# UNKNOWN id, not a slow scan), and tight enough that sweeping a 4-digit badge
# space from one IP takes ~2 weeks instead of ~17 hours at the 10/min cap.
EMPLOYEE_LOGIN_MAX_FAILURES = 8
EMPLOYEE_LOGIN_FAILURE_WINDOW_SECONDS = 15 * 60
EMPLOYEE_LOGIN_COOLDOWN_SECONDS = 15 * 60


def client_ip_from_request(request: Request) -> str:
    """Resolve the client IP exactly like the slowapi limiter does.

    ``slowapi.util.get_remote_address`` (the app's ``key_func`` in
    ``app/main.py``) returns the socket peer address with no proxy-header
    parsing — the platform terminates behind a trusted proxy whose forwarding
    the deployment normalizes. Deliberately no home-grown X-Forwarded-For
    handling: this throttle must key identically to every other rate limit.
    """
    try:
        from slowapi.util import get_remote_address

        return get_remote_address(request)
    except ImportError:  # pragma: no cover - slowapi is a hard requirement
        return request.client.host if request.client else "127.0.0.1"


class FailedLoginThrottle:
    """Counts FAILED attempts per client IP and blocks past a threshold.

    Semantics: the counter starts a fixed window at the first failure; hitting
    the threshold re-arms the key's TTL to the cooldown so the block runs its
    full length from the moment it engaged. Expiry (window or cooldown) resets
    the counter entirely. Blocked attempts are refused before the user lookup
    and do not extend the cooldown.
    """

    def __init__(
        self,
        *,
        key_prefix: str,
        max_failures: int,
        window_seconds: int,
        cooldown_seconds: int,
    ) -> None:
        self._key_prefix = key_prefix
        self._max_failures = max_failures
        self._window_seconds = window_seconds
        self._cooldown_seconds = cooldown_seconds
        self._redis = None
        # Memory mode: ip -> [count, expires_at_epoch]. Only used when
        # REDIS_URL is unset (matches the slowapi memory:// fallback).
        self._memory: Dict[str, List[float]] = {}
        self._lock = threading.Lock()

    # -- seams (monkeypatched in tests) ----------------------------------
    def _now(self) -> float:
        return time.time()

    def _redis_client(self):
        """Lazy Redis client from settings.REDIS_URL; None = memory mode."""
        if not settings.REDIS_URL:
            return None
        if self._redis is None:
            import redis

            self._redis = redis.from_url(settings.REDIS_URL, encoding="utf-8", decode_responses=True)
        return self._redis

    # -- internals -------------------------------------------------------
    def _key(self, client_ip: str) -> str:
        return f"{self._key_prefix}:{client_ip}"

    def _log_fail_open(self, action: str, client_ip: str, exc: Exception) -> None:
        # FAIL-OPEN IS DELIBERATE (see module docstring): the slowapi 10/min
        # per-path limit still holds, and a Redis blip must never brick a
        # shift change. Warn with a stable marker so a SIEM can alert on a
        # sustained fail-open.
        logger.warning(
            "employee_login_throttle_fail_open: %s failed for ip=%s (%s); allowing attempt",
            action,
            client_ip,
            exc,
        )

    def _prune_memory(self, now: float) -> None:
        expired = [ip for ip, (count, expires_at) in self._memory.items() if expires_at <= now]
        for ip in expired:
            del self._memory[ip]

    # -- API -------------------------------------------------------------
    def blocked_retry_after(self, client_ip: str) -> Optional[int]:
        """Seconds until this IP may retry, or None when not blocked.

        Fail-open: any storage error answers None (not blocked) with a
        logged warning.
        """
        try:
            client = self._redis_client()
        except Exception as exc:  # pragma: no cover - bad REDIS_URL
            self._log_fail_open("client-init", client_ip, exc)
            return None
        if client is not None:
            try:
                raw = client.get(self._key(client_ip))
                if raw is None or int(raw) < self._max_failures:
                    return None
                ttl = client.ttl(self._key(client_ip))
                return ttl if isinstance(ttl, int) and ttl > 0 else self._cooldown_seconds
            except Exception as exc:
                self._log_fail_open("check", client_ip, exc)
                return None
        now = self._now()
        with self._lock:
            self._prune_memory(now)
            entry = self._memory.get(client_ip)
            if entry is None or entry[0] < self._max_failures:
                return None
            return max(1, int(entry[1] - now))

    def register_failure(self, client_ip: str) -> None:
        """Record one FAILED attempt for this IP (never called on success)."""
        try:
            client = self._redis_client()
        except Exception as exc:  # pragma: no cover - bad REDIS_URL
            self._log_fail_open("client-init", client_ip, exc)
            return
        if client is not None:
            try:
                key = self._key(client_ip)
                count = client.incr(key)
                if count == 1:
                    client.expire(key, self._window_seconds)
                elif count == self._max_failures:
                    # The cooldown runs from the moment the block engages,
                    # not from the window's first failure.
                    client.expire(key, self._cooldown_seconds)
                return
            except Exception as exc:
                self._log_fail_open("count", client_ip, exc)
                return
        now = self._now()
        with self._lock:
            self._prune_memory(now)
            entry = self._memory.get(client_ip)
            if entry is None:
                self._memory[client_ip] = [1, now + self._window_seconds]
                return
            entry[0] += 1
            if entry[0] == self._max_failures:
                entry[1] = now + self._cooldown_seconds

    def reset(self) -> None:
        """Test hook: clear the in-memory store and drop the Redis client.

        Redis keys are left to their TTLs (tests run in memory mode; prod
        never calls this).
        """
        with self._lock:
            self._memory.clear()
        self._redis = None


employee_login_throttle = FailedLoginThrottle(
    key_prefix="auth:employee-login:failed",
    max_failures=EMPLOYEE_LOGIN_MAX_FAILURES,
    window_seconds=EMPLOYEE_LOGIN_FAILURE_WINDOW_SECONDS,
    cooldown_seconds=EMPLOYEE_LOGIN_COOLDOWN_SECONDS,
)
