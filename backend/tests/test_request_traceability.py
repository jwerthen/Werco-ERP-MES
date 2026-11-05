"""A failed request must be traceable: a reference the user can quote, readable cross-origin.

Three gaps closed together:

* The app-wide 500 (``global_exception_handler`` in ``app/main.py``) returned a bare
  ``{"detail": "Internal server error"}`` -- nothing a user could quote, and no
  ``X-Correlation-ID`` header either, because that handler runs in Starlette's
  ``ServerErrorMiddleware``, OUTSIDE ``CorrelationIdMiddleware``, so the 500 never passed
  back through the layer that stamps the header. The handler now quotes the request's
  own correlation id in the body and the header, and on its log line.
* ``CORSMiddleware`` exposed no headers, so the cross-origin SPA (Vercel -> Railway)
  could not read ``X-Correlation-ID``, ``Content-Disposition`` (download filenames) or
  ``ETag`` (the conditional-request cache) at all.
* Sentry events carried only a ``component`` tag: no correlation id, company, user or
  role. They are tagged now -- only when Sentry is configured.

The 500 is provoked through a real route (``GET /api/v1/errors/health``, whose
``to_utc_iso`` is patched to raise) so the whole middleware stack is exercised; a
route registered just for the test would also work but must be cleaned out of the
shared app afterwards.
"""

import logging
from typing import Iterator

import pytest
from fastapi.testclient import TestClient

import app.main as app_main
from app.api.endpoints import errors as errors_module
from app.core.config import settings
from app.core.security import create_access_token
from app.models.user import User

pytestmark = pytest.mark.api

BOOM_PATH = "/api/v1/errors/health"
ALLOWED_ORIGIN = settings.cors_origins_list[0]


@pytest.fixture
def server_error_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A client that receives the 500 response instead of re-raising the exception."""

    def _boom(*_args, **_kwargs):
        raise RuntimeError("secret internal detail that must not reach the client")

    monkeypatch.setattr(errors_module, "to_utc_iso", _boom)
    yield TestClient(app_main.app, raise_server_exceptions=False)


class _SentrySpy:
    def __init__(self) -> None:
        self.tags: dict[str, str] = {}
        self.captured: list[tuple[BaseException, dict]] = []

    def set_tag(self, key, value) -> None:
        self.tags[key] = value

    def capture_exception(self, exc, **scope_kwargs) -> None:
        self.captured.append((exc, scope_kwargs))


@pytest.fixture
def sentry_spy(monkeypatch: pytest.MonkeyPatch) -> _SentrySpy:
    """Pretend Sentry is configured and record what would be sent (no network, no init)."""
    import sentry_sdk

    spy = _SentrySpy()
    monkeypatch.setattr(settings, "SENTRY_DSN", "https://public@sentry.invalid/1")
    monkeypatch.setattr(sentry_sdk, "set_tag", spy.set_tag)
    monkeypatch.setattr(sentry_sdk, "capture_exception", spy.capture_exception)
    return spy


# ---------------------------------------------------------------------------
# The 500 carries a quotable reference.
# ---------------------------------------------------------------------------


def test_500_body_and_header_carry_the_requests_correlation_id(server_error_client: TestClient):
    response = server_error_client.get(BOOM_PATH)

    assert response.status_code == 500
    body = response.json()
    correlation_id = body["correlation_id"]
    assert correlation_id
    assert response.headers["X-Correlation-ID"] == correlation_id
    # In ``detail`` too: the SPA already displays a server ``detail`` verbatim.
    assert body["detail"] == f"Internal server error (reference: {correlation_id})"
    # Nothing about the exception itself leaks.
    assert "secret internal detail" not in response.text
    assert "RuntimeError" not in response.text
    assert set(body) == {"detail", "correlation_id"}


def test_500_quotes_the_id_the_middleware_assigned_not_a_fresh_one(server_error_client: TestClient):
    """The quoted reference must be the SAME id the request's log lines carry. A caller-
    supplied X-Correlation-ID is what CorrelationIdMiddleware adopts, so echoing it back
    proves the handler read the middleware's id rather than minting its own."""
    response = server_error_client.get(BOOM_PATH, headers={"X-Correlation-ID": "trace-abc123"})

    assert response.status_code == 500
    assert response.json()["correlation_id"] == "trace-abc123"
    assert response.headers["X-Correlation-ID"] == "trace-abc123"


def test_500_log_line_carries_the_same_reference(server_error_client: TestClient, caplog: pytest.LogCaptureFixture):
    with caplog.at_level(logging.ERROR, logger="app.main"):
        response = server_error_client.get(BOOM_PATH, headers={"X-Correlation-ID": "trace-log-1"})

    assert response.status_code == 500
    records = [r for r in caplog.records if r.name == "app.main" and "Unhandled exception" in r.getMessage()]
    assert len(records) == 1
    assert "correlation_id=trace-log-1" in records[0].getMessage()
    assert records[0].exc_info is not None
    assert records[0].extra_data == {"correlation_id": "trace-log-1"}


def test_500_reference_is_readable_cross_origin(server_error_client: TestClient):
    """The 500 never passes back through CORSMiddleware, so the handler's own CORS
    headers must expose X-Correlation-ID or the browser hides it from the SPA."""
    response = server_error_client.get(BOOM_PATH, headers={"Origin": ALLOWED_ORIGIN})

    assert response.status_code == 500
    assert response.headers["Access-Control-Allow-Origin"] == ALLOWED_ORIGIN
    exposed = {h.strip().lower() for h in response.headers["Access-Control-Expose-Headers"].split(",")}
    assert "x-correlation-id" in exposed


def test_500_reports_to_sentry_tagged_with_the_reference(server_error_client: TestClient, sentry_spy: _SentrySpy):
    response = server_error_client.get(BOOM_PATH, headers={"X-Correlation-ID": "trace-sentry-1"})

    assert response.status_code == 500
    assert sentry_spy.tags.get("correlation_id") == "trace-sentry-1"  # set by the middleware
    assert len(sentry_spy.captured) == 1
    exc, scope_kwargs = sentry_spy.captured[0]
    assert isinstance(exc, RuntimeError)
    assert scope_kwargs["tags"] == {"correlation_id": "trace-sentry-1"}


# ---------------------------------------------------------------------------
# CORS: the headers the SPA needs are exposed on ordinary responses too.
# ---------------------------------------------------------------------------


def test_cors_exposes_correlation_id_content_disposition_and_etag(client: TestClient):
    response = client.get("/api/v1/errors/health", headers={"Origin": ALLOWED_ORIGIN})

    assert response.status_code == 200
    assert response.headers["Access-Control-Allow-Origin"] == ALLOWED_ORIGIN
    exposed = {h.strip().lower() for h in response.headers["Access-Control-Expose-Headers"].split(",")}
    assert {"x-correlation-id", "content-disposition", "etag"} <= exposed
    # And the header being exposed is actually there to read.
    assert response.headers["X-Correlation-ID"]


def test_cors_expose_list_is_not_widened_to_a_disallowed_origin(client: TestClient):
    response = client.get("/api/v1/errors/health", headers={"Origin": "https://evil.example"})

    assert response.status_code == 200
    assert "Access-Control-Allow-Origin" not in response.headers


# ---------------------------------------------------------------------------
# Sentry request tags: who, in which active company. Only when Sentry is configured.
# ---------------------------------------------------------------------------


def test_authenticated_request_tags_sentry_with_company_user_and_role(
    client: TestClient, admin_user: User, sentry_spy: _SentrySpy
):
    token = create_access_token(subject=admin_user.id, company_id=admin_user.company_id)

    response = client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200, response.text
    assert sentry_spy.tags["company_id"] == str(admin_user.company_id)
    assert sentry_spy.tags["user_id"] == str(admin_user.id)
    assert sentry_spy.tags["role"] == admin_user.role.value
    assert sentry_spy.tags["correlation_id"]
    # Ids and role only -- no name or email.
    assert admin_user.email not in sentry_spy.tags.values()


def test_no_sentry_tags_without_a_dsn(client: TestClient, admin_user: User, monkeypatch: pytest.MonkeyPatch):
    import sentry_sdk

    calls: list = []
    monkeypatch.setattr(settings, "SENTRY_DSN", "")
    monkeypatch.setattr(sentry_sdk, "set_tag", lambda *a, **k: calls.append(a))
    token = create_access_token(subject=admin_user.id, company_id=admin_user.company_id)

    response = client.get("/api/v1/users/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200, response.text
    assert calls == []
