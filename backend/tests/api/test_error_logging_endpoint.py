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
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

import app.main as app_main
from app.api.endpoints import errors as errors_module
from app.core.media_types import is_text_plain_media_type
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


# ---------------------------------------------------------------------------
# The beacon wire format: a text/plain JSON body -- the SUPPORTED contract.
#
# The client's ``navigator.sendBeacon`` body is labelled ``text/plain;charset=UTF-8``
# on purpose: it is CORS-safelisted, so the cross-origin beacon needs no preflight,
# while Chromium refuses an ``application/json`` Blob beacon outright. FastAPI parses
# only ``application/json`` (or ``+json``) as JSON, so until 2026-11-05 every beacon --
# including the global-boundary crashes that blank the screen -- was rejected 422 and
# reached no log and no Sentry. Every test above posts ``json=``, which is why it was
# never seen. The route now reads both labels.
# ---------------------------------------------------------------------------

_BEACON_CONTENT_TYPE = "text/plain;charset=UTF-8"


def _post_text_plain(client: TestClient, body, content_type: str = _BEACON_CONTENT_TYPE):
    raw = body if isinstance(body, (bytes, str)) else json.dumps(body)
    return client.post(ENDPOINT, content=raw, headers={"content-type": content_type})


def test_text_plain_beacon_body_is_accepted_and_processed(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """The sendBeacon shape lands: 200, and the entries are actually processed
    (the global entry reaches the alert path), not merely acknowledged."""
    alerted: list[str] = []

    async def _fake_alert(error):
        alerted.append(error.id)

    monkeypatch.setattr(errors_module, "send_error_alert", _fake_alert)

    payload = {"errors": [_entry(id="err_crash", boundaryLevel="global"), _entry(id="err_page")]}
    response = _post_text_plain(client, payload)

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "queued", "count": 2}
    assert alerted == ["err_crash"]


@pytest.mark.parametrize("content_type", ["text/plain", "text/plain; charset=utf-8", "TEXT/PLAIN;charset=UTF-8"])
def test_text_plain_label_variants_are_accepted(client: TestClient, content_type: str):
    response = _post_text_plain(client, {"errors": [_entry()]}, content_type=content_type)

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "queued", "count": 1}


def _post_json_twin(client: TestClient, raw):
    return client.post(ENDPOINT, content=raw, headers={"content-type": "application/json"})


# A bare "422" would pass against the PRE-fix code too: it refused every text/plain
# body with 422 -- one "Input should be a valid dictionary" error at loc ["body"],
# because the body was never parsed. So each case below compares the text/plain
# response with the SAME bytes labelled application/json: identical status and an
# identical error list proves the text/plain body was parsed and validated exactly
# as JSON is, field by field.
_INVALID_BODIES = {
    "field_over_cap": json.dumps({"errors": [_entry(message="A" * 4001)]}),
    "batch_over_cap": json.dumps(
        {"errors": [_entry(id=f"err_{i}") for i in range(errors_module.MAX_ERRORS_PER_REQUEST + 1)]}
    ),
    "missing_required_fields": json.dumps({"errors": [{"id": "err_1"}]}),
    "not_json": b"this is not json",
}


@pytest.mark.parametrize("case", sorted(_INVALID_BODIES))
def test_text_plain_body_gets_the_same_validation_as_json(client: TestClient, case: str):
    """Relabelling is not a validation bypass: the per-field and batch caps apply."""
    raw = _INVALID_BODIES[case]

    as_text = _post_text_plain(client, raw)
    as_json = _post_json_twin(client, raw)

    assert as_json.status_code == 422, as_json.text
    assert as_text.status_code == 422, as_text.text
    assert as_text.json() == as_json.json()
    # And the errors are about the body's CONTENT, not about the body being unparsed.
    assert all(error["loc"] != ["body"] for error in as_text.json()["detail"]), as_text.text


def test_text_plain_body_that_is_not_json_is_a_422(client: TestClient):
    response = _post_text_plain(client, b"this is not json")

    assert response.status_code == 422, response.text
    assert [error["type"] for error in response.json()["detail"]] == ["json_invalid"]


def test_text_plain_body_that_is_not_utf8_is_refused_not_a_500(client: TestClient):
    """``json.loads`` raises UnicodeDecodeError (not JSONDecodeError) on undecodable bytes.
    An unauthenticated route must refuse that, never 500 on it."""
    response = _post_text_plain(client, b'{"errors": ["\xff\xfe"]}')

    assert 400 <= response.status_code < 500, response.text


def test_text_plain_beacon_is_bound_by_the_json_body_cap(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """Parsing text/plain as JSON must not open an uncapped unauthenticated JSON parse:
    main.py's body cap covers the text/plain label on this path."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "MAX_JSON_BODY_BYTES", 1024)
    body = json.dumps({"errors": [_entry(stack="A" * 4000)]})
    assert len(body) > 1024

    response = _post_text_plain(client, body)

    assert response.status_code == 413, response.text


def test_only_text_plain_is_relabelled(client: TestClient):
    """Any other non-JSON label is still refused -- the carve-in is for the beacon's
    exact shape, not "parse whatever arrives"."""
    response = _post_text_plain(client, {"errors": [_entry()]}, content_type="application/octet-stream")

    assert response.status_code == 422, response.text


def test_text_plain_media_type_helper():
    assert is_text_plain_media_type("text/plain")
    assert is_text_plain_media_type("text/plain;charset=UTF-8")
    assert is_text_plain_media_type(" Text/Plain ; charset=utf-8")
    assert not is_text_plain_media_type("application/json")
    assert not is_text_plain_media_type("text/plainx")
    # ``email.message`` would read a malformed label as text/plain; this helper must not,
    # or a garbage Content-Type would be relabelled and parsed as JSON.
    assert not is_text_plain_media_type("garbage")
    assert not is_text_plain_media_type("")
    assert not is_text_plain_media_type(None)


def test_the_relabel_is_installed_on_exactly_the_path_the_body_cap_covers():
    """The ``text/plain`` relabel and main.py's ``text/plain`` cap are two halves of one
    rule: a relabelled body must be a capped body. So the route class sits on the ONE
    ``/errors/log`` route (not the whole router, where a POST added later would inherit
    the relabel without the cap), and that route's path is exactly the cap's path."""
    relabelled = {route.path for route in app_main.app.routes if isinstance(route, errors_module._BeaconBodyRoute)}

    assert relabelled == {ENDPOINT}
    assert app_main._ERROR_BEACON_PATH == ENDPOINT
    # Its sibling on the same router is an ordinary route.
    health = [route for route in app_main.app.routes if getattr(route, "path", None) == "/api/v1/errors/health"]
    assert health and not any(isinstance(route, errors_module._BeaconBodyRoute) for route in health)


def _chunked(data: bytes, chunk_size: int = 256):
    """A generator body: httpx sends it ``Transfer-Encoding: chunked`` with NO Content-Length."""
    for offset in range(0, len(data), chunk_size):
        yield data[offset : offset + chunk_size]


def _send_chunked_text_plain(client: TestClient, data: bytes):
    request = client.build_request(
        "POST", ENDPOINT, content=_chunked(data), headers={"content-type": _BEACON_CONTENT_TYPE}
    )
    # The premise, established rather than assumed: no declared length for gate 1 to read.
    assert "content-length" not in request.headers
    assert request.headers["transfer-encoding"] == "chunked"
    return client.send(request)


def test_chunked_text_plain_beacon_is_capped_on_the_bytes_that_actually_arrive(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """Gate 2 of the body cap (the post-read length check) covers the text/plain label on
    this path too. The 413 test above sends a Content-Length, so it only reaches gate 1."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "MAX_JSON_BODY_BYTES", 1024)
    oversized = json.dumps({"errors": [_entry(stack="A" * 4000)]}).encode()
    assert len(oversized) > 1024

    response = _send_chunked_text_plain(client, oversized)

    assert response.status_code == 413, response.text


def test_chunked_text_plain_beacon_under_the_cap_reaches_the_route_intact(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """The cap middleware READS the body to measure it; the relabelled request must then be
    handed those same bytes (Starlette replays the cached body), not an empty stream."""
    from app.core.config import settings

    alerted: list[str] = []

    async def _fake_alert(error):
        alerted.append(error.id)

    monkeypatch.setattr(settings, "MAX_JSON_BODY_BYTES", 4096)
    monkeypatch.setattr(errors_module, "send_error_alert", _fake_alert)
    body = json.dumps({"errors": [_entry(id="err_chunked", boundaryLevel="global")]}).encode()
    assert len(body) < 4096

    response = _send_chunked_text_plain(client, body)

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "queued", "count": 1}
    assert alerted == ["err_chunked"]


def test_openapi_contract_for_the_beacon_route_is_unchanged():
    """The text/plain carve-in lives in a route class, not in a raw-``Request`` handler, so the
    published request body is still the typed ``ErrorLogRequest`` JSON schema -- the API docs
    and every generated client stay accurate. A handler that parsed the body itself would
    silently drop this schema."""
    operation = app_main.app.openapi()["paths"][ENDPOINT]["post"]
    content = operation["requestBody"]["content"]

    assert set(content) == {"application/json"}
    assert content["application/json"]["schema"] == {"$ref": "#/components/schemas/ErrorLogRequest"}
    assert operation["requestBody"]["required"] is True


def test_text_plain_is_not_parsed_as_json_on_any_other_router(client: TestClient):
    """The route class is installed on the ``/errors/log`` route ONLY -- the carve-in must not
    leak app-wide (e.g. by moving ``route_class`` onto a router). ``/auth/refresh`` is
    an unauthenticated, CSRF-exempt JSON route: a parsed body would be checked as a refresh
    token (401); an unparsed one fails validation as a whole (422 at loc ["body"])."""
    response = client.post(
        "/api/v1/auth/refresh",
        content=json.dumps({"refresh_token": "not-a-real-token"}),
        headers={"content-type": _BEACON_CONTENT_TYPE},
    )

    assert response.status_code == 422, response.text
    assert [error["loc"] for error in response.json()["detail"]] == [["body"]]

    # Control: the same bytes labelled JSON ARE parsed, and refused on their merits.
    as_json = client.post(
        "/api/v1/auth/refresh",
        content=json.dumps({"refresh_token": "not-a-real-token"}),
        headers={"content-type": "application/json"},
    )
    assert as_json.status_code == 401, as_json.text


@pytest.mark.parametrize("path", [ENDPOINT, "/api/v1/auth/refresh"])
def test_undecodable_body_with_a_non_json_label_is_refused_not_a_500(path: str):
    """``validation_exception_handler`` (app/main.py) echoes each error's ``input``; for a
    non-JSON label that is the raw body BYTES, which ``jsonable_encoder`` ``.decode()``s
    strictly. Undecodable bytes used to raise inside the 422 handler and turn an
    unauthenticated request into a 500 -- an ERROR line and Sentry events per request, on
    any JSON route. The handler now decodes them with replacement characters."""
    client = TestClient(app_main.app, raise_server_exceptions=False)

    response = client.post(
        path, content=b'{"errors": ["\xff\xfe"]}', headers={"content-type": "application/octet-stream"}
    )

    assert response.status_code == 422, response.text
    [error] = response.json()["detail"]
    assert error["loc"] == ["body"]
    assert error["input"] == '{"errors": ["\ufffd\ufffd"]}'


def test_decodable_raw_body_in_a_422_is_echoed_exactly_as_before():
    """The undecodable-bytes fix must not change the 422 for bytes that DO decode: those
    were always echoed as their text, and still are, byte for byte."""
    response = TestClient(app_main.app).post(
        "/api/v1/auth/refresh", content=b"plain caf\xc3\xa9 body", headers={"content-type": "application/octet-stream"}
    )

    assert response.status_code == 422, response.text
    [error] = response.json()["detail"]
    assert error["input"] == "plain caf\u00e9 body"
