"""The unauthenticated frontend error beacon must never write to the database.

``POST /api/v1/errors/log`` is unauthenticated and CSRF-exempt by necessity —
``navigator.sendBeacon`` on page unload cannot attach an Authorization header.
It used to resolve the client-supplied ``userId`` string to a ``User`` row with
no tenant scoping and hand that user to ``AuditService``, so any internet caller
could inject rows into the tamper-evident audit chain attributed to a named
employee in a named company. Those rows are immutable by DB trigger (migrations
008/060): they can never be removed, only reasoned about.

The amplification was worse than the forgery. The ``errors`` list had no length
cap, and every entry became one audit INSERT taking the single install-wide
``pg_advisory_xact_lock`` that serializes the hash chain — so one 256 KB request
(~2,000 minimal entries) could stall EVERY audited write in the system
(completions, receipts, NCRs) while the background task drained.

Both are fixed at the root: the endpoint writes nothing at all. These tests pin
that end state — the value-level assertion that a posted error produces no audit
row, the boundary assertions on the new caps, and (GUARD, below) a structural
assertion that the module cannot reach a database at all, which is what stops
the write from being reintroduced by someone who reads only the handler.
"""

import ast
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import app.main as app_main
from app.api.endpoints import errors as errors_module
from app.models.audit_log import AuditLog
from app.models.user import User

ENDPOINT = "/api/v1/errors/log"

_RATE_LIMITING_ON = getattr(app_main, "ENDPOINT_RATE_LIMITS", None) is not None


def _entry(**overrides) -> dict:
    """A realistic beacon entry, shaped like frontend/src/services/errorLogging.ts."""
    entry = {
        "id": "err_0123456789",
        "message": "TypeError: Cannot read properties of undefined (reading 'id')",
        "stack": "TypeError: ...\n    at WorkOrders (index-abc123.js:41:1817)",
        "componentStack": "\n    in WorkOrders\n    in ErrorBoundary",
        "boundaryName": "WorkOrdersBoundary",
        "boundaryLevel": "page",
        "url": "https://wercomfg.app/work-orders",
        "timestamp": "2026-08-01T14:03:22.001Z",
        "userAgent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
        "sessionId": "session_0123456789abcdef",
        "metadata": {"screenWidth": 1920, "screenHeight": 1080, "language": "en-US"},
    }
    entry.update(overrides)
    return entry


def test_posted_client_error_is_queued_and_writes_no_audit_row(
    client: TestClient, db_session: Session, admin_user: User
):
    """The forgery vector: a beacon naming a real admin's id writes NO audit row.

    ``userId`` is attacker-controlled (the real client fills it from
    sessionStorage, and the request carries no credentials), so it must never
    reach the audit chain. TestClient runs BackgroundTasks synchronously before
    returning, so by the time we assert, the background processing has finished.
    """
    payload = {"errors": [_entry(userId=str(admin_user.id))]}

    response = client.post(ENDPOINT, json=payload)

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "queued", "count": 1}

    # No audit row at all — not one attributed to the named admin, and not an
    # unattributed one either (an anonymous row would still burn a chain
    # sequence number and take the global chain lock).
    assert db_session.query(AuditLog).count() == 0
    assert db_session.query(AuditLog).filter(AuditLog.user_id == admin_user.id).count() == 0
    assert db_session.query(AuditLog).filter(AuditLog.action == "FRONTEND_ERROR").count() == 0


def test_full_client_batch_is_still_accepted(client: TestClient, db_session: Session):
    """The cap must not break the real client: 50 entries is exactly maxQueueSize."""
    payload = {"errors": [_entry(id=f"err_{i}") for i in range(errors_module.MAX_ERRORS_PER_REQUEST)]}

    response = client.post(ENDPOINT, json=payload)

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "queued", "count": 50}
    assert db_session.query(AuditLog).count() == 0


def test_oversized_batch_is_rejected_by_validation(client: TestClient, db_session: Session):
    """One entry past the cap is a 422 — the batch never reaches the handler."""
    payload = {"errors": [_entry(id=f"err_{i}") for i in range(errors_module.MAX_ERRORS_PER_REQUEST + 1)]}

    response = client.post(ENDPOINT, json=payload)

    assert response.status_code == 422, response.text
    assert db_session.query(AuditLog).count() == 0


@pytest.mark.parametrize(
    "field,length",
    [
        ("message", 4001),
        ("stack", 20001),
        ("componentStack", 20001),
        ("url", 2001),
        ("userAgent", 1001),
        ("userId", 65),
        ("sessionId", 129),
        ("id", 129),
        ("boundaryName", 201),
        ("boundaryLevel", 51),
        ("timestamp", 65),
    ],
)
def test_oversized_string_field_is_rejected_by_validation(client: TestClient, field: str, length: int):
    """Every string field is length-bounded, so one entry cannot carry a payload
    of arbitrary size into the logger."""
    response = client.post(ENDPOINT, json={"errors": [_entry(**{field: "A" * length})]})

    assert response.status_code == 422, response.text


def test_empty_batch_is_accepted(client: TestClient):
    """An empty list is a no-op, not an error — the caps only bound the top end."""
    response = client.post(ENDPOINT, json={"errors": []})

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "queued", "count": 0}


def test_undeclared_metadata_is_ignored_not_rejected(client: TestClient):
    """The client still sends ``metadata``; the server no longer models it.

    It was the one field left with no length bound, it is never logged or
    emitted, and it carries ``preservedData`` — arbitrary in-progress form
    contents. Dropping the field keeps unbounded client data out of the process
    without breaking the wire contract: Pydantic ignores undeclared fields.
    """
    entry = _entry()
    entry["metadata"] = {"preservedData": {"secret": "x" * 50_000}, "screenWidth": 1920}

    response = client.post(ENDPOINT, json={"errors": [entry]})

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "queued", "count": 1}
    assert "metadata" not in errors_module.ErrorLogEntry.model_fields


def test_global_boundary_alerts_are_capped_per_request(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """``boundaryLevel`` is attacker-supplied and "global" costs a Sentry event.

    Uncapped, one 50-entry batch spends 50 events and the rate limit permits 60
    requests/minute — up to 3,000 events/minute from a spoofable field, burning
    the quota that buys real error visibility.
    """
    sent: list[str] = []

    async def _fake_alert(error):
        sent.append(error.id)

    monkeypatch.setattr(errors_module, "send_error_alert", _fake_alert)

    payload = {"errors": [_entry(id=f"err_{i}", boundaryLevel="global") for i in range(50)]}
    response = client.post(ENDPOINT, json=payload)

    assert response.status_code == 200, response.text
    assert len(sent) == errors_module.MAX_GLOBAL_ALERTS_PER_REQUEST


def test_non_global_entries_never_alert(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """Only the "global" level alerts — capping did not widen the trigger."""
    sent: list[str] = []

    async def _fake_alert(error):
        sent.append(error.id)

    monkeypatch.setattr(errors_module, "send_error_alert", _fake_alert)

    payload = {"errors": [_entry(id=f"err_{i}", boundaryLevel="page") for i in range(10)]}
    response = client.post(ENDPOINT, json=payload)

    assert response.status_code == 200, response.text
    assert sent == []


@pytest.mark.skipif(
    not _RATE_LIMITING_ON,
    reason="Rate limiting disabled in this environment (settings.RATE_LIMIT_ENABLED=False)",
)
def test_errors_log_is_rate_limited(client: TestClient):
    """The beacon has its own per-path ceiling (60/min), not just the global default.

    Deliberately generous: this is a whole shop behind one NAT IP (kiosks,
    wallboards, office tabs), and a single tab flushes at most every 5s. The cap
    exists to bound abuse of an unauthenticated path, not to ration real reports.
    """
    payload = {"errors": [_entry()]}

    for i in range(60):
        r = client.post(ENDPOINT, json=payload)
        assert r.status_code == 200, f"request {i} unexpectedly {r.status_code}: {r.text}"

    rejected = client.post(ENDPOINT, json=payload)
    assert rejected.status_code == 429, rejected.text
    assert "Rate limit exceeded" in rejected.json()["detail"]


def test_errors_log_rate_limit_is_registered():
    """Config-level assertion, so the entry cannot be dropped from the map
    without a failure even where the limiter itself is disabled."""
    limits = getattr(app_main, "ENDPOINT_RATE_LIMITS", None)
    if limits is None:
        pytest.skip("Rate limiting disabled in this environment")
    assert limits.get(ENDPOINT) == "60/minute"


def test_error_logging_module_cannot_reach_the_database():
    """GUARD: structural, so it holds for code that does not exist yet.

    The value-level tests above prove today's handler writes nothing. This one
    proves the *module* has no database or audit reach at all — no session
    factory, no models, no AuditService — so reintroducing the write means
    adding an import that fails this test, rather than quietly adding a call
    inside a background task nobody re-reads. Docstrings naturally mention
    ``AuditService`` (explaining why it is gone), so this asserts on the import
    graph via AST, not on source text.
    """
    source_path = Path(errors_module.__file__)
    tree = ast.parse(source_path.read_text())

    forbidden_prefixes = ("app.db", "app.models", "app.services.audit_service", "sqlalchemy")
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)

    offenders = [name for name in imported if name.startswith(forbidden_prefixes)]
    assert not offenders, (
        f"{source_path.name} must stay database-free — it is an unauthenticated, "
        f"CSRF-exempt endpoint. Offending imports: {offenders}"
    )
