"""A floor PIN, and its bcrypt hash, never reach Sentry (PR #369 review findings 6 and 7).

The key-only ``EventScrubber`` matches dict KEYS, so three things slipped past it in a REAL
FastAPI error event: the raw request-body locals (``fastapi.routing``'s ``body_bytes``,
``app.main.limit_json_body_size``'s ``body``) holding ``b'{"employee_id": "0042", "pin": "2580"}'``
as a repr string; a bcrypt hash bound to any local; and SQLAlchemy's ``[parameters: {...}]`` in a
chained exception's message. A bcrypt hash of a 4-6 digit PIN IS the PIN to anyone who can run
bcrypt 10^4-10^6 times.

These tests are deliberately not a hand-built event. They boot Sentry through the PRODUCTION
``app.main.init_sentry`` (the real ``sentry_sdk.init`` with the production options: the scrubber,
``before_send``, the FastAPI integration) and swap only the transport for one that keeps every
envelope, then drive a FastAPI app -- carrying ``app.main``'s own body-size middleware and
HTTPException handler -- whose routes raise a 500 and a 503 with the PIN in the body, a hash in a
local and SQL parameters in the exception chain. The serialized event must contain neither the
PIN nor any ``$2b$`` hash. A control test proves the routes really do leak without
``before_send``, so the assertions are not vacuous.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterator, List

import pytest
import sentry_sdk
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sentry_sdk.transport import Transport
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException

import app.main as app_main
from app.api.endpoints.users import FloorPinSet
from app.core import observability
from app.core.config import settings
from app.core.security import get_password_hash
from app.schemas.user import EmployeeLoginRequest

pytestmark = pytest.mark.unit

PIN = "2580"
BADGE = "0042"
SIGN_ON_BODY = {"employee_id": BADGE, "pin": PIN}
FAKE_DSN = "https://public@example.ingest.sentry.io/1"

# The credential route the production rule drops everything for, and an ordinary path with the
# SAME handler, so the every-event layers (raw-body locals, hash values, SQL parameters) are
# proven on their own.
SIGN_ON_PATHS = ["/api/v1/auth/employee-login", "/api/v1/probe/sign-on-shaped"]
FLOOR_PIN_PATHS = ["/api/v1/users/7/floor-pin", "/api/v1/probe/users/7/floor-pin-shaped"]


class CapturingTransport(Transport):
    """Keeps every event Sentry would have sent, as the JSON it would have sent."""

    def __init__(self, options: Any = None) -> None:
        super().__init__(options)
        self.events: List[Dict[str, Any]] = []

    def capture_envelope(self, envelope: Any) -> None:
        for item in envelope.items:
            if item.type in ("event", "transaction") and item.payload.json is not None:
                self.events.append(item.payload.json)


def _sign_on_handler(payload: EmployeeLoginRequest, status_code: int) -> None:
    stored_hash = get_password_hash(PIN)  # a hash in a local, as the old sign-on frame held one
    # ...and one under a name no denylist knows: only the bcrypt-pattern layer catches it.
    cached_credential = get_password_hash(PIN)
    assert stored_hash and cached_credential and payload.employee_id
    if status_code == 503:
        raise HTTPException(status_code=503, detail="Unable to save audit record")
    raise RuntimeError("the audit insert lost the chain lock")


def _floor_pin_handler(uid: int, payload: FloorPinSet) -> None:
    hashed = get_password_hash(payload.pin)
    try:
        raise IntegrityError(
            "UPDATE users SET floor_pin_hash=? WHERE users.id = ?",
            {"floor_pin_hash": hashed, "id": uid},
            Exception("deadlock detected"),
        )
    except IntegrityError as exc:  # what atomic_security_write turns an AuditWriteError into
        raise HTTPException(status_code=503, detail="Unable to save audit record") from exc


def _build_app() -> FastAPI:
    probe = FastAPI()
    # app.main's own middleware: it holds the raw body in a ``body`` local.
    probe.middleware("http")(app_main.limit_json_body_size)

    @probe.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request, exc):  # mirrors app.main's handler
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    for path in SIGN_ON_PATHS:

        @probe.post(path)
        def sign_on_500(payload: EmployeeLoginRequest):
            _sign_on_handler(payload, 500)

        @probe.post(path + "/503")
        def sign_on_503(payload: EmployeeLoginRequest):
            _sign_on_handler(payload, 503)

    for path in FLOOR_PIN_PATHS:
        probe.put(path.replace("/7/", "/{uid}/"))(_floor_pin_handler)

    return probe


@pytest.fixture
def sentry_events(monkeypatch: pytest.MonkeyPatch) -> Iterator[CapturingTransport]:
    """Sentry initialized by ``app.main.init_sentry`` exactly as production does, minus the network."""
    transport = CapturingTransport()
    real_init = sentry_sdk.init

    def init_with_capturing_transport(**options: Any) -> Any:
        options["transport"] = transport
        return real_init(**options)

    monkeypatch.setattr(sentry_sdk, "init", init_with_capturing_transport)
    monkeypatch.setattr(settings, "SENTRY_DSN", FAKE_DSN)
    app_main.init_sentry()
    assert sentry_sdk.get_client().is_active(), "Sentry did not initialize"
    try:
        yield transport
    finally:
        sentry_sdk.get_client().close()
        sentry_sdk.get_global_scope().set_client(None)


def _serialized(transport: CapturingTransport) -> str:
    sentry_sdk.flush()
    assert transport.events, "no Sentry event was captured -- the assertion below would be vacuous"
    return json.dumps(transport.events)


# Random identifiers an event carries on its own -- event / trace / span ids, memory addresses in
# reprs, epoch timestamps, ISO timestamps -- can contain "2580" by chance. They are removed before
# the PIN is searched for, so the search is about the request's PIN and never a coincidence; the
# leak forms (a body repr, a dict value) keep the PIN standing on its own and are still found.
_INCIDENTAL_NUMBERS = re.compile(r"0x[0-9a-fA-F]+|[0-9a-fA-F]{16,}|\d{4}-\d\d-\d\dT[0-9:.+Z-]+|\d{8,}(?:\.\d+)?")


def _assert_no_pin_or_hash(serialized: str) -> None:
    assert PIN not in _INCIDENTAL_NUMBERS.sub("#", serialized)
    assert "$2b$" not in serialized and "$2a$" not in serialized and "$2y$" not in serialized


@pytest.mark.parametrize("path", SIGN_ON_PATHS)
@pytest.mark.parametrize("suffix", ["", "/503"], ids=["500", "503"])
def test_a_sign_on_error_event_carries_neither_the_pin_nor_a_hash(sentry_events, path, suffix):
    client = TestClient(_build_app(), raise_server_exceptions=False)

    response = client.post(path + suffix, json=SIGN_ON_BODY)

    assert response.status_code == (503 if suffix else 500)
    _assert_no_pin_or_hash(_serialized(sentry_events))


@pytest.mark.parametrize("path", FLOOR_PIN_PATHS)
def test_a_floor_pin_write_error_event_carries_neither_the_pin_nor_a_hash(sentry_events, path):
    client = TestClient(_build_app(), raise_server_exceptions=False)

    response = client.put(path, json={"pin": PIN})

    assert response.status_code == 503
    serialized = _serialized(sentry_events)
    _assert_no_pin_or_hash(serialized)
    # The chained IntegrityError is reported, with its parameters cut out.
    assert "IntegrityError" in serialized and "[parameters: [Filtered]]" in serialized


def test_an_ordinary_route_keeps_its_frames_minus_the_raw_body(sentry_events):
    """Off a credential route the frames stay useful: variables are kept, only the raw-body ones go."""
    client = TestClient(_build_app(), raise_server_exceptions=False)

    client.post("/api/v1/probe/sign-on-shaped", json=SIGN_ON_BODY)

    sentry_sdk.flush()
    frames = [
        frame
        for event in sentry_events.events
        for value in (event.get("exception") or {}).get("values", [])
        for frame in (value.get("stacktrace") or {}).get("frames", [])
    ]
    assert any(frame.get("vars") for frame in frames), "frame variables were expected off a credential route"
    for frame in frames:
        assert not set(frame.get("vars") or {}) & {"body_bytes", "body"}, frame.get("function")
        stored = (frame.get("vars") or {}).get("stored_hash")
        assert stored in (None, "[Filtered]"), stored


def test_a_credential_route_drops_every_frame_variable_and_the_body(sentry_events):
    client = TestClient(_build_app(), raise_server_exceptions=False)

    client.post("/api/v1/auth/employee-login", json=SIGN_ON_BODY)

    sentry_sdk.flush()
    (event,) = [event for event in sentry_events.events if event.get("exception")]
    assert "data" not in (event.get("request") or {})
    for value in event["exception"]["values"]:
        for frame in (value.get("stacktrace") or {}).get("frames", []):
            assert "vars" not in frame, frame.get("function")


def test_control_without_before_send_the_pin_does_leak(sentry_events, monkeypatch):
    """Non-vacuity: with only the key scrubber (no ``before_send``), these routes leak the PIN (through
    the raw-body locals) and a hash (through a local whose name is on no denylist)."""
    sentry_sdk.get_client().close()
    monkeypatch.setattr(observability, "scrub_sentry_event", lambda event, hint=None: event)
    app_main.init_sentry()
    client = TestClient(_build_app(), raise_server_exceptions=False)

    client.post("/api/v1/probe/sign-on-shaped", json=SIGN_ON_BODY)

    serialized = _serialized(sentry_events)
    assert PIN in _INCIDENTAL_NUMBERS.sub(
        "#", serialized
    ), "the raw body local no longer carries the PIN; the leak tests above prove nothing"
    assert "$2b$" in serialized


# ------------------------------------------------- the REAL password-bearing create routes
#
# PR #369 re-check: a request MODEL in a frame variable reprs its fields --
# ``UserCreate(..., password='...')`` -- which no key list can see. Driven here through the real
# ``app.main`` app (real routes, real models), with a dependency forced to fail after validation.

PASSWORD = "Glacier7!Harbor-Quartz"


@pytest.fixture
def real_client(db_session) -> Iterator[TestClient]:
    from app.db.database import get_db

    def override_get_db():
        yield db_session

    app_main.app.dependency_overrides[get_db] = override_get_db
    try:
        with TestClient(app_main.app, raise_server_exceptions=False) as test_client:
            yield test_client
    finally:
        app_main.app.dependency_overrides.clear()


def _fail_after_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("provisioning store unavailable")

    monkeypatch.setattr("app.api.endpoints.users.create_tenant_user", boom)
    monkeypatch.setattr("app.api.endpoints.companies.onboard_company", boom)


def _post_user(client: TestClient, headers: Dict[str, str]):
    return client.post(
        "/api/v1/users/",
        json={
            "email": "new.hire@werco.com",
            "employee_id": "NH-1001",
            "first_name": "New",
            "last_name": "Hire",
            "password": PASSWORD,
            "role": "operator",
        },
        headers=headers,
    )


def _post_company_signup(client: TestClient):
    return client.post(
        "/api/v1/companies/register",
        json={
            "company_name": "Signup Probe Co",
            "admin_email": "owner@signup-probe.com",
            "admin_first_name": "Owner",
            "admin_last_name": "Probe",
            "admin_password": PASSWORD,
        },
        headers={"X-Requested-With": "XMLHttpRequest"},
    )


@pytest.mark.parametrize("route", ["users_create", "company_signup"])
def test_a_password_create_route_error_event_carries_no_password(
    sentry_events, real_client, admin_headers, monkeypatch, route
):
    _fail_after_validation(monkeypatch)

    response = _post_user(real_client, admin_headers) if route == "users_create" else _post_company_signup(real_client)

    assert response.status_code == 500, response.text
    serialized = _serialized(sentry_events)
    assert PASSWORD not in serialized
    assert "$2b$" not in serialized


@pytest.mark.parametrize("route", ["users_create", "company_signup"])
def test_control_without_before_send_the_password_does_leak(
    sentry_events, real_client, admin_headers, monkeypatch, route
):
    """Non-vacuity: the same requests with only the key scrubber leak the password."""
    sentry_sdk.get_client().close()
    monkeypatch.setattr(observability, "scrub_sentry_event", lambda event, hint=None: event)
    app_main.init_sentry()
    _fail_after_validation(monkeypatch)

    _post_user(real_client, admin_headers) if route == "users_create" else _post_company_signup(real_client)

    assert PASSWORD in _serialized(sentry_events)


# ------------------------------------------------------------ breadcrumbs and log entries

# Assembled at run time: a captured frame carries its SOURCE lines as context, so a secret written
# literally near the logging call would "leak" from this file's own code, not from the scrubber.
LOGGED_SECRET = "".join(["hun", "ter2-", "Sec", "ret"])
FAILING_ROW = (
    '(psycopg2.errors.NotNullViolation) null value in column "first_name" violates not-null constraint\n'
    "DETAIL:  Failing row contains (5, 1, EMP-1, op@werco.com, {hash}, null, Op, operator, {hash}).\n"
    "[SQL: UPDATE users SET first_name=%(first_name)s WHERE users.id = %(users_id)s]\n"
    "[parameters: {{'first_name': None, 'password': '{secret}'}}]"
)


def test_a_logged_db_error_is_scrubbed_in_its_event_and_its_breadcrumb(sentry_events):
    import logging

    text = FAILING_ROW.format(hash=get_password_hash(PIN), secret=LOGGED_SECRET)
    logger = logging.getLogger("app.services.audit_service")
    # As a log line the old code wrote (f-string), and with the text as a param.
    logger.error(f"Failed to create audit log: {text}")
    logger.error("Failed to create audit log: %s", text)
    logger.warning(f"context password={LOGGED_SECRET} pin: {PIN}")
    try:
        raise RuntimeError("a later failure carries the earlier log lines as breadcrumbs")
    except RuntimeError as exc:
        sentry_sdk.capture_exception(exc)

    serialized = _serialized(sentry_events)
    assert any(event.get("breadcrumbs") for event in sentry_events.events), "the breadcrumbs were expected"
    assert any(event.get("logentry") for event in sentry_events.events), "the log events were expected"
    assert "$2b$" not in serialized
    assert LOGGED_SECRET not in serialized
    _assert_no_pin_or_hash(serialized)
    assert "Failing row contains ([Filtered])" in serialized


def test_the_audit_writer_logs_a_db_error_by_class_and_sqlstate_only(db_session, monkeypatch, caplog):
    from sqlalchemy.exc import OperationalError

    from app.services.audit_service import AuditService

    class _DriverError(Exception):
        pgcode = "23502"

    hashed = get_password_hash(PIN)

    def lost(*_args: Any, **_kwargs: Any) -> None:
        raise OperationalError("UPDATE users", {}, _DriverError(FAILING_ROW.format(hash=hashed, secret=LOGGED_SECRET)))

    monkeypatch.setattr(AuditService, "_acquire_chain_lock", lost)
    with caplog.at_level("ERROR"), pytest.raises(OperationalError):
        AuditService(db_session).log(action="UPDATE", resource_type="user", resource_id=1)

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "Failed to create audit log" in logged
    assert "OperationalError [_DriverError] (SQLSTATE 23502)" in logged
    assert "$2b$" not in logged and "Failing row" not in logged and LOGGED_SECRET not in logged
