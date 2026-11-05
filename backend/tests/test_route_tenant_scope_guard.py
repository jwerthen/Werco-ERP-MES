"""GUARDS: a secured route is tenant-scoped, and a tenant row is born with its company.

Invariant 1 (CLAUDE.md) says every query against tenant data is scoped to the
active company, derived from ``get_current_company_id``. Until this file, nothing
enforced it structurally: the MCP coverage guard (``tests/test_mcp_coverage.py``)
proves every ``/api/v1`` route is either secured or named in ``PUBLIC_OPERATIONS``,
but "secured and *untenanted*" passed every gate, and each instance was found only
by audit -- the 2026-11-05 deep dive found 25 such routes (complaints/RMA by id,
custom-field values, calibration history, the tool crib) plus five verbs that build
a ``TenantMixin`` row with no ``company_id`` and so 500 on the NOT NULL. This file
holds two structural guards, so tomorrow's route and tomorrow's constructor are
checked too:

1. **Route guard.** Every ``APIRoute`` under ``settings.API_V1_PREFIX`` whose
   dependency tree authenticates (reaches ``deps.oauth2_scheme``) must also reach
   ``deps.get_current_company_id`` -- anywhere in the tree, so a router-level or a
   wrapping dependency counts -- or be named in exactly one of two allowlists:

   * ``GLOBAL_ROUTES`` -- legitimately not scoped by the active company: self-only
     routes, static catalogs, platform administration that addresses tenants by
     explicit id under ``require_platform_admin``, and the four station/display
     principal resolvers, whose ``company_id`` comes from their own DB row.
   * ``TRACKED_DEBT`` -- real offenders, each with the reason and the Wave 1 branch
     that fixes it. This list is the work queue: the fixing PR deletes its entries.

2. **Constructor guard.** Every call under ``backend/app/`` that constructs a
   ``TenantMixin`` model must pass ``company_id=`` (or a ``**`` splat), or bind the
   result to a name and assign ``<name>.company_id = ...`` in the same function --
   or be named in ``UNGUARDED_CONSTRUCTIONS``.

Both allowlists are **stale-checked**: an entry whose route now depends on
``get_current_company_id`` (or no longer exists / is no longer secured), or whose
constructor now sets ``company_id`` (or is gone), fails the test with a message
saying to delete the entry. So the lists can only shrink honestly, never rot.

What these guards do NOT prove -- stated so nobody leans on them for it:

* A route that depends on ``get_current_company_id`` but then queries a tenant
  table without it (e.g. a stats query that forgets the filter) passes the route
  guard. Presence of the dependency is necessary, not sufficient; per-router
  tenant-isolation tests (``tests/api/*tenant_isolation*``) cover use.
* WebSocket routes (``APIWebSocketRoute``) authenticate inside the handler through
  ``services/access_identity.py`` and are out of scope here.
* The constructor guard resolves model names through the module's own imports. A
  model reached dynamically (``cls(...)``, a class held in a variable, ``getattr``)
  is invisible to it; ``company_id`` set through some other object
  (``setattr(other, ...)``, a helper that mutates the row later) does not count.

Both guards are pure introspection -- no database, no requests -- and the
``Test*Mechanics`` classes prove each classifier on synthetic input, so an empty
offender set in the live case is a finding rather than a vacuous pass.
"""

from __future__ import annotations

import ast
import importlib
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Dict, FrozenSet, Iterable, List, Mapping, Optional, Set, Tuple

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

import app.models as _all_models  # noqa: F401  -- registers every mapper on Base
from app.api import deps
from app.core.config import settings
from app.db.database import Base
from app.db.mixins import TenantMixin
from app.main import app as fastapi_app
from app.models.user import UserRole

pytestmark = pytest.mark.unit

BACKEND_DIR = Path(__file__).resolve().parents[1]
APP_DIR = BACKEND_DIR / "app"
API_PREFIX = settings.API_V1_PREFIX.rstrip("/")
HTTP_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})

RouteKey = Tuple[str, str]
ConstructionKey = Tuple[str, str, str]

# ---------------------------------------------------------------------------
# What "authenticated" and "tenant-scoped" mean, derived from app/api/deps.py
# ---------------------------------------------------------------------------
#
# ``oauth2_scheme`` is the only security scheme in the app (no HTTPBearer, no API-key
# header), and every credential door reads the bearer through it:
#
# * ``get_current_user`` -- and therefore everything that ``Depends`` on it:
#   ``get_current_active_user``, every ``require_role([...])`` product,
#   ``require_platform_admin``, ``get_admin_user``, ``get_audit_service``;
# * the four principal resolvers, which accept a NON-access JWT (display, signin,
#   kiosk station) and call ``get_current_user`` directly -- not through
#   ``Depends`` -- for their user branch, so they never show ``get_current_user`` in
#   a route's dependency tree.
#
# So reaching ``oauth2_scheme`` is the complete test for "this route authenticates",
# and it is exactly what puts ``security`` on the OpenAPI operation -- the property
# the MCP coverage guard keys on. ``TestDepsDoorsAreKnown`` fails if deps.py grows a
# door this file does not know about.
SCOPE_DEPENDENCY = deps.get_current_company_id
PRINCIPAL_RESOLVERS: Tuple[Callable[..., Any], ...] = (
    deps.get_display_or_user,
    deps.get_signin_principal,
    deps.get_kiosk_or_user,
    deps.get_kiosk_station,
)
KNOWN_DOORS: Tuple[Callable[..., Any], ...] = (deps.get_current_user, *PRINCIPAL_RESOLVERS)

# The Wave 1 branches that own a TRACKED_DEBT / UNGUARDED_CONSTRUCTIONS entry. Named
# so a typo in an entry is a test failure rather than a dangling pointer.
WAVE_1_BRANCHES = frozenset(
    {
        "fix/complaints-rma-tenant-audit",
        "fix/customers-custom-fields-tenant",
        "fix/calibration-tools-downtime-tenant",
        "fix/ncr-car-audit-chain",
        "fix/controlled-write-audit-gaps",
    }
)


@dataclass(frozen=True)
class TrackedDebt:
    """A known offender: why it is wrong, and which branch removes the entry."""

    reason: str
    fixed_by: Optional[str] = None


_SELF = "Self-scoped: reads/writes only current_user's own record; no tenant query."
_STATIC = "Static catalog built from code; no database read."
_PLATFORM = "Platform administration under require_platform_admin; addresses tenants by explicit company id."
_CHAIN = "require_platform_admin; the audit hash chain is ONE global sequence across tenants, verified whole."

# Authenticated routes that legitimately do not depend on get_current_company_id.
GLOBAL_ROUTES: Dict[RouteKey, str] = {
    # -- the caller's own session / profile ------------------------------------------------
    ("POST", "/api/v1/auth/logout"): _SELF + " Writes the caller's LOGOUT auth event.",
    ("POST", "/api/v1/auth/employee-logout"): _SELF + " Identity is the bearer, never the body.",
    ("GET", "/api/v1/users/me"): _SELF,
    ("PUT", "/api/v1/users/me/phone"): _SELF + " Audited.",
    ("POST", "/api/v1/users/change-password"): _SELF + " Audited.",
    # -- static catalogs --------------------------------------------------------------------
    ("GET", "/api/v1/documents/types/list"): _STATIC + " (the DocumentType enum)",
    ("GET", "/api/v1/analytics/data-sources"): _STATIC + " (the report-builder field catalog)",
    ("GET", "/api/v1/import/templates"): _STATIC + " (the import-template registry)",
    ("GET", "/api/v1/import/templates/{entity}"): _STATIC + " (a blank XLSX built from the registry)",
    ("GET", "/api/v1/notifications/catalog"): _STATIC + " (notification_catalog.CATALOG)",
    # -- platform administration ------------------------------------------------------------
    ("POST", "/api/v1/auth/switch-company/{target_company_id}"): (
        "require_platform_admin context switch: the target company is the explicit path id (active only)."
    ),
    ("GET", "/api/v1/platform/companies"): _PLATFORM,
    ("POST", "/api/v1/platform/companies"): _PLATFORM,
    ("GET", "/api/v1/platform/companies/{company_id}"): _PLATFORM,
    ("PUT", "/api/v1/platform/companies/{company_id}"): _PLATFORM,
    ("GET", "/api/v1/platform/companies/{company_id}/users"): _PLATFORM,
    ("GET", "/api/v1/platform/companies/{company_id}/dashboard"): _PLATFORM,
    ("GET", "/api/v1/platform/overview"): _PLATFORM,
    ("GET", "/api/v1/audit/integrity/status"): _CHAIN,
    ("GET", "/api/v1/audit/integrity/verify"): _CHAIN,
    ("GET", "/api/v1/audit/integrity/verify-recent"): _CHAIN,
    ("GET", "/api/v1/audit/integrity/record/{sequence_number}"): (
        "Global chain addressed by sequence_number (the predecessor read crosses tenants by design); a "
        "non-platform Admin is fenced by hand on current_user._active_company_id -- the value "
        "get_current_company_id returns -- and gets the same 404 for a foreign record."
    ),
    ("POST", "/api/v1/admin/settings/seed-database"): (
        "Whole-install bootstrap gated on db.query(User).first(): an authenticated caller implies a user "
        "row, so it can only answer already_seeded and never writes."
    ),
    # -- station / display principals: company_id comes from the principal's own DB row ------
    ("GET", "/api/v1/shop-floor/wallboard"): (
        "get_display_or_user: scoped by principal.company_id (display_tokens row, or the user's active "
        "company). deps.py: the ONLY route this resolver may guard."
    ),
    ("GET", "/api/v1/shop-floor/work-center-queue/{work_center_id}"): (
        "get_kiosk_or_user: scoped by principal.company_id; a station is further fenced to its own work center."
    ),
    ("GET", "/api/v1/shop-floor/kiosk-stations/work-centers"): (
        "get_kiosk_station: lists work centers via tenant_query on the kiosk_stations row's company_id."
    ),
    ("PUT", "/api/v1/shop-floor/kiosk-stations/work-center"): (
        "get_kiosk_station: selects a work center via tenant_query on the station row's company_id; audited "
        "under that company."
    ),
    ("POST", "/api/v1/auth/kiosk-badge-token"): (
        "get_kiosk_station: the badge lookup is fenced to the kiosk_stations row's company_id."
    ),
    ("POST", "/api/v1/visitor-logs/sign-in"): (
        "get_signin_principal: scoped by principal.company_id (signin_stations row, or the user's active "
        "company). deps.py: one of the only two routes this resolver may guard."
    ),
    ("POST", "/api/v1/visitor-logs/sign-out"): (
        "get_signin_principal: scoped by principal.company_id. deps.py: one of the only two routes this "
        "resolver may guard."
    ),
}

_COMPLAINTS = "fix/complaints-rma-tenant-audit"
_CUSTOM_FIELDS = "fix/customers-custom-fields-tenant"
_CAL_TOOLS = "fix/calibration-tools-downtime-tenant"


def _by_id(model: str, verb: str) -> str:
    return f"Resolves {model} by id with no company filter: any authenticated user can {verb} another tenant's row."


# Authenticated routes that SHOULD be tenant-scoped and are not. Each fixing PR deletes its rows.
TRACKED_DEBT: Dict[RouteKey, TrackedDebt] = {
    # -- customer_complaints.py: by-id complaint/RMA verbs and the cross-tenant dashboard ------
    ("GET", "/api/v1/complaints/complaints/dashboard"): TrackedDebt(
        "Aggregates CustomerComplaint/RMA counts across every tenant (no company filter).", _COMPLAINTS
    ),
    ("GET", "/api/v1/complaints/complaints/8d-report/{complaint_id}"): TrackedDebt(
        _by_id("CustomerComplaint", "read the 8D report of"), _COMPLAINTS
    ),
    ("GET", "/api/v1/complaints/complaints/{complaint_id}"): TrackedDebt(
        _by_id("CustomerComplaint", "read"), _COMPLAINTS
    ),
    ("POST", "/api/v1/complaints/complaints/{complaint_id}/investigate"): TrackedDebt(
        _by_id("CustomerComplaint", "move to investigating"), _COMPLAINTS
    ),
    ("POST", "/api/v1/complaints/complaints/{complaint_id}/resolve"): TrackedDebt(
        _by_id("CustomerComplaint", "resolve"), _COMPLAINTS
    ),
    ("POST", "/api/v1/complaints/complaints/{complaint_id}/close"): TrackedDebt(
        _by_id("CustomerComplaint", "close"), _COMPLAINTS
    ),
    ("POST", "/api/v1/complaints/complaints/{complaint_id}/create-ncr"): TrackedDebt(
        _by_id("CustomerComplaint", "escalate to an NCR")
        + " The NCR is also built without company_id (see UNGUARDED_CONSTRUCTIONS).",
        _COMPLAINTS,
    ),
    ("POST", "/api/v1/complaints/complaints/{complaint_id}/create-car"): TrackedDebt(
        _by_id("CustomerComplaint", "escalate to a CAR")
        + " The CAR is also built without company_id (see UNGUARDED_CONSTRUCTIONS).",
        _COMPLAINTS,
    ),
    ("GET", "/api/v1/complaints/rma/{rma_id}"): TrackedDebt(_by_id("ReturnMaterialAuthorization", "read"), _COMPLAINTS),
    ("POST", "/api/v1/complaints/rma/{rma_id}/approve"): TrackedDebt(
        _by_id("ReturnMaterialAuthorization", "approve"), _COMPLAINTS
    ),
    ("POST", "/api/v1/complaints/rma/{rma_id}/deny"): TrackedDebt(
        _by_id("ReturnMaterialAuthorization", "deny"), _COMPLAINTS
    ),
    ("POST", "/api/v1/complaints/rma/{rma_id}/receive"): TrackedDebt(
        _by_id("ReturnMaterialAuthorization", "receive"), _COMPLAINTS
    ),
    ("POST", "/api/v1/complaints/rma/{rma_id}/inspect"): TrackedDebt(
        _by_id("ReturnMaterialAuthorization", "inspect"), _COMPLAINTS
    ),
    ("POST", "/api/v1/complaints/rma/{rma_id}/dispose"): TrackedDebt(
        _by_id("ReturnMaterialAuthorization", "dispose"), _COMPLAINTS
    ),
    ("POST", "/api/v1/complaints/rma/{rma_id}/complete"): TrackedDebt(
        _by_id("ReturnMaterialAuthorization", "complete"), _COMPLAINTS
    ),
    # -- custom_fields.py: the VALUES routes (the definitions routes are scoped) ---------------
    ("GET", "/api/v1/custom-fields/values/{entity_type}/{entity_id}"): TrackedDebt(
        "Reads CustomFieldValue rows joined to definitions with no company filter.", _CUSTOM_FIELDS
    ),
    ("POST", "/api/v1/custom-fields/values/{entity_type}/{entity_id}"): TrackedDebt(
        "Resolves the CustomFieldDefinition by field_key with no company filter, then writes the value.",
        _CUSTOM_FIELDS,
    ),
    ("POST", "/api/v1/custom-fields/values/bulk"): TrackedDebt(
        "Resolves each CustomFieldDefinition by field_key with no company filter, then writes values.",
        _CUSTOM_FIELDS,
    ),
    ("DELETE", "/api/v1/custom-fields/values/{entity_type}/{entity_id}/{field_key}"): TrackedDebt(
        "Resolves the definition and deletes the value with no company filter.", _CUSTOM_FIELDS
    ),
    # -- calibration.py / tool_management.py --------------------------------------------------
    ("GET", "/api/v1/calibration/equipment/{equipment_id}/history"): TrackedDebt(
        "Reads CalibrationRecord rows by equipment_id with no company filter.", _CAL_TOOLS
    ),
    ("GET", "/api/v1/tool-management/tools/dashboard"): TrackedDebt(
        "Aggregates every tenant's Tool rows (no company filter).", _CAL_TOOLS
    ),
    ("GET", "/api/v1/tool-management/tools/checked-out"): TrackedDebt(
        "Lists every tenant's checked-out tools (no company filter).", _CAL_TOOLS
    ),
    ("GET", "/api/v1/tool-management/tools/replacement-due"): TrackedDebt(
        "Lists every tenant's replacement-due tools (no company filter).", _CAL_TOOLS
    ),
    ("GET", "/api/v1/tool-management/tools/inspection-due"): TrackedDebt(
        "Lists every tenant's inspection-due tools (no company filter).", _CAL_TOOLS
    ),
    ("GET", "/api/v1/tool-management/tools/{tool_id}/history"): TrackedDebt(
        _by_id("Tool", "read the checkout/usage history of"), _CAL_TOOLS
    ),
}


# TenantMixin constructions under app/ that neither pass company_id nor assign it.
# Keyed by (path relative to backend/, innermost enclosing function, model class name).
UNGUARDED_CONSTRUCTIONS: Dict[ConstructionKey, TrackedDebt] = {
    ("app/api/endpoints/calibration.py", "record_calibration", "CalibrationRecord"): TrackedDebt(
        "Every call 500s on the NOT NULL; set company_id from the resolved equipment.", _CAL_TOOLS
    ),
    ("app/api/endpoints/tool_management.py", "checkout_tool", "ToolCheckout"): TrackedDebt(
        "Every call 500s on the NOT NULL; set company_id from the resolved tool.", _CAL_TOOLS
    ),
    ("app/api/endpoints/tool_management.py", "log_usage", "ToolUsageLog"): TrackedDebt(
        "Every call 500s on the NOT NULL; set company_id from the resolved tool.", _CAL_TOOLS
    ),
    ("app/api/endpoints/customer_complaints.py", "create_ncr_from_complaint", "NonConformanceReport"): TrackedDebt(
        "Every call 500s on the NOT NULL; build through quality_numbering with the complaint's company.",
        _COMPLAINTS,
    ),
    ("app/api/endpoints/customer_complaints.py", "create_car_from_complaint", "CorrectiveActionRequest"): TrackedDebt(
        "Every call 500s on the NOT NULL; build through quality_numbering with the complaint's company.",
        _COMPLAINTS,
    ),
}


# ===========================================================================
# Route guard machinery
# ===========================================================================


@dataclass(frozen=True)
class RouteScope:
    """What one route's dependency tree reaches."""

    key: RouteKey
    where: str  # "module.function" of the endpoint, for failure messages
    authenticated: bool
    scoped: bool
    doors: Tuple[str, ...]  # names of the known doors reached, for failure messages


def dependency_calls(dependant: Dependant) -> List[Any]:
    """Every ``call`` reachable from ``dependant``: route-level, router-level and nested dependencies."""
    calls: List[Any] = []
    stack = [dependant]
    while stack:
        current = stack.pop()
        if current.call is not None:
            calls.append(current.call)
        stack.extend(current.dependencies)
    return calls


def _reaches(calls: Iterable[Any], target: Any) -> bool:
    return any(call is target for call in calls)


def route_scopes(routes: Iterable[Any], prefix: str = API_PREFIX) -> Dict[RouteKey, RouteScope]:
    """Classify every ``(METHOD, path)`` of every ``APIRoute`` under ``prefix``."""
    scopes: Dict[RouteKey, RouteScope] = {}
    for route in routes:
        if not isinstance(route, APIRoute) or not route.path_format.startswith(prefix):
            continue
        calls = dependency_calls(route.dependant)
        authenticated = _reaches(calls, deps.oauth2_scheme)
        scoped = _reaches(calls, SCOPE_DEPENDENCY)
        doors = tuple(door.__name__ for door in KNOWN_DOORS if _reaches(calls, door))
        where = f"{route.endpoint.__module__}.{route.endpoint.__name__}"
        for method in sorted(route.methods & HTTP_METHODS):
            key = (method, route.path_format)
            scopes[key] = RouteScope(key, where, authenticated, scoped, doors)
    return scopes


def unscoped_route_keys(scopes: Mapping[RouteKey, RouteScope]) -> Set[RouteKey]:
    """Authenticated routes whose tree never reaches ``get_current_company_id``."""
    return {key for key, scope in scopes.items() if scope.authenticated and not scope.scoped}


def unaccounted_routes(
    scopes: Mapping[RouteKey, RouteScope],
    global_routes: Iterable[RouteKey],
    tracked_debt: Iterable[RouteKey],
) -> Set[RouteKey]:
    return unscoped_route_keys(scopes) - set(global_routes) - set(tracked_debt)


def stale_route_entries(scopes: Mapping[RouteKey, RouteScope], allowlisted: Iterable[RouteKey]) -> Dict[RouteKey, str]:
    """Allowlisted keys that no longer describe an unscoped authenticated route, with why."""
    stale: Dict[RouteKey, str] = {}
    for key in allowlisted:
        scope = scopes.get(key)
        if scope is None:
            stale[key] = "no such route any more"
        elif not scope.authenticated:
            stale[key] = "route is no longer authenticated (it is public now -- see PUBLIC_OPERATIONS)"
        elif scope.scoped:
            stale[key] = "route now depends on get_current_company_id"
    return stale


def _describe(scope: Optional[RouteScope]) -> str:
    if scope is None:
        return ""
    door = ", ".join(scope.doors) or "a bare oauth2_scheme read -- an unrecognized door"
    return f"{scope.where} via {door}"


# ===========================================================================
# Constructor guard machinery
# ===========================================================================


@dataclass(frozen=True)
class Construction:
    file: str
    function: str
    model: str
    line: int
    guarded: bool

    @property
    def key(self) -> ConstructionKey:
        return (self.file, self.function, self.model)


def tenant_model_classes() -> FrozenSet[type]:
    """Every mapped class that carries ``company_id`` through ``TenantMixin``."""
    return frozenset(mapper.class_ for mapper in Base.registry.mappers if issubclass(mapper.class_, TenantMixin))


_Binding = Tuple[str, Optional[str]]  # (absolute module, attribute) -- attribute None binds the module itself
_FUNCTION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef)


def _absolute_module(module: Optional[str], level: int, current: str, is_package: bool) -> str:
    if level == 0:
        return module or ""
    parts = current.split(".")
    if not is_package:
        parts = parts[:-1]
    parts = parts[: len(parts) - (level - 1)]
    return ".".join(parts + ([module] if module else []))


def _own_nodes(scope: ast.AST) -> Iterable[ast.AST]:
    """Nodes of ``scope`` excluding the bodies of nested functions (but not nested classes/blocks)."""
    stack = list(ast.iter_child_nodes(scope))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, _FUNCTION_NODES):
            stack.extend(ast.iter_child_nodes(node))


def _bindings(scope: ast.AST, current: str, is_package: bool, *, module_level: bool) -> Dict[str, _Binding]:
    bound: Dict[str, _Binding] = {}
    for node in _own_nodes(scope):
        if isinstance(node, ast.ImportFrom):
            source = _absolute_module(node.module, node.level, current, is_package)
            for alias in node.names:
                bound[alias.asname or alias.name] = (source, alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    bound[alias.asname] = (alias.name, None)
                else:
                    head = alias.name.split(".")[0]
                    bound[head] = (head, None)
        elif module_level and isinstance(node, ast.ClassDef):
            bound[node.name] = (current, node.name)
    return bound


def _load(module: str) -> Optional[ModuleType]:
    # Only the application's own modules are ever resolved: a TenantMixin model can
    # only be defined under ``app``, and importing arbitrary third-party modules from
    # a guard would be slow and side-effectful.
    if module != "app" and not module.startswith("app."):
        return None
    if module in sys.modules:
        return sys.modules[module]
    try:
        return importlib.import_module(module)
    except ImportError:
        return None


def _resolve_binding(binding: _Binding) -> Any:
    module, attribute = binding
    loaded = _load(module)
    if loaded is None or attribute is None:
        return loaded
    found = getattr(loaded, attribute, None)
    if found is None:
        found = _load(f"{module}.{attribute}")  # ``from app import models`` binds a submodule
    return found


def _resolve_callee(func: ast.expr, scopes: List[Dict[str, _Binding]]) -> Any:
    chain: List[str] = []
    node = func
    while isinstance(node, ast.Attribute):
        chain.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    binding = next((scope[node.id] for scope in scopes if node.id in scope), None)
    if binding is None:
        return None
    target = _resolve_binding(binding)
    for attribute in reversed(chain):
        if target is None:
            return None
        target = getattr(target, attribute, None)
    return target


def _assigns_company_id(scope: ast.AST, name: str) -> bool:
    for node in ast.walk(scope):
        targets: List[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            targets = [node.target]
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "setattr"
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == name
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "company_id"
        ):
            return True
        for target in targets:
            if (
                isinstance(target, ast.Attribute)
                and target.attr == "company_id"
                and isinstance(target.value, ast.Name)
                and target.value.id == name
            ):
                return True
    return False


def scan_source(
    source: str, *, file: str, module: str, is_package: bool, tenant_classes: FrozenSet[type]
) -> List[Construction]:
    """Every call in ``source`` that constructs one of ``tenant_classes``, and whether it sets company_id."""
    tree = ast.parse(source)
    parents: Dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    module_bindings = _bindings(tree, module, is_package, module_level=True)
    function_bindings: Dict[ast.AST, Dict[str, _Binding]] = {}
    found: List[Construction] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        enclosing: List[ast.AST] = []
        cursor: ast.AST = node
        while cursor in parents:
            cursor = parents[cursor]
            if isinstance(cursor, _FUNCTION_NODES):
                enclosing.append(cursor)
        scopes = []
        for function in enclosing:
            if function not in function_bindings:
                function_bindings[function] = _bindings(function, module, is_package, module_level=False)
            scopes.append(function_bindings[function])
        scopes.append(module_bindings)

        callee = _resolve_callee(node.func, scopes)
        if not isinstance(callee, type) or callee not in tenant_classes:
            continue

        guarded = any(keyword.arg is None or keyword.arg == "company_id" for keyword in node.keywords)
        if not guarded:
            parent = parents.get(node)
            bound: Optional[str] = None
            if isinstance(parent, ast.Assign) and len(parent.targets) == 1 and isinstance(parent.targets[0], ast.Name):
                bound = parent.targets[0].id
            elif isinstance(parent, ast.AnnAssign) and isinstance(parent.target, ast.Name):
                bound = parent.target.id
            if bound is not None:
                guarded = _assigns_company_id(enclosing[0] if enclosing else tree, bound)

        function_name = getattr(enclosing[0], "name", "<module>") if enclosing else "<module>"
        found.append(Construction(file, function_name, callee.__name__, node.lineno, guarded))
    return found


def scan_app(tenant_classes: FrozenSet[type]) -> List[Construction]:
    constructions: List[Construction] = []
    for path in sorted(APP_DIR.rglob("*.py")):
        relative = path.relative_to(BACKEND_DIR)
        is_package = path.name == "__init__.py"
        parts = relative.with_suffix("").parts
        module = ".".join(parts[:-1] if is_package else parts)
        constructions.extend(
            scan_source(
                path.read_text(encoding="utf-8"),
                file=relative.as_posix(),
                module=module,
                is_package=is_package,
                tenant_classes=tenant_classes,
            )
        )
    return constructions


# ===========================================================================
# Live guards
# ===========================================================================


@pytest.fixture(scope="module")
def live_scopes() -> Dict[RouteKey, RouteScope]:
    return route_scopes(fastapi_app.routes)


@pytest.fixture(scope="module")
def tenant_classes() -> FrozenSet[type]:
    return tenant_model_classes()


@pytest.fixture(scope="module")
def live_constructions(tenant_classes) -> List[Construction]:
    return scan_app(tenant_classes)


class TestRouteTenantScope:
    def test_every_authenticated_route_is_scoped_or_named(self, live_scopes):
        missing = unaccounted_routes(live_scopes, GLOBAL_ROUTES, TRACKED_DEBT)
        lines = [f"  {method} {path}  ({_describe(live_scopes[(method, path)])})" for method, path in sorted(missing)]
        assert not missing, (
            "Authenticated route(s) that never depend on get_current_company_id:\n"
            + "\n".join(lines)
            + "\nAdd `company_id: int = Depends(get_current_company_id)` and scope every tenant query with it "
            "(tenant_query/tenant_filter), or -- only if the route genuinely reads no tenant data by the active "
            "company -- name it in GLOBAL_ROUTES in tests/test_route_tenant_scope_guard.py with a one-line reason."
        )

    def test_no_allowlist_entry_is_stale(self, live_scopes):
        stale = stale_route_entries(live_scopes, list(GLOBAL_ROUTES) + list(TRACKED_DEBT))
        lines = [f"  {method} {path}: {why}" for (method, path), why in sorted(stale.items())]
        assert not stale, (
            "Stale allowlist entr(ies) in tests/test_route_tenant_scope_guard.py -- delete them from "
            "GLOBAL_ROUTES / TRACKED_DEBT:\n" + "\n".join(lines)
        )

    def test_the_two_lists_are_disjoint_and_well_formed(self):
        assert not set(GLOBAL_ROUTES) & set(TRACKED_DEBT), "a route is either global or debt, never both"
        for key, reason in GLOBAL_ROUTES.items():
            assert reason.strip(), f"GLOBAL_ROUTES{key} needs a reason"
        for key, debt in TRACKED_DEBT.items():
            assert debt.reason.strip(), f"TRACKED_DEBT{key} needs a reason"
            assert debt.fixed_by is None or debt.fixed_by in WAVE_1_BRANCHES, f"TRACKED_DEBT{key}: {debt.fixed_by}"
        for method, path in list(GLOBAL_ROUTES) + list(TRACKED_DEBT):
            assert method in HTTP_METHODS and path.startswith(API_PREFIX + "/"), (method, path)

    def test_platform_routes_are_platform_admin_gated(self):
        """The platform entries are justified by ``require_platform_admin`` -- hold them to it."""
        platform = [
            route
            for route in fastapi_app.routes
            if isinstance(route, APIRoute) and route.path_format.startswith(f"{API_PREFIX}/platform/")
        ]
        assert platform, "the platform router moved -- re-derive this check"
        for route in platform:
            assert _reaches(dependency_calls(route.dependant), deps.require_platform_admin), route.path_format

    def test_the_guard_is_not_vacuous_on_the_live_app(self, live_scopes):
        authenticated = [scope for scope in live_scopes.values() if scope.authenticated]
        assert len(live_scopes) >= 600 and len(authenticated) >= 600
        assert sum(scope.scoped for scope in authenticated) >= 600
        # Every principal resolver guards at least one live route, so a GLOBAL_ROUTES
        # reason that cites one is about something that exists.
        reached = {door for scope in authenticated for door in scope.doors}
        assert {resolver.__name__ for resolver in PRINCIPAL_RESOLVERS} <= reached


class TestTenantConstructions:
    def test_every_tenant_construction_sets_company_id_or_is_named(self, live_constructions):
        unguarded = {c.key: c for c in live_constructions if not c.guarded}
        missing = sorted(set(unguarded) - set(UNGUARDED_CONSTRUCTIONS))
        lines = [f"  {file}:{unguarded[(file, fn, model)].line}  {fn}() builds {model}" for file, fn, model in missing]
        assert not missing, (
            "TenantMixin row(s) constructed without company_id:\n"
            + "\n".join(lines)
            + "\nPass `company_id=` (from get_current_company_id or the already-resolved tenant parent), or bind "
            "the row to a name and assign `<name>.company_id = ...` in the same function. TenantMixin.company_id "
            "is NOT NULL, so an unguarded construction is a 500 at flush or a row in the wrong tenant."
        )

    def test_no_construction_allowlist_entry_is_stale(self, live_constructions):
        unguarded = {c.key for c in live_constructions if not c.guarded}
        stale = sorted(set(UNGUARDED_CONSTRUCTIONS) - unguarded)
        lines = [f"  {file} :: {fn}() :: {model}" for file, fn, model in stale]
        assert not stale, (
            "Stale UNGUARDED_CONSTRUCTIONS entr(ies) in tests/test_route_tenant_scope_guard.py -- the construction "
            "now sets company_id (or is gone); delete the entry:\n" + "\n".join(lines)
        )
        for key, debt in UNGUARDED_CONSTRUCTIONS.items():
            assert debt.reason.strip() and (debt.fixed_by is None or debt.fixed_by in WAVE_1_BRANCHES), key

    def test_the_guard_is_not_vacuous_on_the_live_tree(self, tenant_classes, live_constructions):
        assert len(tenant_classes) >= 150, "TenantMixin discovery broke -- the Base registry is not populated"
        recognized = {c.model for c in live_constructions}
        assert len(live_constructions) >= 200 and len(recognized) >= 100, (
            len(live_constructions),
            len(recognized),
        )
        assert sum(c.guarded for c in live_constructions) >= 200


# ===========================================================================
# Mechanics: each classifier proven on synthetic input
# ===========================================================================


class TestDepsDoorsAreKnown:
    def test_every_bearer_reading_dependency_in_deps_is_a_known_door(self):
        """If deps.py grows a new credential door, this file must learn about it."""
        readers = set()
        for name in dir(deps):
            candidate = getattr(deps, name)
            if not callable(candidate) or getattr(candidate, "__module__", None) != deps.__name__:
                continue
            defaults = list(getattr(candidate, "__defaults__", None) or ())
            defaults += list((getattr(candidate, "__kwdefaults__", None) or {}).values())
            if any(getattr(default, "dependency", None) is deps.oauth2_scheme for default in defaults):
                readers.add(candidate)
        assert readers == set(KNOWN_DOORS), sorted(r.__name__ for r in readers ^ set(KNOWN_DOORS))


def _synthetic_app() -> FastAPI:
    synthetic = FastAPI()
    router = APIRouter()

    @router.get("/user-only")
    def user_only(user=Depends(deps.get_current_user)):
        return None

    @router.post("/role-only")
    def role_only(user=Depends(deps.require_role([UserRole.ADMIN]))):
        return None

    @router.get("/scoped")
    def scoped(
        user=Depends(deps.require_role([UserRole.ADMIN])),
        company_id: int = Depends(deps.get_current_company_id),
    ):
        return None

    @router.get("/kiosk")
    def kiosk(principal=Depends(deps.get_kiosk_or_user)):
        return None

    @router.get("/public")
    def public():
        return None

    def wrapper(company_id: int = Depends(deps.get_current_company_id)) -> int:
        return company_id

    @router.get("/wrapped")
    def wrapped(user=Depends(deps.get_current_user), company_id: int = Depends(wrapper)):
        return None

    scoped_router = APIRouter(dependencies=[Depends(deps.get_current_company_id)])

    @scoped_router.get("/router-level")
    def router_level(user=Depends(deps.get_current_user)):
        return None

    synthetic.include_router(router, prefix="/api/v1/x")
    synthetic.include_router(scoped_router, prefix="/api/v1/x")

    @synthetic.get("/outside/user-only")
    def outside(user=Depends(deps.get_current_user)):
        return None

    return synthetic


class TestRouteGuardMechanics:
    def test_classifies_doors_scope_and_prefix(self):
        scopes = route_scopes(_synthetic_app().routes)
        assert set(scopes) == {
            ("GET", "/api/v1/x/user-only"),
            ("POST", "/api/v1/x/role-only"),
            ("GET", "/api/v1/x/scoped"),
            ("GET", "/api/v1/x/kiosk"),
            ("GET", "/api/v1/x/public"),
            ("GET", "/api/v1/x/wrapped"),
            ("GET", "/api/v1/x/router-level"),
        }, "only routes under the API prefix are classified"
        assert unscoped_route_keys(scopes) == {
            ("GET", "/api/v1/x/user-only"),
            ("POST", "/api/v1/x/role-only"),  # require_role's closure reaches get_current_user
            ("GET", "/api/v1/x/kiosk"),  # a principal resolver is a door, but not the scope dependency
        }
        assert not scopes[("GET", "/api/v1/x/public")].authenticated
        assert scopes[("GET", "/api/v1/x/wrapped")].scoped, "a wrapping dependency counts"
        assert scopes[("GET", "/api/v1/x/router-level")].scoped, "a router-level dependency counts"
        assert scopes[("GET", "/api/v1/x/kiosk")].doors == ("get_kiosk_or_user",)

    def test_allowlists_excuse_and_go_stale(self):
        scopes = route_scopes(_synthetic_app().routes)
        user_only = ("GET", "/api/v1/x/user-only")
        role_only = ("POST", "/api/v1/x/role-only")
        kiosk = ("GET", "/api/v1/x/kiosk")
        assert unaccounted_routes(scopes, [kiosk], [user_only]) == {role_only}
        assert unaccounted_routes(scopes, [kiosk, role_only], [user_only]) == set()
        stale = stale_route_entries(
            scopes, [user_only, ("GET", "/api/v1/x/scoped"), ("GET", "/api/v1/x/public"), ("GET", "/api/v1/x/gone")]
        )
        assert set(stale) == {("GET", "/api/v1/x/scoped"), ("GET", "/api/v1/x/public"), ("GET", "/api/v1/x/gone")}
        assert "get_current_company_id" in stale[("GET", "/api/v1/x/scoped")]


def _scan(source: str, tenant_classes: FrozenSet[type]) -> Dict[Tuple[str, str], bool]:
    found = scan_source(
        source, file="app/synthetic.py", module="app.synthetic", is_package=False, tenant_classes=tenant_classes
    )
    return {(c.function, c.model): c.guarded for c in found}


class TestConstructorGuardMechanics:
    def test_keyword_splat_and_bound_assignment_guard(self, tenant_classes):
        source = '''
from app.models.calibration import CalibrationRecord
from app.models import tool_management as tm
import app.models.quality as quality_models


def kw(company_id):
    return CalibrationRecord(equipment_id=1, company_id=company_id)


def splat(data):
    return CalibrationRecord(**data)


def assigned(company_id):
    record = CalibrationRecord(equipment_id=1)
    record.company_id = company_id
    return record


def annotated(company_id):
    record: CalibrationRecord = CalibrationRecord(equipment_id=1)
    setattr(record, "company_id", company_id)
    return record


def missing():
    record = CalibrationRecord(equipment_id=1)
    return record


def unbound(db):
    db.add(CalibrationRecord(equipment_id=1))


def attribute_module():
    return tm.ToolCheckout(tool_id=1)


def dotted_module():
    return quality_models.NonConformanceReport(title="x")


def other_variable(company_id):
    record = CalibrationRecord(equipment_id=1)
    other = object()
    other.company_id = company_id
    return record
'''
        assert _scan(source, tenant_classes) == {
            ("kw", "CalibrationRecord"): True,
            ("splat", "CalibrationRecord"): True,
            ("assigned", "CalibrationRecord"): True,
            ("annotated", "CalibrationRecord"): True,
            ("missing", "CalibrationRecord"): False,
            ("unbound", "CalibrationRecord"): False,
            ("attribute_module", "ToolCheckout"): False,
            ("dotted_module", "NonConformanceReport"): False,
            ("other_variable", "CalibrationRecord"): False,
        }

    def test_names_resolve_through_imports_not_spelling(self, tenant_classes):
        """A same-named third-party class (python-docx ``Document``, ``mcp.types.Tool``) is not a model."""
        source = '''
from app.models.document import Document as ErpDocument


def docx_text(path):
    from docx import Document

    return Document(path)


def mcp_tool():
    from mcp.types import Tool

    return Tool(name="x")


def erp_document():
    return ErpDocument(title="x")


def shadowed_import():
    from app.models.tool_management import Tool

    return Tool(name="x")
'''
        assert _scan(source, tenant_classes) == {
            ("erp_document", "Document"): False,
            ("shadowed_import", "Tool"): False,
        }

    def test_the_known_tenant_classes_are_the_ones_resolved(self, tenant_classes):
        names = {cls.__name__ for cls in tenant_classes}
        assert {"CalibrationRecord", "ToolCheckout", "ToolUsageLog", "NonConformanceReport"} <= names
        assert {"Document", "Tool"} <= names, "the spelling-collision test above depends on these being models"
