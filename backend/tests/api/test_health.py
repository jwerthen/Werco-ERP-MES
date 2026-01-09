"""
Integration tests for health check endpoints.
Tests liveness, readiness, and detailed health checks.
"""
import pytest
from fastapi import status
from fastapi.testclient import TestClient


@pytest.mark.api
class TestHealthEndpoints:
    """Test health check endpoints."""

    def test_basic_health_check(self, client: TestClient):
        """Test basic health endpoint returns healthy status."""
        response = client.get("/health")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["status"] == "healthy"
        assert "app" in data
        assert "environment" in data

    def test_liveness_probe(self, client: TestClient):
        """Test liveness probe endpoint."""
        response = client.get("/health/live")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["status"] == "alive"
        assert "timestamp" in data

    def test_readiness_probe(self, client: TestClient):
        """Test readiness probe with database check."""
        response = client.get("/health/ready")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["status"] == "healthy"
        assert "checks" in data
        assert "database" in data["checks"]
        assert data["checks"]["database"]["status"] == "healthy"

    def test_readiness_includes_latency(self, client: TestClient):
        """Test readiness check includes database latency."""
        response = client.get("/health/ready")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        db_check = data["checks"]["database"]
        assert "latency_ms" in db_check
        assert isinstance(db_check["latency_ms"], (int, float))

    def test_detailed_health_check(self, client: TestClient):
        """Test detailed health endpoint with system info."""
        response = client.get("/health/detailed")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        
        # Check structure
        assert "status" in data
        assert "timestamp" in data
        assert "checks" in data
        
        # Check database info
        assert "database" in data["checks"]
        
        # Check system info
        assert "system" in data["checks"]
        assert "python_version" in data["checks"]["system"]
        
        # Check application info
        assert "application" in data["checks"]
        assert "name" in data["checks"]["application"]
        assert "environment" in data["checks"]["application"]
        
        # Check features
        assert "features" in data["checks"]
        assert "rate_limiting" in data["checks"]["features"]

    def test_health_no_auth_required(self, client: TestClient):
        """Test health endpoints don't require authentication."""
        endpoints = ["/health", "/health/live", "/health/ready", "/health/detailed"]
        for endpoint in endpoints:
            response = client.get(endpoint)
            assert response.status_code == status.HTTP_200_OK, f"Failed for {endpoint}"


@pytest.mark.api
class TestHealthCheckFormat:
    """Test health check response formats."""

    def test_health_response_json(self, client: TestClient):
        """Test health endpoints return valid JSON."""
        response = client.get("/health")
        assert response.headers["content-type"].startswith("application/json")

    def test_ready_timestamp_format(self, client: TestClient):
        """Test readiness timestamp is ISO format."""
        response = client.get("/health/ready")
        data = response.json()
        timestamp = data["timestamp"]
        # Should be ISO format: 2026-01-09T12:00:00.000000
        assert "T" in timestamp
        assert len(timestamp) > 10
