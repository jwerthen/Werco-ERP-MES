"""
Tests for security headers and middleware behavior.
"""

import pytest
from fastapi.testclient import TestClient


@pytest.mark.api
class TestSecurityHeaders:
    def test_health_includes_csp(self, client: TestClient):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.headers.get("Content-Security-Policy") == "default-src 'self'; frame-ancestors 'none'"

    def test_docs_skip_csp_and_return_html(self, client: TestClient):
        response = client.get("/api/docs")
        assert response.status_code == 200
        assert response.headers.get("Content-Security-Policy") is None
        assert response.headers.get("content-type", "").startswith("text/html")

    def test_redoc_skip_csp_and_return_html(self, client: TestClient):
        response = client.get("/api/redoc")
        assert response.status_code == 200
        assert response.headers.get("Content-Security-Policy") is None
        assert response.headers.get("content-type", "").startswith("text/html")


@pytest.mark.api
class TestRateLimitMiddleware:
    def test_slowapi_middleware_registered(self, client: TestClient):
        middleware_names = {mw.cls.__name__ for mw in client.app.user_middleware}
        assert "SlowAPIMiddleware" in middleware_names


@pytest.mark.api
class TestHostHeaderHandling:
    """Regression coverage for CVE-2026-48710 (Starlette).

    The CVE was a missing Host-header validation that let a malicious ``Host``
    header poison ``request.url.path`` (path-based security bypass). This app's
    own middleware keys security decisions off ``request.url.path`` -- the CSRF
    exemption check and the rate-limit/logging path matching in ``app/main.py``
    all read ``request.url.path`` -- so a path poisoned by the Host header would
    be a real bypass here, not just a Starlette-internal concern.

    These tests pin the fixed behavior: the request-target path is derived from
    the ASGI ``path`` scope and is never influenced by the ``Host`` header.
    """

    MALICIOUS_HOSTS = [
        "evil.com",
        "evil.com/../../admin",
        "evil.com\r\nX-Injected: 1",
        "http://evil.com/poison",
        "localhost:1/../secret",
    ]

    @pytest.mark.parametrize("malicious_host", MALICIOUS_HOSTS)
    def test_host_header_does_not_poison_url_path(self, client: TestClient, malicious_host: str):
        """A spoofed Host header must not alter the resolved request path.

        ``/health`` is CSRF-exempt and CSP-tagged based on ``request.url.path``;
        if the Host header could change the path, the request would either miss
        the exemption (403) or skip the CSP tagging. Both stay intact here.
        """
        response = client.get("/health", headers={"host": malicious_host})

        # Path-keyed middleware behavior is unchanged: still 200, still CSP-tagged,
        # i.e. request.url.path resolved to "/health" regardless of the Host header.
        assert response.status_code == 200
        assert response.headers.get("Content-Security-Policy") == "default-src 'self'; frame-ancestors 'none'"
        assert response.json().get("status") == "healthy"

    def test_baseline_health_ok(self, client: TestClient):
        """Sanity check that the probe endpoint behaves with a normal Host header."""
        response = client.get("/health")
        assert response.status_code == 200
        assert response.headers.get("Content-Security-Policy") == "default-src 'self'; frame-ancestors 'none'"

    def test_trusted_host_middleware_registered(self, client: TestClient):
        """TrustedHostMiddleware is now wired into the app as defense-in-depth for
        Host-header attacks (flipped from the prior 'not yet configured' guard)."""
        middleware_names = {mw.cls.__name__ for mw in client.app.user_middleware}
        assert "TrustedHostMiddleware" in middleware_names

    def test_trusted_host_wired_from_settings_and_outermost(self, client: TestClient):
        """The allowlist comes from settings.allowed_hosts_list (not a hardcoded list),
        www_redirect is disabled, and the middleware is outermost so an unrecognized
        Host is rejected before any other middleware or route runs."""
        from app.core.config import settings

        thm = [mw for mw in client.app.user_middleware if mw.cls.__name__ == "TrustedHostMiddleware"]
        assert len(thm) == 1
        assert thm[0].kwargs["allowed_hosts"] == settings.allowed_hosts_list
        assert thm[0].kwargs["www_redirect"] is False
        # Starlette's add_middleware inserts at index 0 and TrustedHost is added last,
        # so it must be the outermost (first-executed) middleware. If this fails, a
        # middleware was registered AFTER TrustedHost in main.py — move it before, or
        # TrustedHost stops being the first to reject a bad Host.
        assert (
            client.app.user_middleware[0].cls.__name__ == "TrustedHostMiddleware"
        ), "TrustedHostMiddleware must be registered LAST in main.py so it stays outermost"


@pytest.mark.api
class TestTrustedHostRejection:
    """Contract coverage for TrustedHostMiddleware: a Host not in the allowlist is
    rejected with HTTP 400 before reaching the app. Exercised on a small purpose-built
    app because the module-level production app defaults to ALLOWED_HOSTS='*' --
    enforcement is intentionally disabled in dev, so the real app cannot itself
    demonstrate a rejection."""

    @staticmethod
    def _client(allowed_hosts):
        from fastapi import FastAPI
        from starlette.middleware.trustedhost import TrustedHostMiddleware

        app = FastAPI()
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts, www_redirect=False)

        @app.get("/ping")
        def ping():
            return {"ok": True}

        return TestClient(app)

    def test_allowed_hosts_pass(self):
        client = self._client(["testserver", "api.werco.com"])
        # TestClient's default Host header is 'testserver'.
        assert client.get("/ping").status_code == 200
        assert client.get("/ping", headers={"host": "api.werco.com"}).status_code == 200

    def test_allowed_host_with_port_passes(self):
        client = self._client(["api.werco.com"])
        assert client.get("/ping", headers={"host": "api.werco.com:8443"}).status_code == 200

    def test_disallowed_host_rejected_with_400(self):
        client = self._client(["api.werco.com"])
        resp = client.get("/ping", headers={"host": "evil.com"})
        assert resp.status_code == 400
        assert "host" in resp.text.lower()

    def test_empty_host_rejected_with_400(self):
        client = self._client(["api.werco.com"])
        assert client.get("/ping", headers={"host": ""}).status_code == 400

    def test_wildcard_subdomain_scope(self):
        client = self._client(["*.werco.com"])
        assert client.get("/ping", headers={"host": "api.werco.com"}).status_code == 200
        # A wildcard matches subdomains only, not the apex.
        assert client.get("/ping", headers={"host": "werco.com"}).status_code == 400
        assert client.get("/ping", headers={"host": "evil.com"}).status_code == 400

    def test_star_allows_any_host(self):
        client = self._client(["*"])
        assert client.get("/ping", headers={"host": "literally-anything.example"}).status_code == 200

    def test_host_match_is_case_sensitive(self):
        # Starlette matches the Host header case-sensitively.
        client = self._client(["api.werco.com"])
        assert client.get("/ping", headers={"host": "API.WERCO.COM"}).status_code == 400

    def test_malicious_host_rejected_when_restricted(self):
        # Connects the CVE-2026-48710 regression (Host can't poison request.url.path) to the
        # new enforcement layer: under a real allowlist the spoofed host is rejected outright.
        client = self._client(["api.werco.com"])
        assert client.get("/ping", headers={"host": "evil.com"}).status_code == 400

    def test_railway_healthcheck_host_must_be_allowlisted(self):
        # Operational trap (see docs/ENVIRONMENT_VARIABLES.md): a public-domain-only allowlist
        # 400s Railway's probe (Host: healthcheck.railway.app) and fails the deploy; listing
        # the probe host fixes it. '*.up.railway.app' does NOT cover healthcheck.railway.app.
        public_only = self._client(["api.werco.com", "erp.werco.com"])
        assert public_only.get("/ping", headers={"host": "healthcheck.railway.app"}).status_code == 400
        with_probe = self._client(["api.werco.com", "healthcheck.railway.app"])
        assert with_probe.get("/ping", headers={"host": "healthcheck.railway.app"}).status_code == 200


@pytest.mark.unit
class TestHostValidationLogRecord:
    """The startup log must report DISABLED whenever ALLOWED_HOSTS contains '*' (allow-any),
    even outside production, and WARN in production. Pure-function test so it needs no app
    boot (and dodges configure_logging() stripping caplog's handler)."""

    def test_production_wildcard_warns_disabled(self):
        import logging as stdlogging

        from app.main import _host_validation_log_record

        level, msg = _host_validation_log_record("production", ["*"])
        assert level == stdlogging.WARNING
        assert "DISABLED" in msg

    def test_development_wildcard_reports_disabled_not_enabled(self):
        import logging as stdlogging

        from app.main import _host_validation_log_record

        level, msg = _host_validation_log_record("development", ["*"])
        assert level == stdlogging.INFO
        assert "disabled" in msg.lower()
        assert "enabled" not in msg.lower()

    def test_explicit_hosts_report_enabled(self):
        import logging as stdlogging

        from app.main import _host_validation_log_record

        level, msg = _host_validation_log_record("production", ["api.werco.com"])
        assert level == stdlogging.INFO
        assert "enabled" in msg.lower()

    def test_wildcard_mixed_with_hosts_still_disabled(self):
        import logging as stdlogging

        from app.main import _host_validation_log_record

        # '*' anywhere = allow-any, so it must report disabled, not enabled.
        level, msg = _host_validation_log_record("production", ["api.werco.com", "*"])
        assert level == stdlogging.WARNING
        assert "DISABLED" in msg
