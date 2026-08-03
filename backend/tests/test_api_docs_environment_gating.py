"""The interactive API docs must not be served in production.

``app/main.py`` derives ``docs_url`` / ``redoc_url`` / ``openapi_url`` from
``settings.ENVIRONMENT``. All three matter: disabling only the two UIs would
leave ``/api/openapi.json`` handing out the full endpoint inventory, request
schemas and auth requirements to anyone who asks.

Because those URLs are resolved once, at import time, the production case is
checked in a SUBPROCESS with a valid production environment rather than by
reloading ``app.main`` in place -- a reload would rebind the module-global
``app`` that every other test's TestClient is holding.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.core.config import settings
from app.main import _DOCS_PATHS, app

pytestmark = pytest.mark.unit

BACKEND_ROOT = Path(__file__).resolve().parents[1]

DOCS_PATHS = ("/api/docs", "/api/redoc", "/api/openapi.json")

# A minimally valid production config: Settings.validate_production_settings
# requires DEBUG off, CORS set without localhost, and a Supabase database URL.
# Same recipe as tests/test_config.py's production cases. No connection is
# opened -- importing app.main only constructs the engine.
PRODUCTION_ENV = {
    "ENVIRONMENT": "production",
    "DEBUG": "false",
    "CORS_ORIGINS": "https://erp.example.com",
    "DATABASE_URL": "postgresql://postgres:secret@db.example.supabase.co:5432/postgres",
    "SECRET_KEY": "a" * 64,
    "REFRESH_TOKEN_SECRET_KEY": "b" * 64,
}

_SENTINEL = "__DOCS_PROBE__"

_PROBE = f"""
import json
from app.main import app, _DOCS_PATHS
print("{_SENTINEL}" + json.dumps({{
    "environment": __import__("app.core.config", fromlist=["settings"]).settings.ENVIRONMENT,
    "docs_url": app.docs_url,
    "redoc_url": app.redoc_url,
    "openapi_url": app.openapi_url,
    "route_paths": sorted({{getattr(r, "path", "") for r in app.routes}}),
    "csp_exempt_paths": list(_DOCS_PATHS),
}}))
"""


@pytest.fixture(scope="module")
def production_app() -> dict:
    """Import the app with a production environment and report its docs wiring.

    Module-scoped: the import costs a few seconds, and nothing in it varies
    between the assertions below.
    """
    env = {**os.environ, **PRODUCTION_ENV}
    # Drop the harness overrides (conftest points DATABASE_URL at SQLite) that
    # would fight the production values.
    for key in ("TEST_DATABASE_URL", "ALLOW_NON_SUPABASE_DATABASE"):
        env.pop(key, None)

    completed = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert completed.returncode == 0, (
        f"Importing app.main with ENVIRONMENT=production failed:\n"
        f"--- stdout ---\n{completed.stdout}\n--- stderr ---\n{completed.stderr}"
    )
    payload = next((line for line in completed.stdout.splitlines() if line.startswith(_SENTINEL)), None)
    assert payload, f"Probe produced no result line. stdout:\n{completed.stdout}"
    return json.loads(payload[len(_SENTINEL) :])


class TestDocsDisabledInProduction:
    def test_production_serves_no_docs_or_schema(self, production_app: dict) -> None:
        assert production_app["environment"] == "production", "Probe did not actually run as production"
        assert production_app["docs_url"] is None
        assert production_app["redoc_url"] is None
        assert production_app["openapi_url"] is None, (
            "openapi_url is still set in production. The schema is the sensitive part -- it "
            "enumerates every endpoint, payload shape and auth requirement."
        )

    def test_production_registers_no_route_for_the_docs_paths(self, production_app: dict) -> None:
        # Belt and braces: FastAPI only registers the docs routes when the URLs
        # are set, so absent URLs must mean absent routes (a plain 404).
        leaked = [path for path in DOCS_PATHS if path in production_app["route_paths"]]
        assert not leaked, f"Production app still routes {leaked}"

    def test_production_applies_csp_everywhere(self, production_app: dict) -> None:
        # The security-headers middleware skips CSP for the docs paths (Swagger
        # needs inline styles). With docs off that exemption must vanish too,
        # or production keeps a CSP hole on paths that now 404.
        assert production_app["csp_exempt_paths"] == []


class TestDocsEnabledOutsideProduction:
    def test_current_non_production_environment_serves_docs(self) -> None:
        assert settings.ENVIRONMENT != "production", "Test suite must not run with ENVIRONMENT=production"

        assert app.docs_url == "/api/docs"
        assert app.redoc_url == "/api/redoc"
        assert app.openapi_url == "/api/openapi.json"

    def test_docs_paths_are_routed_and_csp_exempt_outside_production(self) -> None:
        route_paths = {getattr(route, "path", "") for route in app.routes}
        missing = [path for path in DOCS_PATHS if path not in route_paths]
        assert not missing, f"Non-production app should serve {missing}"
        assert set(_DOCS_PATHS) == set(DOCS_PATHS)

    def test_openapi_schema_is_reachable(self, client) -> None:
        response = client.get("/api/openapi.json")
        assert response.status_code == 200
        assert "paths" in response.json()
