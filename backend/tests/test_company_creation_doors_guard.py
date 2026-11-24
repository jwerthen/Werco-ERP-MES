"""Every door that can create a ``Company`` row is named here, with its guard.

Owner decision 2026-11-23: Werco is the only company on this system, so the
unauthenticated ``POST /api/v1/companies/register`` self-registration route was
deleted. This file is the tripwire that keeps it deleted in spirit, not just by path:

* a STATIC scan of ``app/`` pins every place that constructs a ``Company(...)`` or
  calls ``onboard_company(...)``. A new site fails CI until it is added below with a
  reason, so a second public company-creation door cannot appear quietly under a
  different path;
* a ROUTE scan pins which mounted routes reach those sites and what guards them:
  ``POST /platform/companies`` must depend on ``require_platform_admin``, and the one
  unauthenticated site, the first-user bootstrap in ``POST /auth/register-public``,
  must stay confined to an empty install (proved over HTTP below).
"""

import ast
from pathlib import Path

import pytest
from fastapi.routing import APIRoute

from app.api.deps import require_platform_admin
from app.core.security import get_password_hash
from app.main import app
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.user import User, UserRole

APP_DIR = Path(__file__).resolve().parents[1] / "app"

# (path relative to backend/app, enclosing function) -> why it is allowed.
EXPECTED_COMPANY_CONSTRUCTORS = {
    ("services/company_onboarding.py", "onboard_company"): "the one onboarding service",
    ("api/endpoints/auth.py", "register_public"): "empty-install bootstrap, zero users only",
}
EXPECTED_ONBOARD_CALLERS = {
    ("api/endpoints/platform.py", "create_company"): "POST /platform/companies, platform admin only",
}


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _sites(target: str) -> set[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for path in sorted(APP_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(APP_DIR).as_posix()

        def visit(node: ast.AST, enclosing: str) -> None:
            for child in ast.iter_child_nodes(node):
                name = enclosing
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    name = child.name
                if isinstance(child, ast.Call) and _call_name(child) == target:
                    found.add((rel, enclosing))
                visit(child, name)

        visit(tree, "<module>")
    return found


def _dependency_calls(dependant) -> set:
    calls = set()
    for dep in dependant.dependencies:
        if dep.call is not None:
            calls.add(dep.call)
        calls |= _dependency_calls(dep)
    return calls


def _routes_for(endpoint_name: str, module_suffix: str) -> list[APIRoute]:
    return [
        r
        for r in app.routes
        if isinstance(r, APIRoute)
        and r.endpoint.__name__ == endpoint_name
        and r.endpoint.__module__.endswith(module_suffix)
    ]


@pytest.mark.unit
def test_every_company_constructor_is_known():
    assert _sites("Company") == set(EXPECTED_COMPANY_CONSTRUCTORS)


@pytest.mark.unit
def test_every_onboard_company_caller_is_known():
    assert _sites("onboard_company") == set(EXPECTED_ONBOARD_CALLERS)


@pytest.mark.unit
def test_platform_company_create_is_the_only_route_onboarding_and_requires_platform_admin():
    routes = _routes_for("create_company", "endpoints.platform")
    assert [(r.path, sorted(r.methods)) for r in routes] == [("/api/v1/platform/companies", ["POST"])]
    assert require_platform_admin in _dependency_calls(routes[0].dependant)


@pytest.mark.unit
def test_no_route_serves_the_deleted_self_registration_path():
    served = {(r.path, m) for r in app.routes if isinstance(r, APIRoute) for m in r.methods}
    assert not any(path.endswith("/companies/register") for path, _ in served)


@pytest.mark.unit
def test_companies_module_docstring_names_only_served_company_creation_doors():
    """The companies module tells a reader where companies come from; every door it names must exist."""
    import re

    from app.api.endpoints import companies

    named = set(re.findall(r"``POST (/[^`]+)``", companies.__doc__ or ""))
    assert named == {"/platform/companies", "/auth/register-public"}
    served = {(r.path, m) for r in app.routes if isinstance(r, APIRoute) for m in r.methods}
    for path in named:
        assert (f"/api/v1{path}", "POST") in served, path


@pytest.mark.unit
def test_register_public_is_the_only_unauthenticated_company_constructor():
    routes = _routes_for("register_public", "endpoints.auth")
    assert [(r.path, sorted(r.methods)) for r in routes] == [("/api/v1/auth/register-public", ["POST"])]


def _public_body(n: int) -> dict:
    return {
        "email": f"bootstrap-probe-{n}@example.com",
        "first_name": "Probe",
        "last_name": "Person",
        "password": "Zephyr9!Quill-Test",
    }


@pytest.mark.api
def test_public_registration_with_any_existing_user_creates_no_company(client, db_session):
    db_session.add(
        User(
            company_id=1,
            email="existing@example.com",
            employee_id="EXISTING-1",
            first_name="Existing",
            last_name="Person",
            role=UserRole.ADMIN,
            is_active=True,
            hashed_password=get_password_hash("Zephyr9!Quill-Test"),
        )
    )
    db_session.commit()
    companies_before = db_session.query(Company).count()

    response = client.post("/api/v1/auth/register-public", json=_public_body(1))

    assert response.status_code == 200, response.text
    assert response.json()["is_first_user"] is False
    db_session.expire_all()
    assert db_session.query(Company).count() == companies_before
    user = db_session.query(User).filter_by(email="bootstrap-probe-1@example.com").one()
    assert user.company_id == 1
    assert user.role == UserRole.VIEWER and user.is_active is False and user.is_superuser is False


@pytest.mark.api
def test_empty_install_bootstrap_reuses_the_werco_company_and_creates_no_second_one(client, db_session):
    # conftest seeds the Werco company (id 1, slug "werco") and no users.
    assert db_session.query(User).count() == 0
    companies_before = db_session.query(Company).count()

    first = client.post("/api/v1/auth/register-public", json=_public_body(2))
    second = client.post("/api/v1/auth/register-public", json=_public_body(3))

    assert first.status_code == 200, first.text
    assert first.json()["is_first_user"] is True
    assert second.status_code == 200, second.text
    assert second.json()["is_first_user"] is False
    db_session.expire_all()
    assert db_session.query(Company).count() == companies_before
    assert {u.company_id for u in db_session.query(User).all()} == {1}
    assert db_session.query(AuditLog).filter(AuditLog.company_id != 1).count() == 0
