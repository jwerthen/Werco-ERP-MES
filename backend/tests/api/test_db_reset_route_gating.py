"""``POST /api/v1/auth/reset-database`` must not exist in production.

The handler TRUNCATEs every table in the public schema with FK triggers
disabled. It has no auth dependency: the only credential is an ``X-Reset-Key``
header compared against ``SECRET_KEY`` — which is also the JWT signing key, so
the byte-by-byte ``!=`` it used made the endpoint a timing oracle for total
token forgery as well as for total data destruction.

Two changes are pinned here:

* the route is only MOUNTED when ``settings.ENVIRONMENT != "production"``, so on
  a production host it is not merely disabled but absent — it 404s like any
  unknown path and never appears in the OpenAPI schema (a runtime check inside
  the handler would still leave it enumerable);
* the comparison uses ``hmac.compare_digest``.

Deleting the endpoint outright is the recommended end state and remains an
owner decision; these tests are the floor, not the ceiling.
"""

import ast
import importlib.util
import inspect
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api.endpoints import auth as auth_module

ENDPOINT = "/api/v1/auth/reset-database"
ROUTE_PATH = "/reset-database"

_ALLOW_DB_RESET = os.environ.get("ALLOW_DB_RESET", "false").lower() == "true"


def _load_auth_module_for_environment(monkeypatch: pytest.MonkeyPatch, environment: str):
    """Execute a fresh, throwaway copy of the auth endpoint module under ``environment``.

    Loaded under a private name and never inserted into ``sys.modules``, so the
    running app (which already holds the real router) is untouched. This
    exercises the actual registration decision rather than a paraphrase of it.
    """
    from app.core.config import settings

    monkeypatch.setattr(settings, "ENVIRONMENT", environment)

    source_path = Path(auth_module.__file__)
    spec = importlib.util.spec_from_file_location(f"_auth_route_probe_{environment}", source_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _route_paths(module) -> set:
    return {route.path for route in module.router.routes}


def test_reset_database_route_is_absent_in_production(monkeypatch: pytest.MonkeyPatch):
    """The destructive route is never mounted on a production host."""
    module = _load_auth_module_for_environment(monkeypatch, "production")
    paths = _route_paths(module)

    # Sanity: the module really did build its router, so the assertion below is
    # about the gate and not about an empty/failed load.
    assert "/login" in paths
    assert ROUTE_PATH not in paths


@pytest.mark.parametrize("environment", ["development", "test", "staging"])
def test_reset_database_route_is_present_outside_production(monkeypatch: pytest.MonkeyPatch, environment: str):
    """Non-production environments keep the route — this is a gate, not a deletion."""
    module = _load_auth_module_for_environment(monkeypatch, environment)

    assert ROUTE_PATH in _route_paths(module)


def test_db_reset_route_enabled_predicate():
    """The decision is a pure function, testable without booting the app."""
    assert auth_module.db_reset_route_enabled("production") is False
    assert auth_module.db_reset_route_enabled("development") is True
    assert auth_module.db_reset_route_enabled("test") is True
    assert auth_module.db_reset_route_enabled("staging") is True


@pytest.mark.parametrize(
    "environment",
    ["Production", "PRODUCTION", " production", "production ", " Production ", "\tproduction\n"],
)
def test_db_reset_route_enabled_does_not_fail_open_on_casing_or_whitespace(environment: str):
    """REGRESSION: the gate must not fail open on a dashboard-typed variant.

    ENVIRONMENT is a free-form string pasted into a deploy dashboard. A bare
    ``!= "production"`` would treat "Production", or a stray trailing space, as
    non-production and MOUNT the no-auth TRUNCATE-every-table route on the
    production host — silently, and maximally destructively.
    """
    assert auth_module.db_reset_route_enabled(environment) is False


@pytest.mark.parametrize("environment", ["", None])
def test_db_reset_route_enabled_tolerates_unset_environment(environment):
    """An unset ENVIRONMENT is not production — mount it, but never crash."""
    assert auth_module.db_reset_route_enabled(environment) is True


def test_reset_database_route_is_absent_for_a_casing_variant_of_production(monkeypatch: pytest.MonkeyPatch):
    """End-to-end companion to the predicate test: "Production" also unmounts."""
    module = _load_auth_module_for_environment(monkeypatch, "Production")

    assert "/login" in _route_paths(module)
    assert ROUTE_PATH not in _route_paths(module)


@pytest.mark.skipif(_ALLOW_DB_RESET, reason="ALLOW_DB_RESET=true in this environment; refusing to probe a live reset")
@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({"X-Reset-Key": "not-the-secret-key"}, id="wrong-key"),
        pytest.param({}, id="missing-key"),
        pytest.param({"X-Reset-Key": ""}, id="empty-key"),
        pytest.param({"X-Reset-Key": os.environ.get("SECRET_KEY", "")}, id="CORRECT-key"),
    ],
)
def test_disarmed_host_gives_one_identical_answer_to_every_key(client: TestClient, headers: dict):
    """NO ORACLE: on a disarmed host, a correct key is indistinguishable from a wrong one.

    The arm check runs BEFORE the key comparison for exactly this reason. Under
    the old ordering a wrong key answered "Invalid reset key" while a correct one
    answered "Database reset is disabled" — which confirms a candidate
    SECRET_KEY (also the JWT signing key) against a host where the reset is not
    even armed. Sending the correct key here is safe precisely because the arm
    gate refuses first: nothing is truncated.
    """
    response = client.post(ENDPOINT, headers=headers)

    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "Database reset is disabled"


def test_armed_host_still_rejects_a_wrong_key(client: TestClient, monkeypatch: pytest.MonkeyPatch):
    """With the reset ARMED, the key is the control and a wrong one is 403.

    Deliberately never sends the correct key: armed + correct key is the one
    combination that truncates.
    """
    monkeypatch.setenv("ALLOW_DB_RESET", "true")

    for headers in ({"X-Reset-Key": "not-the-secret-key"}, {}, {"X-Reset-Key": ""}):
        response = client.post(ENDPOINT, headers=headers)
        assert response.status_code == 403, response.text
        assert response.json()["detail"] == "Invalid reset key", headers


def test_reset_key_comparison_is_constant_time():
    """GUARD: the key check must be ``hmac.compare_digest``, never ``==``/``!=``.

    Asserted structurally over the handler's AST rather than its text, because
    the docstring legitimately discusses the ``!=`` that used to be there.
    """
    tree = ast.parse(inspect.getsource(auth_module.reset_database))

    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "compare_digest"
    ]
    assert calls, "reset_database must compare the reset key with hmac.compare_digest"

    compared_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Compare) and any(isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops):
            for operand in [node.left, *node.comparators]:
                if isinstance(operand, ast.Name):
                    compared_names.add(operand.id)

    assert not compared_names & {"provided_key", "actual_key"}, (
        "the reset key must not be compared with == / != — that is a timing "
        "oracle for SECRET_KEY, which is also the JWT signing key"
    )
