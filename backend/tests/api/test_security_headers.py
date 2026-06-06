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

    def test_trusted_host_middleware_not_yet_configured(self, client: TestClient):
        """Document the remaining gap as a follow-up, not a failure.

        Starlette 1.2.1 stops the Host header from poisoning ``request.url.path``,
        which closes CVE-2026-48710. Defense-in-depth -- outright *rejecting*
        unexpected Host/authority values -- still requires
        ``TrustedHostMiddleware`` with an explicit ``allowed_hosts`` list, which
        this app does not yet register. This test records that state; flip the
        assertion to ``in`` when the middleware is added (see app/main.py).
        """
        middleware_names = {mw.cls.__name__ for mw in client.app.user_middleware}
        assert "TrustedHostMiddleware" not in middleware_names
