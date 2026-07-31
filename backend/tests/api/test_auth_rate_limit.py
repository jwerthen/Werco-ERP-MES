"""Per-path auth rate-limit enforcement (brute-force protection).

These tests lock in the fix for the previously-unenforced AUTH_RATE_LIMITS map in
``app/main.py``: the stricter per-endpoint limits were declared but the limiter was
only ever applying the global default, leaving /auth/login and the visitor
station-login (a short numeric PIN) brute-forceable. Enforcement now returns HTTP
429 once a path's limit is exceeded.

Isolation: the autouse ``_reset_rate_limiter`` fixture (conftest) clears the
limiter's counters before each test, so each test gets a fresh request budget and
these thresholds are deterministic.

Skip guard: if rate limiting is disabled in the environment
(settings.RATE_LIMIT_ENABLED=False) the enforcement middleware is never wired, so
these tests skip rather than fail.
"""

import pytest
from fastapi.testclient import TestClient

import app.main as app_main

LOGIN = "/api/v1/auth/login"
EMPLOYEE_LOGIN = "/api/v1/auth/employee-login"
STATION_LOGIN = "/api/v1/visitor-logs/station-login"
SETUP_STATUS = "/api/v1/auth/setup-status"

_RATE_LIMITING_ON = getattr(app_main, "AUTH_RATE_LIMITS", None) is not None

pytestmark = pytest.mark.skipif(
    not _RATE_LIMITING_ON,
    reason="Rate limiting disabled in this environment (settings.RATE_LIMIT_ENABLED=False)",
)


def _assert_rate_limited(response):
    """A rejected request is a well-formed 429 with our canonical body + Retry-After."""
    assert response.status_code == 429, response.text
    assert "Rate limit exceeded" in response.json()["detail"]
    retry_after = response.headers.get("Retry-After")
    assert retry_after is not None, "429 must carry a Retry-After header"
    assert 1 <= int(retry_after) <= 60


def test_login_rate_limited_after_five_attempts(client: TestClient):
    """/auth/login allows 5 attempts/min, then 429 — brute force is blocked."""
    creds = {"username": "nobody@example.com", "password": "wrongpassword123"}

    # First 5 attempts hit the endpoint (invalid creds -> 401), none rate-limited.
    for i in range(5):
        r = client.post(LOGIN, data=creds)
        assert r.status_code == 401, f"attempt {i} unexpectedly {r.status_code}: {r.text}"

    # 6th attempt within the window is rejected before reaching the handler.
    _assert_rate_limited(client.post(LOGIN, data=creds))


def test_station_login_rate_limited_after_five_attempts(client: TestClient):
    """Visitor station-login (shared numeric PIN) allows 5/min, then 429.

    This is the endpoint whose brute-force exposure the interim 6-8 digit PIN
    length was mitigating; enforcement here is what lets that constraint relax.
    """
    # station_id has no matching row -> 401 "Invalid station or PIN"; the PIN is a
    # schema-valid 4-8 digit string so the request reaches the endpoint (not 422).
    payload = {"station_id": 999999, "pin": "000000"}

    for i in range(5):
        r = client.post(STATION_LOGIN, json=payload)
        assert r.status_code == 401, f"attempt {i} unexpectedly {r.status_code}: {r.text}"

    _assert_rate_limited(client.post(STATION_LOGIN, json=payload))


def test_employee_login_rate_limited_after_ten_attempts(client: TestClient, test_user):
    """/auth/employee-login allows 10 requests/min, then 429 (kiosk badge login).

    Raised from 3/min for the Kiosk Foundry redesign: a shift change cycles
    several badges through ONE shared station within a minute. Uses SUCCESSFUL
    logins so this stays a pure slowapi-limit test — failed attempts are ALSO
    counted by the per-IP failed-attempt throttle (8 failures/15 min → 429,
    ``tests/api/test_employee_login_throttle.py``), which fires first on a
    failure-only sequence. Together: successes budgeted at 10/min, failures
    hard-stopped at 8 per 15 minutes.
    """
    payload = {"employee_id": test_user.employee_id}

    for i in range(10):
        r = client.post(EMPLOYEE_LOGIN, json=payload)
        assert r.status_code == 200, f"attempt {i} unexpectedly {r.status_code}: {r.text}"

    _assert_rate_limited(client.post(EMPLOYEE_LOGIN, json=payload))


def test_unrated_endpoint_not_limited_at_auth_threshold(client: TestClient):
    """The strict per-path limit is targeted, not global.

    An endpoint with no override (setup-status) tolerates well past the 3-5/min
    auth thresholds — proving login's cap is path-scoped, not a regression that
    throttles the whole app.
    """
    for _ in range(8):
        r = client.get(SETUP_STATUS)
        assert r.status_code == 200, r.text


def test_valid_login_within_budget_succeeds(client: TestClient, test_user, test_user_credentials):
    """Enforcement must not break the happy path: a valid login under the 5/min
    cap still returns 200.

    This guards the other failure mode of the fix — a middleware that mis-keys or
    over-counts would throttle legitimate users, not just brute-forcers. Runs after
    the autouse ``_reset_rate_limiter`` fixture, so the full budget is available;
    stays at 5 requests (the exact per-minute allowance) so none is rejected.
    """
    creds = {"username": test_user_credentials["email"], "password": test_user_credentials["password"]}

    for i in range(5):
        r = client.post(LOGIN, data=creds)
        assert r.status_code == 200, f"attempt {i} unexpectedly {r.status_code}: {r.text}"
        assert "access_token" in r.json()
