"""GUARDS: a secured route is tenant-scoped, and a tenant row is born with its company.

Invariant 1 (CLAUDE.md) says every query against tenant data is scoped to the
active company, derived from ``get_current_company_id``. Until this file, nothing
enforced it structurally: the MCP coverage guard (``tests/test_mcp_coverage.py``)
proves every ``/api/v1`` route is either secured or named in ``PUBLIC_OPERATIONS``,
but "secured and *untenanted*" passed every gate, and each instance was found only
by audit -- the 2026-11-05 deep dive found 25 such routes (complaints/RMA by id,
custom-field values, calibration history, the tool crib) plus seven live call sites
that build a ``TenantMixin`` row with no ``company_id`` and so 500 on the NOT NULL
(five verbs, and both custom-field value writes). This file holds two structural
guards, so tomorrow's route and tomorrow's constructor are checked too:

1. **Route guard.** Every ``APIRoute`` under ``settings.API_V1_PREFIX`` whose
   dependency tree authenticates (reaches ``deps.oauth2_scheme``) must also reach
   ``deps.get_current_company_id`` -- anywhere in the tree, so a router-level or a
   wrapping dependency counts -- or be named in exactly one of two allowlists:

   * ``GLOBAL_ROUTES`` -- legitimately not scoped by the active company: self-only
     routes, static catalogs, platform administration that addresses tenants by
     explicit id under ``require_platform_admin``, and the four station/display
     principal resolvers, whose ``company_id`` comes from their own DB row.
   * ``TRACKED_DEBT`` -- real offenders, each with the reason and the Wave 1 branch
     that fixes it (``fixed_by=None`` for an unowned follow-up, whose reason names the
     fix). This list is the work queue: the fixing PR deletes its entries.

   A ``GLOBAL_ROUTES`` reason that cites a principal resolver or
   ``require_platform_admin`` must describe a route that still reaches it, a
   ``(METHOD, path)`` registered twice is classified by its worse twin, and nothing
   under the prefix may be a ``Mount`` or plain ``Route`` the guard cannot see into.

2. **Constructor guard.** Every call under ``backend/app/`` that constructs a tenant
   model -- a ``TenantMixin`` subclass, or a mapped class declaring its own NOT NULL
   ``company_id`` (``User``, the workspace and runtime-metric records) -- must pass
   ``company_id=`` (or a ``**{...}`` dict literal with a ``"company_id"`` key), or
   bind the result to a name and assign ``<name>.company_id = ...`` in the same
   function -- or be named in ``UNGUARDED_CONSTRUCTIONS``, which excuses exactly
   ``occurrences`` calls per key. A ``**`` splat of any other mapping
   (``**parsed``, ``**payload.model_dump()``) counts for nothing: the guard cannot see
   what it holds, and that shape is what hid the two custom-field value writes.

Both allowlists are **stale-checked**: an entry whose route now depends on
``get_current_company_id`` (or no longer exists / is no longer secured), or whose
constructor now sets ``company_id`` (or is gone, or occurs fewer times), fails the
test with a message saying to delete the entry. So the lists can only shrink
honestly, never rot.

What these guards do NOT prove -- stated so nobody leans on them for it:

* A route that depends on ``get_current_company_id`` but then queries a tenant
  table without it (e.g. a stats query that forgets the filter) passes the route
  guard. Presence of the dependency is necessary, not sufficient; per-router
  tenant-isolation tests (``tests/api/*tenant_isolation*``) cover use.
* WebSocket routes (``APIWebSocketRoute``) authenticate inside the handler through
  ``services/access_identity.py`` and are out of scope here.
* Unauthenticated routes (``PUBLIC_OPERATIONS`` in ``tests/test_mcp_coverage.py``)
  are out of scope by construction. A public route that derives a company from its
  own credential (a carrier HMAC webhook, a station login, a display-token claim)
  must scope by that credential, and is reviewed by hand.
* ``company_id=`` is checked for PRESENCE, not provenance: a value taken from
  request input (``company_id=payload.company_id``) or ``company_id=None`` still
  passes. Every live use is server-derived today; that is review's job to keep.
* The constructor guard resolves model names through the module's own imports. A
  model reached dynamically (``cls(...)``, a class held in a variable, ``getattr``)
  is invisible to it; ``company_id`` set through some other object
  (``setattr(other, ...)``, a helper that mutates the row later) does not count, and
  neither does the ``company=`` relationship form -- that one fails loudly (write
  ``company_id=company.id``, the house convention) rather than passing silently.

Both guards are pure introspection -- no database, no requests -- and the
``Test*Mechanics`` classes prove each classifier on synthetic input, so an empty
offender set in the live case is a finding rather than a vacuous pass.
"""

from __future__ import annotations

import ast
import importlib
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Annotated, Any, Callable, Dict, FrozenSet, Iterable, List, Mapping, Optional, Set, Tuple

import pytest
from fastapi import APIRouter, Depends, FastAPI, Security
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, APIWebSocketRoute
from starlette.routing import Mount, Route

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
# The gates a GLOBAL_ROUTES reason may cite as its justification. A reason that names one
# must describe a route that still reaches it (``misjustified_global_routes``): an entry
# keyed by (METHOD, path) would otherwise keep excusing the route after the dependency that
# made it legitimately global was swapped for a plain ``get_current_user``.
JUSTIFYING_GATES: Tuple[Callable[..., Any], ...] = (*PRINCIPAL_RESOLVERS, deps.require_platform_admin)

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


@dataclass(frozen=True)
class UnguardedConstruction(TrackedDebt):
    """A known unguarded constructor site; it excuses EXACTLY ``occurrences`` calls under its key.

    The key is (file, innermost function, model), so without a count a second unguarded build
    of the same model in the same function -- or in a same-named method of another class in
    the file -- would hide behind an existing entry.
    """

    occurrences: int = 1


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


# The one entry pair no Wave 1 branch owns. Its write branch is unreachable only while the
# unscoped ``db.query(User).first()`` existence gate holds -- nothing machine-checks that -- so it
# is filed as debt with a named fix rather than excused as global.
_SEED_DATABASE = (
    "Unowned follow-up: seed_database's write branch builds an is_superuser=True User plus four sample users "
    "with no company_id and no audit row, outside user_provisioning.create_tenant_user, gated only on an "
    "unscoped db.query(User).first(). Fix: delete the write branch (empty-install bootstrap has its own "
    "server-selected flow), then re-classify or remove the route."
)


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
        "Resolves the CustomFieldDefinition by field_key with no company filter, then writes the value. A new "
        "value is also built without company_id (see UNGUARDED_CONSTRUCTIONS).",
        _CUSTOM_FIELDS,
    ),
    ("POST", "/api/v1/custom-fields/values/bulk"): TrackedDebt(
        "Resolves each CustomFieldDefinition by field_key with no company filter, then writes values. A new "
        "value is also built without company_id (see UNGUARDED_CONSTRUCTIONS).",
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
    # -- unowned follow-up (no Wave 1 branch): debt, NOT an approved global exception -----------
    ("POST", "/api/v1/admin/settings/seed-database"): TrackedDebt(
        _SEED_DATABASE + " Today an authenticated caller is a user row, so it only answers already_seeded.",
    ),
}


# Tenant-model constructions under app/ that neither pass company_id nor assign it.
# Keyed by (path relative to backend/, innermost enclosing function, model class name).
UNGUARDED_CONSTRUCTIONS: Dict[ConstructionKey, UnguardedConstruction] = {
    ("app/api/endpoints/calibration.py", "record_calibration", "CalibrationRecord"): UnguardedConstruction(
        "Every call 500s on the NOT NULL; set company_id from the resolved equipment.", _CAL_TOOLS
    ),
    ("app/api/endpoints/tool_management.py", "checkout_tool", "ToolCheckout"): UnguardedConstruction(
        "Every call 500s on the NOT NULL; set company_id from the resolved tool.", _CAL_TOOLS
    ),
    ("app/api/endpoints/tool_management.py", "log_usage", "ToolUsageLog"): UnguardedConstruction(
        "Every call 500s on the NOT NULL; set company_id from the resolved tool.", _CAL_TOOLS
    ),
    (
        "app/api/endpoints/customer_complaints.py",
        "create_ncr_from_complaint",
        "NonConformanceReport",
    ): UnguardedConstruction(
        "Every call 500s on the NOT NULL; build through quality_numbering with the complaint's company.",
        _COMPLAINTS,
    ),
    (
        "app/api/endpoints/customer_complaints.py",
        "create_car_from_complaint",
        "CorrectiveActionRequest",
    ): UnguardedConstruction(
        "Every call 500s on the NOT NULL; build through quality_numbering with the complaint's company.",
        _COMPLAINTS,
    ),
    ("app/api/endpoints/custom_fields.py", "set_custom_field_value", "CustomFieldValue"): UnguardedConstruction(
        "Every create 500s on the NOT NULL: the **parsed splat is parse_value()'s value_* keys only, no company_id.",
        _CUSTOM_FIELDS,
    ),
    ("app/api/endpoints/custom_fields.py", "set_bulk_custom_fields", "CustomFieldValue"): UnguardedConstruction(
        "Every create 500s on the NOT NULL: the **parsed splat is parse_value()'s value_* keys only, no company_id.",
        _CUSTOM_FIELDS,
    ),
    # Unowned follow-up, paired with the seed-database TRACKED_DEBT row; named so a new User(...)
    # elsewhere still fails.
    ("app/api/endpoints/admin_settings.py", "seed_database", "User"): UnguardedConstruction(
        _SEED_DATABASE + " Dead code today rather than a live 500.",
        occurrences=2,
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
    gates: FrozenSet[str] = frozenset()  # names of the JUSTIFYING_GATES reached


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
    """Classify every ``(METHOD, path)`` of every ``APIRoute`` under ``prefix``.

    A ``(METHOD, path)`` registered twice is merged PESSIMISTICALLY -- authenticated if
    either registration is, scoped only if both are. Starlette serves the FIRST match, so a
    plain "last one wins" dict would let a scoped twin registered later vouch for the
    unscoped route that actually answers.
    """
    scopes: Dict[RouteKey, RouteScope] = {}
    for route in routes:
        if not isinstance(route, APIRoute) or not route.path_format.startswith(prefix):
            continue
        calls = dependency_calls(route.dependant)
        authenticated = _reaches(calls, deps.oauth2_scheme)
        scoped = _reaches(calls, SCOPE_DEPENDENCY)
        doors = tuple(door.__name__ for door in KNOWN_DOORS if _reaches(calls, door))
        gates = frozenset(gate.__name__ for gate in JUSTIFYING_GATES if _reaches(calls, gate))
        where = f"{route.endpoint.__module__}.{route.endpoint.__name__}"
        for method in sorted(route.methods & HTTP_METHODS):
            key = (method, route.path_format)
            earlier = scopes.get(key)
            if earlier is None:
                scopes[key] = RouteScope(key, where, authenticated, scoped, doors, gates)
            else:
                scopes[key] = RouteScope(
                    key,
                    f"{earlier.where} + {where} (registered twice)",
                    earlier.authenticated or authenticated,
                    earlier.scoped and scoped,
                    tuple(door.__name__ for door in KNOWN_DOORS if door.__name__ in {*earlier.doors, *doors}),
                    earlier.gates & gates,
                )
    return scopes


def unclassifiable_routes(routes: Iterable[Any], prefix: str = API_PREFIX) -> List[str]:
    """Routes under ``prefix`` that ``route_scopes`` cannot see into.

    ``route_scopes`` reads ``APIRoute.dependant``; a plain Starlette ``Route`` has none, and a
    ``Mount`` hides its sub-application's routes from ``app.routes`` entirely. Either one under
    the API prefix (or a ``Mount`` enclosing it) would be an authenticated surface the guard
    silently skips. ``APIWebSocketRoute`` is out of scope by design (see the module docstring).
    A route-less mount (static files at ``/``) hides nothing and is not reported.
    """
    found: List[str] = []
    for route in routes:
        if isinstance(route, (APIRoute, APIWebSocketRoute)):
            continue
        path = getattr(route, "path", "") or ""
        if isinstance(route, Mount):
            encloses = (prefix + "/").startswith(path.rstrip("/") + "/")
            if (path.startswith(prefix) or encloses) and route.routes:
                found.append(f"Mount {path or '/'} ({len(route.routes)} sub-route(s) the guard never classifies)")
        elif path.startswith(prefix):
            found.append(f"{type(route).__name__} {path}")
    return found


def misjustified_global_routes(
    scopes: Mapping[RouteKey, RouteScope], global_routes: Mapping[RouteKey, str]
) -> Dict[RouteKey, List[str]]:
    """GLOBAL_ROUTES entries whose reason cites a JUSTIFYING_GATE the route no longer reaches."""
    wrong: Dict[RouteKey, List[str]] = {}
    for key, reason in global_routes.items():
        scope = scopes.get(key)
        if scope is None:
            continue  # stale_route_entries reports a vanished route
        missing = [
            gate.__name__ for gate in JUSTIFYING_GATES if gate.__name__ in reason and gate.__name__ not in scope.gates
        ]
        if missing:
            wrong[key] = missing
    return wrong


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
    """Every mapped class whose rows must be born with a company.

    That is every ``TenantMixin`` model, plus the few mapped classes that declare their own
    NOT NULL ``company_id`` instead of inheriting it (``User``, the workspace records, the
    runtime-metric rows). The NOT NULL is what turns a missing company into a 500 at flush,
    whichever way the column was declared, and ``User`` is the one whose constructions most
    need watching (CLAUDE.md: tenant accounts go through ``create_tenant_user``). A nullable
    ``company_id`` (``AuditLog``: platform events carry none) is not included.
    """
    classes: Set[type] = set()
    for mapper in Base.registry.mappers:
        column = mapper.columns.get("company_id")
        if issubclass(mapper.class_, TenantMixin) or (column is not None and not column.nullable):
            classes.add(mapper.class_)
    return frozenset(classes)


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


def _keyword_sets_company_id(keyword: ast.keyword) -> bool:
    """``company_id=...``, or a ``**{...}`` dict LITERAL with a ``"company_id"`` key.

    A splat of anything else (``**parsed``, ``**payload.model_dump()``) proves nothing: the
    guard cannot see what the mapping holds, and that is exactly the shape that hid
    ``CustomFieldValue(..., **parsed)`` -- ``parse_value`` returns only the ``value_*`` keys.
    Such a construction is guarded only by the bound-name ``<name>.company_id = ...`` check.
    """
    if keyword.arg is not None:
        return keyword.arg == "company_id"
    return isinstance(keyword.value, ast.Dict) and any(
        isinstance(key, ast.Constant) and key.value == "company_id" for key in keyword.value.keys
    )


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

        guarded = any(_keyword_sets_company_id(keyword) for keyword in node.keywords)
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


def construction_drift(
    constructions: Iterable[Construction], allowlist: Mapping[ConstructionKey, UnguardedConstruction]
) -> Tuple[List[Construction], Dict[ConstructionKey, Tuple[int, int]]]:
    """``(uncovered, drift)`` for the unguarded constructions in ``constructions``.

    ``uncovered`` are unguarded constructions under a key the allowlist does not name.
    ``drift`` maps each allowlisted key whose live count differs from its ``occurrences`` to
    ``(live count, occurrences)``: more is a new offender hiding behind an old entry, fewer
    means a fix landed and the entry must shrink or go.
    """
    unguarded = [c for c in constructions if not c.guarded]
    counts = Counter(c.key for c in unguarded)
    uncovered = [c for c in unguarded if c.key not in allowlist]
    drift = {
        key: (counts.get(key, 0), entry.occurrences)
        for key, entry in allowlist.items()
        if counts.get(key, 0) != entry.occurrences
    }
    return uncovered, drift


def _site(construction: Construction) -> Tuple[str, int]:
    return (construction.file, construction.line)


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

    def test_every_route_under_the_prefix_is_one_the_guard_can_classify(self):
        hidden = unclassifiable_routes(fastapi_app.routes)
        assert not hidden, (
            "Route(s) under the API prefix that route_scopes cannot see into (it reads APIRoute.dependant):\n  "
            + "\n  ".join(hidden)
            + "\nRegister them through an APIRouter, or teach tests/test_route_tenant_scope_guard.py to walk them."
        )

    def test_global_reasons_cite_only_gates_the_route_still_reaches(self, live_scopes):
        wrong = misjustified_global_routes(live_scopes, GLOBAL_ROUTES)
        lines = [f"  {method} {path}: cites {', '.join(gates)}" for (method, path), gates in sorted(wrong.items())]
        assert not wrong, (
            "GLOBAL_ROUTES entr(ies) justified by a dependency the route no longer reaches -- the route is not "
            "global for the reason given; re-classify it (likely TRACKED_DEBT, or scope it):\n" + "\n".join(lines)
        )

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
        uncovered, drift = construction_drift(live_constructions, UNGUARDED_CONSTRUCTIONS)
        lines = [f"  {c.file}:{c.line}  {c.function}() builds {c.model}" for c in sorted(uncovered, key=_site)]
        for key, (live, allowed) in sorted(drift.items()):
            if live > allowed:
                lines += [
                    f"  {c.file}:{c.line}  {c.function}() builds {c.model}  "
                    f"(UNGUARDED_CONSTRUCTIONS excuses {allowed} here, found {live})"
                    for c in sorted(live_constructions, key=_site)
                    if c.key == key and not c.guarded
                ]
        assert not lines, (
            "Tenant row(s) constructed without company_id:\n"
            + "\n".join(lines)
            + "\nPass `company_id=` (from get_current_company_id or the already-resolved tenant parent), or bind "
            "the row to a name and assign `<name>.company_id = ...` in the same function. company_id is NOT NULL "
            "on every tenant model, so an unguarded construction is a 500 at flush or a row in the wrong tenant."
        )

    def test_no_construction_allowlist_entry_is_stale(self, live_constructions):
        _, drift = construction_drift(live_constructions, UNGUARDED_CONSTRUCTIONS)
        lines = [
            f"  {file} :: {fn}() :: {model}  (occurrences={allowed}, found {live})"
            for (file, fn, model), (live, allowed) in sorted(drift.items())
            if live < allowed
        ]
        assert not lines, (
            "Stale UNGUARDED_CONSTRUCTIONS entr(ies) in tests/test_route_tenant_scope_guard.py -- the construction "
            "now sets company_id (or is gone); delete the entry, or lower its occurrences to what remains:\n"
            + "\n".join(lines)
        )
        for key, debt in UNGUARDED_CONSTRUCTIONS.items():
            assert debt.reason.strip() and (debt.fixed_by is None or debt.fixed_by in WAVE_1_BRANCHES), key
            assert debt.occurrences >= 1, key

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
        assert "no longer authenticated" in stale[("GET", "/api/v1/x/public")]
        assert "no such route" in stale[("GET", "/api/v1/x/gone")]

    def test_annotated_decorator_level_and_hand_rolled_bearer_forms(self):
        synthetic = FastAPI()
        router = APIRouter()

        @router.get("/annotated")
        def annotated(
            company_id: Annotated[int, Depends(deps.get_current_company_id)],
            user=Depends(deps.get_current_user),
        ):
            return None

        @router.get("/decorator-level", dependencies=[Depends(deps.get_current_company_id)])
        def decorator_level(user=Depends(deps.get_current_user)):
            return None

        @router.get("/hand-rolled")
        def hand_rolled(token: str = Security(deps.oauth2_scheme)):
            return None

        synthetic.include_router(router, prefix="/api/v1/y")
        scopes = route_scopes(synthetic.routes)
        assert scopes[("GET", "/api/v1/y/annotated")].scoped
        assert scopes[("GET", "/api/v1/y/decorator-level")].scoped
        hand_rolled_scope = scopes[("GET", "/api/v1/y/hand-rolled")]
        assert hand_rolled_scope.authenticated and not hand_rolled_scope.scoped and hand_rolled_scope.doors == ()
        assert "unrecognized door" in _describe(hand_rolled_scope)

    @pytest.mark.parametrize("unscoped_first", [True, False])
    def test_a_twin_registration_cannot_vouch_for_the_route(self, unscoped_first):
        """Starlette serves the first ``(METHOD, path)`` match; the guard must not trust whichever came last."""
        synthetic = FastAPI()

        def unscoped(user=Depends(deps.get_current_user)):
            return None

        def scoped(user=Depends(deps.get_current_user), company_id: int = Depends(deps.get_current_company_id)):
            return None

        for endpoint in (unscoped, scoped) if unscoped_first else (scoped, unscoped):
            synthetic.add_api_route("/api/v1/x/twin", endpoint, methods=["GET"])
        scopes = route_scopes(synthetic.routes)
        assert unscoped_route_keys(scopes) == {("GET", "/api/v1/x/twin")}
        assert "registered twice" in scopes[("GET", "/api/v1/x/twin")].where

    def test_routes_the_guard_cannot_see_into_are_reported(self):
        inner = FastAPI()

        @inner.get("/hidden")
        def hidden(user=Depends(deps.get_current_user)):
            return None

        async def plain(request):
            return None

        async def routeless_asgi(scope, receive, send):
            return None

        routes = [
            Mount("/api/v1/sub", app=inner),  # under the prefix
            Mount("/api", app=inner),  # encloses the prefix
            Route("/api/v1/plain", endpoint=plain),  # no dependant to read
            Mount("/", app=routeless_asgi),  # static-files style catch-all: hides no route
            Mount("/static", app=inner),  # neither under nor enclosing the prefix
            Route("/mcp", endpoint=plain),  # the MCP door shape: outside the prefix
        ]
        reported = unclassifiable_routes(routes)
        assert [entry.split(" (")[0] for entry in reported] == [
            "Mount /api/v1/sub",
            "Mount /api",
            "Route /api/v1/plain",
        ]

    def test_a_global_reason_must_cite_only_gates_the_route_reaches(self):
        scopes = route_scopes(_synthetic_app().routes)
        kiosk = ("GET", "/api/v1/x/kiosk")
        user_only = ("GET", "/api/v1/x/user-only")
        assert misjustified_global_routes(scopes, {kiosk: "get_kiosk_or_user: principal.company_id"}) == {}
        assert misjustified_global_routes(scopes, {kiosk: "get_kiosk_station: the station row"}) == {
            kiosk: ["get_kiosk_station"]
        }
        assert misjustified_global_routes(scopes, {user_only: _PLATFORM}) == {user_only: ["require_platform_admin"]}
        assert misjustified_global_routes(scopes, {user_only: _SELF}) == {}, "a reason citing no gate is not checked"


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


def splat_then_assigned(data, company_id):
    record = CalibrationRecord(**data)
    record.company_id = company_id
    return record


def splat_literal(company_id):
    return CalibrationRecord(**{"equipment_id": 1, "company_id": company_id})


def splat_literal_without(company_id):
    return CalibrationRecord(**{"equipment_id": 1, "owner_id": company_id})


def splat_nested(data):
    return CalibrationRecord(**{**data, "equipment_id": 1})


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
            # A splat of an opaque mapping proves nothing (the CustomFieldValue(**parsed) shape) ...
            ("splat", "CalibrationRecord"): False,
            ("splat_nested", "CalibrationRecord"): False,
            ("splat_literal_without", "CalibrationRecord"): False,
            # ... unless the row is then assigned a company, or the literal visibly carries one.
            ("splat_then_assigned", "CalibrationRecord"): True,
            ("splat_literal", "CalibrationRecord"): True,
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

    def test_a_hand_declared_not_null_company_id_is_a_tenant_model_too(self, tenant_classes):
        """``User`` declares ``company_id`` itself rather than through ``TenantMixin``; its rows still need one."""
        from app.models.audit_log import AuditLog
        from app.models.user import User

        assert not issubclass(User, TenantMixin) and User in tenant_classes
        assert AuditLog not in tenant_classes, "a NULLABLE company_id (platform audit events) is not a tenant row"
        source = '''
from app.models.audit_log import AuditLog
from app.models.user import User


def provision(email):
    return User(email=email)


def provision_scoped(email, company_id):
    return User(email=email, company_id=company_id)


def platform_event():
    return AuditLog(action="LOGIN")
'''
        assert _scan(source, tenant_classes) == {("provision", "User"): False, ("provision_scoped", "User"): True}

    def test_relative_and_dotted_module_imports_resolve(self, tenant_classes):
        """``_absolute_module`` turns ``from .x``/``from ..x`` into the real module, for a module and a package."""
        module_source = '''
from .calibration import CalibrationRecord
from ..models.tool_management import ToolCheckout
from app import models
import app.models.quality


def one_dot():
    return CalibrationRecord(equipment_id=1)


def two_dots():
    return ToolCheckout(tool_id=1)


def package_attribute():
    return models.ToolUsageLog(tool_id=1)


def unaliased_dotted():
    return app.models.quality.NonConformanceReport(title="x")
'''
        found = scan_source(
            module_source,
            file="app/models/synthetic.py",
            module="app.models.synthetic",
            is_package=False,
            tenant_classes=tenant_classes,
        )
        assert {(c.function, c.model) for c in found} == {
            ("one_dot", "CalibrationRecord"),
            ("two_dots", "ToolCheckout"),
            ("package_attribute", "ToolUsageLog"),
            ("unaliased_dotted", "NonConformanceReport"),
        }
        package_source = '''
from .calibration import CalibrationRecord


def in_package_init():
    return CalibrationRecord(equipment_id=1)
'''
        found = scan_source(
            package_source,
            file="app/models/__init__.py",
            module="app.models",
            is_package=True,
            tenant_classes=tenant_classes,
        )
        assert [(c.function, c.model) for c in found] == [("in_package_init", "CalibrationRecord")]

    def test_the_innermost_def_owns_a_construction(self, tenant_classes):
        """Methods, nested functions and module scope each key (and guard) their own constructions."""
        source = '''
from app.models.calibration import CalibrationRecord

MODULE_LEVEL = CalibrationRecord(equipment_id=1)


class Service:
    def build(self, company_id):
        record = CalibrationRecord(equipment_id=1)
        record.company_id = company_id
        return record

    def forget(self):
        return CalibrationRecord(equipment_id=1)


def outer(company_id):
    def inner():
        return CalibrationRecord(equipment_id=1)

    record = CalibrationRecord(equipment_id=1)
    record.company_id = company_id
    return inner, record
'''
        assert _scan(source, tenant_classes) == {
            ("<module>", "CalibrationRecord"): False,
            ("build", "CalibrationRecord"): True,
            ("forget", "CalibrationRecord"): False,
            ("inner", "CalibrationRecord"): False,
            ("outer", "CalibrationRecord"): True,
        }


def _construction(function: str, line: int, guarded: bool) -> Construction:
    return Construction("app/x.py", function, "CalibrationRecord", line, guarded)


class TestConstructionAllowlistMechanics:
    def test_an_entry_excuses_exactly_its_occurrences(self):
        key = ("app/x.py", "build", "CalibrationRecord")
        allowlist = {key: UnguardedConstruction("known", occurrences=1)}
        one = [_construction("build", 10, False), _construction("build", 20, True)]
        assert construction_drift(one, allowlist) == ([], {})

        # A second unguarded build of the same model in the same function: same key, so
        # only the count can tell it apart from the one the entry was written for.
        two = one + [_construction("build", 30, False)]
        assert construction_drift(two, allowlist) == ([], {key: (2, 1)})

        # The fix landed: the entry is stale.
        assert construction_drift([_construction("build", 10, True)], allowlist) == ([], {key: (0, 1)})

    def test_an_unnamed_key_is_uncovered(self):
        stray = _construction("other", 40, False)
        uncovered, drift = construction_drift([stray], {})
        assert uncovered == [stray] and drift == {}
