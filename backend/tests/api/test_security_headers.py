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
