"""Per-IP failed-attempt throttles for the two unauthenticated login routes.

One class, TWO module-level instances with SEPARATE key prefixes and separate
budgets — ``employee_login_throttle`` for ``POST /auth/employee-login`` and
``password_login_throttle`` for ``POST /auth/login``. The split is the point of
this module's shape; see "Why two counters, never one" below before collapsing
them.

``POST /auth/employee-login`` — the kiosk badge route
-----------------------------------------------------
Compensating control for the employee-login rate-limit raise (3/min -> 10/min,
Kiosk Foundry redesign): the route mints a full unscoped token from a bare
employee/badge ID, and the slowapi per-path limit is the only online-guessing
control on it. 10/min alone would let a 4-digit badge space be swept from one IP
in under a day, so the endpoint ADDS this throttle: after
``EMPLOYEE_LOGIN_MAX_FAILURES`` FAILED attempts from the same client IP within
``EMPLOYEE_LOGIN_FAILURE_WINDOW_SECONDS``, further attempts from that IP are
rejected with 429 for ``EMPLOYEE_LOGIN_COOLDOWN_SECONDS``. Successful logins
never count toward the window, so shift-change badge cycling stays fast; the
blocked check runs BEFORE any user lookup, so a throttled IP does zero account
probing.

``POST /auth/login`` — the identifier + password route
------------------------------------------------------
Same mechanism, its own budget (``PASSWORD_LOGIN_*``), applied only to attempts
whose SUBMITTED identifier lies in an enumerable space: a badge, or a synthetic
``emp-…@users.werco.com`` / ``@werco.local`` address this system minted from one.
It is needed even though the route verifies a password, because this route drives
the 5-failure/30-minute ACCOUNT LOCKOUT — which also locks the kiosk route — so
sweeping identifiers here can take people off the floor. An ordinary address at a
real domain is never counted (see the call site in ``api/endpoints/auth.py``).

Why two counters, never one
---------------------------
They were one instance briefly, and that was an outage waiting for a Monday:

* **Starvation across routes.** ``/auth/login`` counts WRONG-PASSWORD failures —
  an outcome ``/auth/employee-login`` cannot even produce, being passwordless.
  The kiosk budget is sized on the premise that a failure is an UNKNOWN id, not a
  slow scan; feeding ordinary password typos into it drains a budget meant for
  rare unknown-badge events, and when it empties the KIOSK 429s for every
  operator behind that egress IP, with no admin reset. The frontend steers
  badge-only operators onto the password path, so the two routes see the same
  people from the same IP all day.
* **Different right answer for the budget.** 5 wrong passwords lock ONE account,
  so one legitimate user can contribute five failures before their own lockout
  stops them. A budget of 8 cannot absorb two such users; see the derivation on
  ``PASSWORD_LOGIN_MAX_FAILURES``.

So: separate key prefixes, so neither route can spend the other's budget, and
separate constants, so neither route's sizing argument is quietly applied to the
other. Add a third route the same way — a new instance — not a shared one.

Storage mirrors the slowapi limiter configuration in ``app/main.py``
(``storage_uri=REDIS_URL or "memory://"``): Redis when ``settings.REDIS_URL``
is configured — the cross-worker deployment reality — else a process-local
in-memory counter (dev/test single-process).

FAIL-OPEN IS DELIBERATE (both instances): on a Redis outage the throttle logs a
warning and allows the attempt. A Redis blip must never brick a shift change, and
the slowapi per-path limits (which share the same fail-open posture and are
backstopped by the app-wide default limit) still bound request volume. A
sustained fail-open during an attack is itself a security event, hence the
SIEM-greppable ``employee_login_throttle_fail_open`` marker on the warning. The
marker string is shared by both instances ON PURPOSE — existing SIEM rules grep
for it — and the warning names the offending key prefix so an operator can still
tell which route degraded.
"""

import logging
import threading
import time
from typing import Dict, List, Optional

from fastapi import Request

from app.core.config import settings

logger = logging.getLogger(__name__)

# --- POST /auth/employee-login (kiosk badge route) -------------------------
#
# 8 failures / 15 min, then a 15-min cooldown. The window is generous enough
# that a fat-fingered crew at one station never trips it (a failure is an
# UNKNOWN id, not a slow scan), and tight enough that sweeping a 4-digit badge
# space from one IP takes ~2 weeks instead of ~17 hours at the 10/min cap.
#
# DO NOT retune these to accommodate /auth/login. The "a failure is an UNKNOWN
# id" premise is what makes 8 safe here, and it is false on the password route —
# which is exactly why that route has its own constants below rather than a
# loosened version of these.
EMPLOYEE_LOGIN_MAX_FAILURES = 8
EMPLOYEE_LOGIN_FAILURE_WINDOW_SECONDS = 15 * 60
EMPLOYEE_LOGIN_COOLDOWN_SECONDS = 15 * 60

# --- POST /auth/login (identifier + password route) ------------------------
#
# 60 failures / 6 h, then a 1-h cooldown — derived, not picked round. Two bounds
# have to hold at once, and they pull in opposite directions, so both are shown.
#
# LOWER BOUND — honest typing must never trip it.
#   A single legitimate user can contribute at most 5 failures before the
#   per-account lockout (5 -> 30 min, in the handler) stops them. Take 10 as a
#   plausible number of badge-holding users behind one NAT egress — a small
#   shop's floor signing into the web app on the same public IP:
#       5 failures/user x 10 users = 50 honest failures, worst case
#   60 clears that with margin, and reaching even the 50 requires all ten to lock
#   themselves out inside the SAME 6-hour window. Note the counted population is
#   smaller still: only ENUMERABLE identifiers count, so office users at a real
#   mail domain contribute nothing on either outcome.
#
# UPPER BOUND — a sweep of the ~10^4 badge space must still cost days, not hours.
#   TWO rates exist here and the ATTACKER PICKS THE FASTER, so both are computed.
#
#   (a) Pacing UNDER the block. A paced attacker gets (max_failures - 1) per
#       window forever, whatever the cooldown — the window is the only lever on
#       this one:
#           59 failures / 6 h = 236/day  ->  10^4 / 236 = ~42 days
#   (b) Deliberately TRIPPING the block and cycling it. The cycle is the time to
#       deliver a full budget plus the cooldown. Delivery is capped by the
#       slowapi 5/min per-path limit, so 60 failures take ~12 min:
#           cycle = 12 min + 60 min cooldown = ~72 min
#           60 failures / 1.2 h = 1200/day  ->  10^4 / 1200 = ~8 days
#
#   So the effective bound is ~8 DAYS, set by (b). Compare the slowapi limit
#   alone: 10^4 / 5 per min = ~33 HOURS. The throttle buys roughly 6x over no
#   throttle at all, not the ~30x a 6-hour cooldown would.
#
# WHY THE COOLDOWN IS SHORTER THAN THE WINDOW — an owner decision (2026-08-17),
# taken with the cost above stated. A cooldown equal to the window (6 h) makes
# (a) and (b) the same ~42 days, because hitting the max re-arms the key's TTL to
# the cooldown and a SHORT cooldown is therefore the fast lane. That is strictly
# better against a sweep. It was traded away for RECOVERABILITY: a tripped block
# has no admin reset, so a 6-hour cooldown means a site that trips it — through
# an attack, a misconfigured client, or a badge scanner stuck against the web
# form — loses password sign-in for most of a working day with nothing anyone on
# site can do. One hour is inside a shift. If the sweep resistance is later worth
# more than the recovery time, raise ONLY this constant: the window carries (a)
# and must not be shortened, or the paced rate rises with it.
#
# BLAST RADIUS OF A TRIP, which is what makes any block here tolerable: it
# refuses only ENUMERABLE identifiers on this one route. An address at a real
# domain still signs in normally, and the kiosk keeps its own untouched budget,
# so badge sign-in at the station is unaffected.
PASSWORD_LOGIN_MAX_FAILURES = 60
PASSWORD_LOGIN_FAILURE_WINDOW_SECONDS = 6 * 60 * 60
PASSWORD_LOGIN_COOLDOWN_SECONDS = 60 * 60


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
        # FAIL-OPEN IS DELIBERATE (see module docstring): the slowapi per-path
        # limit still holds, and a Redis blip must never brick a shift change.
        # Warn with a stable marker so a SIEM can alert on a sustained
        # fail-open.
        #
        # The marker token stays byte-identical now that two instances share it —
        # existing SIEM rules grep for exactly this string — but the key prefix
        # is named in the message, because "which login route just lost its
        # throttle" is the first question an operator asks and a shared marker
        # with no discriminator cannot answer it.
        logger.warning(
            "employee_login_throttle_fail_open: %s failed for throttle=%s ip=%s (%s); allowing attempt",
            action,
            self._key_prefix,
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

# A DISTINCT key prefix, not a shared one: this is the whole mechanism that stops
# ordinary wrong-password failures on /auth/login from spending the kiosk badge
# route's budget (and vice versa). Changing either prefix to match the other
# re-creates the outage described in the module docstring.
password_login_throttle = FailedLoginThrottle(
    key_prefix="auth:login:failed",
    max_failures=PASSWORD_LOGIN_MAX_FAILURES,
    window_seconds=PASSWORD_LOGIN_FAILURE_WINDOW_SECONDS,
    cooldown_seconds=PASSWORD_LOGIN_COOLDOWN_SECONDS,
)
