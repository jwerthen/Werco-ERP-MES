"""Every door that can create a ``Company`` row is named here, with its guard.

Owner decision 2026-11-23: Werco is the only company on this system, so BOTH company
creation routes were deleted -- the unauthenticated ``POST /api/v1/companies/register``
self-registration route and then the platform-admin ``POST /api/v1/platform/companies``
("Delete it too"), together with the ``company_onboarding`` service only they called.
This file is the tripwire that keeps them deleted in spirit, not just by path:

* a STATIC scan of ``app/`` pins every place that constructs a ``Company(...)``. The one
  site left is the first-user bootstrap in ``POST /auth/register-public``; a new site
  fails CI until it is added below with a reason, so a company-creation door cannot
  reappear quietly under a different path;
* a second STATIC scan reads every write-route decorator in ``app/`` (with its mount
  prefix from ``app/api/router.py``) and fails on any route, other than the bootstrap,
  that constructs a ``Company`` or is shaped or named like a company-creation door --
  catching one whose construction hides in a helper;
* a ROUTE scan pins that neither deleted route is mounted, and the bootstrap is proved
  over HTTP to stay confined to an empty install.
"""

import ast
import importlib.util
import re
from pathlib import Path

import pytest
from fastapi.routing import APIRoute

from app.core.security import get_password_hash
from app.main import app
from app.models.audit_log import AuditLog
from app.models.company import Company
from app.models.user import User, UserRole

APP_DIR = Path(__file__).resolve().parents[1] / "app"

# (path relative to backend/app, enclosing function) -> why it is allowed.
EXPECTED_COMPANY_CONSTRUCTORS = {
    ("api/endpoints/auth.py", "register_public"): "empty-install bootstrap, zero users only",
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
def test_the_onboarding_service_is_deleted_and_nothing_calls_it():
    assert not (APP_DIR / "services" / "company_onboarding.py").exists()
    assert importlib.util.find_spec("app.services.company_onboarding") is None
    assert _sites("onboard_company") == set()


@pytest.mark.unit
def test_platform_company_create_route_is_deleted():
    """``GET /platform/companies`` (the list) stays; nothing answers ``POST`` there any more."""
    served = {(r.path, m) for r in app.routes if isinstance(r, APIRoute) for m in r.methods}
    assert ("/api/v1/platform/companies", "GET") in served
    assert ("/api/v1/platform/companies", "POST") not in served
    assert _routes_for("create_company", "endpoints.platform") == []
    assert "post" not in app.openapi()["paths"]["/api/v1/platform/companies"]


@pytest.mark.unit
def test_company_creation_request_schemas_are_deleted():
    """Neither deleted route's request body survives as a schema a new route could reuse."""
    import app.schemas.company as company_schemas

    assert not hasattr(company_schemas, "CompanyRegister")
    assert not hasattr(company_schemas, "CompanyCreate")


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
    assert named == {"/auth/register-public"}
    served = {(r.path, m) for r in app.routes if isinstance(r, APIRoute) for m in r.methods}
    for path in named:
        assert (f"/api/v1{path}", "POST") in served, path


@pytest.mark.unit
def test_register_public_is_the_only_unauthenticated_company_constructor():
    routes = _routes_for("register_public", "endpoints.auth")
    assert [(r.path, sorted(r.methods)) for r in routes] == [("/api/v1/auth/register-public", ["POST"])]


# --- Static scan of route DEFINITIONS ---------------------------------------------------
#
# The constructor scan above catches a route that builds a ``Company`` itself. This one
# catches the shape of a company-creation route even when the construction is hidden in
# a helper or a service: a POST whose mounted path is a company/tenant collection or a
# register/create/onboard-company verb, or whose handler is NAMED like one.

# (path relative to backend/app, handler name) -> why it may construct a Company.
ALLOWED_COMPANY_CREATING_ROUTES = {
    ("api/endpoints/auth.py", "register_public"): (
        "the empty-install bootstrap: builds the one Werco company only when the users table is empty"
    ),
}

_ROUTE_METHODS = {"post", "put", "patch", "api_route"}
_CREATION_PATH = re.compile(
    r"/(compan(y|ies)|tenants?|organi[sz]ations?)/?$"
    r"|/(compan(y|ies)|tenants?|organi[sz]ations?)/(register|create|new|onboard|signup|provision)\b"
    r"|/(register|create|new|onboard|signup|provision)[-_](compan(y|ies)|tenants?|organi[sz]ations?)\b",
    re.IGNORECASE,
)
_CREATION_NAME = re.compile(
    r"(create|register|onboard|provision|signup|add|new)_(a_|new_)?(compan(y|ies)|tenants?|organi[sz]ations?)",
    re.IGNORECASE,
)


def _router_prefixes() -> dict[str, str]:
    """endpoint module name -> mount prefix, read from ``app/api/router.py`` with ast."""
    tree = ast.parse((APP_DIR / "api" / "router.py").read_text(encoding="utf-8"))
    prefixes: dict[str, str] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _call_name(node) == "include_router" and node.args):
            continue
        target = node.args[0]
        if not (isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name)):
            continue
        prefix = next(
            (
                kw.value.value
                for kw in node.keywords
                if kw.arg == "prefix" and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str)
            ),
            "",
        )
        prefixes[target.value.id] = prefix
    return prefixes


def _decorated_routes() -> list[tuple[str, str, str, str, bool]]:
    """Every write-verb route decorator in ``app/``: (file, handler, verb, mounted path, builds Company)."""
    prefixes = _router_prefixes()
    routes = []
    for path in sorted(APP_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(APP_DIR).as_posix()
        prefix = prefixes.get(path.stem, "") if rel.startswith("api/endpoints/") else ""
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            builds_company = any(isinstance(n, ast.Call) and _call_name(n) == "Company" for n in ast.walk(fn))
            for dec in fn.decorator_list:
                if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
                    continue
                verb = dec.func.attr
                if verb not in _ROUTE_METHODS:
                    continue
                if verb == "api_route":
                    methods = next((kw.value for kw in dec.keywords if kw.arg == "methods"), None)
                    named = (
                        {e.value.lower() for e in methods.elts if isinstance(e, ast.Constant)}
                        if isinstance(methods, (ast.List, ast.Tuple, ast.Set))
                        else {"post"}  # unreadable methods list: assume the worst
                    )
                    if not named & {"post", "put", "patch"}:
                        continue
                route_path = (
                    dec.args[0].value
                    if dec.args and isinstance(dec.args[0], ast.Constant) and isinstance(dec.args[0].value, str)
                    else ""
                )
                routes.append((rel, fn.name, verb, prefix + route_path, builds_company))
    return routes


@pytest.mark.unit
def test_the_route_definition_scan_sees_the_routes_it_guards():
    """Pin the scanner itself, so a parsing change cannot make the next test vacuous."""
    routes = _decorated_routes()
    assert len(routes) > 100
    assert ("api/endpoints/auth.py", "register_public", "post", "/auth/register-public", True) in routes
    assert any(r[0] == "api/endpoints/platform.py" and r[3] == "/platform/companies/{company_id}" for r in routes)
    assert _CREATION_PATH.search("/platform/companies")
    assert _CREATION_PATH.search("/companies/register")
    assert _CREATION_PATH.search("/companies")
    assert _CREATION_PATH.search("/admin/create-company")
    assert not _CREATION_PATH.search("/companies/me")
    assert not _CREATION_PATH.search("/auth/switch-company/{target_company_id}")
    assert _CREATION_NAME.search("create_company") and _CREATION_NAME.search("register_new_tenant")


@pytest.mark.unit
def test_no_route_definition_creates_a_company_outside_the_bootstrap():
    """No write route may construct a Company, or look like a company-creation door, except the bootstrap.

    A route found here is a company-creation door: delete it. Adding it to
    ``ALLOWED_COMPANY_CREATING_ROUTES`` needs an owner decision reversing 2026-11-23.
    """
    offenders = []
    for rel, handler, verb, mounted, builds_company in _decorated_routes():
        if (rel, handler) in ALLOWED_COMPANY_CREATING_ROUTES:
            continue
        looks_like_creation = verb in {"post", "api_route"} and _CREATION_PATH.search(mounted)
        if builds_company or looks_like_creation or _CREATION_NAME.search(handler):
            offenders.append(f"{verb.upper()} {mounted} ({rel}::{handler})")
    assert offenders == []


@pytest.mark.unit
def test_no_mounted_post_route_is_a_company_creation_path():
    """The live app agrees with the source scan, prefixes and all."""
    served = [
        (r.path, r.endpoint.__name__)
        for r in app.routes
        if isinstance(r, APIRoute) and "POST" in r.methods and _CREATION_PATH.search(r.path)
    ]
    assert served == []


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
