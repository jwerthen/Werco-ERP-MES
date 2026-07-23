"""Per-IP failed-attempt throttle on /auth/employee-login (compensating control).

The Kiosk Foundry redesign raised the slowapi per-path limit on employee-login
from 3/min to 10/min so a shift change can cycle badges through one shared
station. 10/min alone triples the online guessing budget on an unauthenticated
endpoint that mints a full token from a bare badge ID, so the endpoint adds a
per-IP FAILED-attempt throttle (``app/core/login_throttle.py``): 8 failures
within 15 minutes -> 429 with a 15-minute cooldown; successes never count; the
check runs BEFORE the user lookup (zero account probing while throttled);
storage errors fail open (the slowapi limit still holds).

Isolation: the autouse fixture below resets the throttle's counters and forces
memory mode (no REDIS_URL) so each test gets a deterministic budget; the
conftest ``_reset_rate_limiter`` autouse fixture keeps the separate slowapi
10/min budget fresh, and every test here stays within 10 requests so the two
controls never interfere.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.core import login_throttle
from app.core.config import settings
from app.core.login_throttle import EMPLOYEE_LOGIN_MAX_FAILURES, employee_login_throttle
from app.models.audit_log import AuditLog

EMPLOYEE_LOGIN = "/api/v1/auth/employee-login"
UNKNOWN_BADGE = {"employee_id": "9999"}


@pytest.fixture(autouse=True)
def _reset_throttle(monkeypatch):
    """Fresh throttle state per test, forced into memory mode."""
    monkeypatch.setattr(settings, "REDIS_URL", None)
    employee_login_throttle.reset()
    yield
    employee_login_throttle.reset()


def _fail(client: TestClient):
    return client.post(EMPLOYEE_LOGIN, json=UNKNOWN_BADGE)


def _assert_throttled(response):
    assert response.status_code == 429, response.text
    assert "Too many failed sign-in attempts" in response.json()["detail"]
    retry_after = response.headers.get("Retry-After")
    assert retry_after is not None, "throttle 429 must carry Retry-After"
    assert 1 <= int(retry_after) <= login_throttle.EMPLOYEE_LOGIN_COOLDOWN_SECONDS


def test_failures_below_threshold_pass_through(client: TestClient, test_user):
    """Up to N-1 failures still reach the endpoint, and a good badge still works."""
    for i in range(EMPLOYEE_LOGIN_MAX_FAILURES - 1):
        r = _fail(client)
        assert r.status_code == 401, f"attempt {i} unexpectedly {r.status_code}: {r.text}"

    r = client.post(EMPLOYEE_LOGIN, json={"employee_id": test_user.employee_id})
    assert r.status_code == 200, r.text


def test_ninth_failed_attempt_within_window_throttled(client: TestClient, db_session: Session):
    """The attempt after N failures is refused 429 BEFORE any user lookup, audited."""
    for i in range(EMPLOYEE_LOGIN_MAX_FAILURES):
        r = _fail(client)
        assert r.status_code == 401, f"attempt {i} unexpectedly {r.status_code}: {r.text}"

    _assert_throttled(_fail(client))

    # The rejection writes the standard auth audit event with a throttled marker
    # (through log_auth_event -- never a direct audit_log write).
    row = (
        db_session.query(AuditLog)
        .filter(AuditLog.action == "EMPLOYEE_LOGIN_BLOCKED", AuditLog.error_message.like("Throttled%"))
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert row is not None, "throttled rejection must write an EMPLOYEE_LOGIN_BLOCKED audit event"
    assert row.success == "false"


def test_success_does_not_count_toward_window(client: TestClient, test_user):
    """Successful logins neither trip nor advance the failure counter."""
    for _ in range(EMPLOYEE_LOGIN_MAX_FAILURES - 1):
        assert _fail(client).status_code == 401

    # Not blocked at N-1 failures; the success itself must not become the Nth.
    r = client.post(EMPLOYEE_LOGIN, json={"employee_id": test_user.employee_id})
    assert r.status_code == 200, r.text

    # Still only N-1 failures on the books: one more FAILURE is answered (401),
    # which proves the success above did not count...
    assert _fail(client).status_code == 401
    # ...and that Nth failure arms the block for the attempt after it.
    _assert_throttled(_fail(client))


def test_window_expiry_resets_counter(client: TestClient, monkeypatch):
    """After the cooldown elapses the counter is gone and attempts flow again."""
    for _ in range(EMPLOYEE_LOGIN_MAX_FAILURES):
        assert _fail(client).status_code == 401
    _assert_throttled(_fail(client))

    real_now = employee_login_throttle._now()
    monkeypatch.setattr(
        employee_login_throttle,
        "_now",
        lambda: real_now + login_throttle.EMPLOYEE_LOGIN_COOLDOWN_SECONDS + 60,
    )
    # Back to the ordinary 401 -- the throttle no longer intercepts.
    assert _fail(client).status_code == 401


def test_storage_outage_fails_open(client: TestClient, monkeypatch, caplog):
    """A dead counter backend allows attempts (slowapi still caps volume) and warns."""

    def _boom():
        raise RuntimeError("redis down")

    monkeypatch.setattr(employee_login_throttle, "_redis_client", _boom)

    with caplog.at_level("WARNING"):
        for i in range(EMPLOYEE_LOGIN_MAX_FAILURES + 1):
            r = _fail(client)
            # Never 429 from the throttle: every attempt reaches the endpoint.
            assert r.status_code == 401, f"attempt {i} unexpectedly {r.status_code}: {r.text}"

    assert any("employee_login_throttle_fail_open" in rec.message for rec in caplog.records)
