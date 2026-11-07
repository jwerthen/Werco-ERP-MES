"""Apply an AI recommendation's allowlisted action -- only when a named person clicks Accept & apply.

OWNER RULING 3 (2026-11-05): nothing AI-driven executes without a named person clicking
Accept. There is no auto tier, no system actor and no role bypass. The ONLY place in the
app that constructs :class:`AIActionApplier` is ``POST /api/v1/ai/recommendations/{id}/apply``
(``app/api/endpoints/ai_learning.py``), running AS the person who clicked, inside that
request's transaction, after the route's gates (:func:`apply_refusal`) and the atomic claim
of the recommendation. ``tests/test_ai_apply_single_door.py`` holds that structurally.

WHAT THE PERSON SEES IS WHAT RUNS. :func:`resolve_plan` is ONE read-only, tenant-scoped
resolver behind both the list's ``apply_action.summary`` and the applier's execution. It
resolves every id in the stored action against live rows -- never a stored label -- and
returns an :class:`ApplyPlan` whose ``fingerprint`` hashes the resolved facts. The list
hands the fingerprint to the client, the client sends it back, and :meth:`AIActionApplier.apply`
refuses (``ApplyPlanChangedError``) when the facts moved in between.

The module is also the single POLICY seam for the list, record-only accept and apply:
:func:`effective_autonomy`, :func:`is_applyable`, :func:`role_allows`, :func:`apply_refusal`
and :func:`annotate_apply_actions`.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Callable, Dict, FrozenSet, Iterable, Mapping, Optional, Tuple, Union

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.core.credentials import is_interactive_person
from app.db.tenant_filter import tenant_query
from app.models.ai_learning import AIRecommendation
from app.models.part import Part
from app.models.purchasing import POStatus, PurchaseOrder, PurchaseOrderLine, Vendor
from app.models.quality import NCRSource, NCRStatus, NonConformanceReport
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder
from app.models.work_order_blocker import WorkOrderBlocker, WorkOrderBlockerSeverity, WorkOrderBlockerStatus
from app.schemas.purchasing import POCreate, POLineCreate
from app.schemas.work_order_blocker import WorkOrderBlockerUpdate
from app.services.audit_service import AuditService
from app.services.erp_draft_commands import PurchaseOrderNumberConflict, create_purchase_order_command
from app.services.operational_event_service import OperationalEventService
from app.services.quality_numbering import generate_ncr_number
from app.services.work_order_blocker_service import WorkOrderBlockerService
from app.services.work_order_state_service import TERMINAL_WO_STATUSES

logger = logging.getLogger(__name__)

# Actions a person's Accept & apply may run. Anything else is suggest-only by construction.
DEFAULT_APPLY_ALLOWLIST: FrozenSet[str] = frozenset(
    {
        "escalate_blocker",
        "acknowledge_blocker",
        "adjust_work_order_priority",
        "create_draft_ncr",
        "create_draft_po",
    }
)

ROLE_RANK = {
    UserRole.VIEWER: 0,
    UserRole.OPERATOR: 1,
    UserRole.SHIPPING: 1,
    UserRole.QUALITY: 2,
    UserRole.SUPERVISOR: 3,
    UserRole.MANAGER: 4,
    UserRole.ADMIN: 5,
    UserRole.PLATFORM_ADMIN: 5,
}

ACTION_MIN_ROLE: Dict[str, UserRole] = {
    "escalate_blocker": UserRole.SUPERVISOR,
    "acknowledge_blocker": UserRole.SUPERVISOR,
    "adjust_work_order_priority": UserRole.SUPERVISOR,
    "create_draft_ncr": UserRole.QUALITY,  # quality+ (rank 2); supervisor also ok
    "create_draft_po": UserRole.SUPERVISOR,
}

_NCR_ROLES = frozenset(
    {UserRole.QUALITY, UserRole.SUPERVISOR, UserRole.MANAGER, UserRole.ADMIN, UserRole.PLATFORM_ADMIN}
)

# --- Autonomy ------------------------------------------------------------------------------
APPLY_ON_ACCEPT = "apply_on_accept"
SUGGEST_ONLY = "suggest_only"
# Tags written before ruling 3. Rows carrying them are NOT rewritten (that would rewrite the
# evidence of what the old agent did); every decision point reads them through
# ``effective_autonomy`` as apply_on_accept instead -- a person's click, never automatic.
LEGACY_APPLY_AUTONOMY: FrozenSet[str] = frozenset({"auto_execute", "execute_controlled"})

# --- Refusal details (shared by the route, the list annotation and the frontend contract) ---
ALREADY_DECIDED_ACCEPT_DETAIL = (
    "This recommendation is already {status}; it can no longer be accepted. Refresh the Action Inbox."
)
ALREADY_DECIDED_DISMISS_DETAIL = "This recommendation is already {status}; it can no longer be dismissed."
NOT_APPLYABLE_DETAIL = (
    "This recommendation has no action to apply. Accept it to record your decision, or act on the record directly."
)
CREDENTIAL_DETAIL = (
    "Accept & apply needs a person signed in to Werco. API tokens and agents can read, dismiss or snooze a "
    "recommendation but cannot apply it."
)
READ_ONLY_DETAIL = "Read-only company context cannot modify data"
EGRESS_DETAIL = (
    "AI actions are off for this company (AI egress is off), so nothing will be applied. An Admin can turn AI "
    "egress on in Admin Settings > AI Privacy, or you can act on the record directly."
)
ROLE_DETAIL = "Your role ({role}) cannot apply this action: {summary}. It needs {roles}."
MARK_HANDLED_CREDENTIAL_DETAIL = (
    "API tokens and agents cannot mark an action card handled; a person who can apply it must."
)
MARK_HANDLED_ROLE_DETAIL = (
    "Your role ({role}) cannot apply this action, so it stays open for {roles}. You can snooze it or send feedback."
)
PLAN_CHANGED_DETAIL = (
    "This action changed since you loaded it. It would now: {summary}. Review it and click Accept & apply again."
)

_HOLD_DISCLAIMER = "This does not resolve the blocker or lift its hold."
_DEFAULT_WO_PRIORITY = 5  # WorkOrder.priority column default; 1 = highest, 10 = lowest
_EXPEDITE_PRIORITY = 2
_CLOSED_BLOCKER_STATUSES = frozenset({WorkOrderBlockerStatus.RESOLVED.value, WorkOrderBlockerStatus.DISMISSED.value})
_SEVERITY_RANK = {
    WorkOrderBlockerSeverity.LOW.value: 0,
    WorkOrderBlockerSeverity.MEDIUM.value: 1,
    WorkOrderBlockerSeverity.HIGH.value: 2,
    WorkOrderBlockerSeverity.CRITICAL.value: 3,
}
_OPEN_PO_STATUSES = (
    POStatus.DRAFT,
    POStatus.PENDING_APPROVAL,
    POStatus.APPROVED,
    POStatus.SENT,
    POStatus.PARTIAL,
)


class AIActionApplyError(Exception):
    """User-facing apply refusal (missing/closed target, no-op, role, unsupported type).

    The /apply route returns ``str(exc)`` verbatim as a 409 detail and rolls the whole
    transaction back, including the claim of the recommendation.
    """


class ApplyPlanChangedError(AIActionApplyError):
    """The resolved plan no longer matches the fingerprint the person was shown."""


# =============================================================================================
# Policy helpers -- ONE implementation for the list, record-only accept and /apply
# =============================================================================================


def _action_dict(action: Any) -> Mapping[str, Any]:
    return action if isinstance(action, Mapping) else {}


def action_type_of(action: Any) -> str:
    return str(_action_dict(action).get("type") or "").strip()


def effective_autonomy(action: Any) -> str:
    """``apply_on_accept`` for the canonical tag and for the legacy auto tags; else ``suggest_only``."""
    raw = str(_action_dict(action).get("autonomy") or "").strip().lower()
    if raw == APPLY_ON_ACCEPT or raw in LEGACY_APPLY_AUTONOMY:
        return APPLY_ON_ACCEPT
    return SUGGEST_ONLY


def is_applyable(recommendation: AIRecommendation) -> bool:
    """An ACTION CARD: pending, allowlisted type, and (effectively) apply_on_accept."""
    if str(recommendation.status or "").strip().lower() != "pending":
        return False
    action = recommendation.suggested_action
    return action_type_of(action) in DEFAULT_APPLY_ALLOWLIST and effective_autonomy(action) == APPLY_ON_ACCEPT


def role_allows(user: User, action_type: str) -> bool:
    """Could this person's role apply ``action_type``? (Quality+ for NCR, Supervisor+ otherwise.)"""
    if getattr(user, "is_superuser", False):
        return True
    if action_type == "create_draft_ncr":
        return user.role in _NCR_ROLES
    min_role = ACTION_MIN_ROLE.get(action_type, UserRole.MANAGER)
    return ROLE_RANK.get(user.role, 0) >= ROLE_RANK.get(min_role, 99)


def required_roles_label(action_type: str) -> str:
    if action_type == "create_draft_ncr":
        return "Quality, Supervisor, Manager or Admin"
    if ACTION_MIN_ROLE.get(action_type, UserRole.MANAGER) == UserRole.SUPERVISOR:
        return "Supervisor, Manager or Admin"
    return "Manager or Admin"


def role_label(user: User) -> str:
    role = getattr(user, "role", None)
    return str(getattr(role, "value", role) or "unknown")


# =============================================================================================
# The plan -- what the person sees is what runs
# =============================================================================================


@dataclass(frozen=True)
class ApplyPlan:
    """The resolved, read-only description of what Accept & apply would do right now.

    ``facts`` holds only resolved ids and the displayed current/target values (never a row
    version), so an unrelated edit to the same record does not invalidate the plan.
    """

    action_type: str
    summary: str
    facts: Dict[str, Any] = field(default_factory=dict)
    warnings: Tuple[str, ...] = ()
    blocked_reason: Optional[str] = None

    @property
    def blocked(self) -> bool:
        return self.blocked_reason is not None

    @property
    def fingerprint(self) -> str:
        payload = json.dumps({"type": self.action_type, **self.facts}, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _int_id(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _ref(action: Mapping[str, Any], key: str, target_type: Optional[str], target_id: Any, expected: str):
    """Resolve an id from the action, falling back to the target. Returns (named, id).

    ``named`` is True when the action or target names the reference at all, so a crafted,
    unparseable value is reported as "not found" rather than silently ignored.
    """
    raw = action.get(key)
    if raw not in (None, ""):
        return True, _int_id(raw)
    if target_type == expected and target_id is not None:
        return True, _int_id(target_id)
    return False, None


def _positive_number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _status_text(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def _effective_priority(work_order: WorkOrder) -> int:
    return int(work_order.priority) if work_order.priority is not None else _DEFAULT_WO_PRIORITY


def _wo_is_live_open(work_order: Optional[WorkOrder]) -> bool:
    return bool(work_order is not None and not work_order.is_deleted and work_order.status not in TERMINAL_WO_STATUSES)


def _join_clauses(clauses: Iterable[str]) -> str:
    items = [c for c in clauses if c]
    if not items:
        return ""
    text = items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]
    return text[0].upper() + text[1:]


def _blocked(action_type: str, summary: str, reason: str, facts: Optional[Dict[str, Any]] = None) -> ApplyPlan:
    return ApplyPlan(action_type=action_type, summary=summary, facts=dict(facts or {}), blocked_reason=reason)


def _plan_adjust_priority(db: Session, company_id: int, action: Mapping[str, Any], ctx: Dict[str, Any]) -> ApplyPlan:
    kind = "adjust_work_order_priority"
    named, wo_id = _ref(action, "work_order_id", ctx["target_type"], ctx["target_id"], "work_order")
    if not named:
        return _blocked(kind, "Change a work order's priority", "This recommendation does not name a work order")
    work_order = tenant_query(db, WorkOrder, company_id).filter(WorkOrder.id == wo_id).first() if wo_id else None
    if work_order is None:
        return _blocked(kind, "Change a work order's priority", "Work order not found")
    number = work_order.work_order_number
    requested = _int_id(action.get("priority")) or _EXPEDITE_PRIORITY
    new_priority = max(1, min(10, requested))
    current = _effective_priority(work_order)
    facts = {
        "work_order_id": work_order.id,
        "work_order_number": number,
        "current_priority": work_order.priority,
        "new_priority": new_priority,
    }
    summary = f"Raise {number} priority from P{current} to P{new_priority}"
    if work_order.is_deleted:
        return _blocked(kind, summary, f"Work order {number} has been deleted", facts)
    if work_order.status in TERMINAL_WO_STATUSES:
        return _blocked(kind, summary, "Cannot change priority on a terminal work order", facts)
    if current <= new_priority:
        # Never demote, never a no-op: a P1 job with a "P2" suggestion stays P1.
        return _blocked(
            kind,
            summary,
            f"{number} is already P{current} (the suggestion was P{new_priority}); nothing to apply",
            facts,
        )
    return ApplyPlan(action_type=kind, summary=summary, facts=facts)


def _blocker_facts(blocker: WorkOrderBlocker) -> Dict[str, Any]:
    return {
        "blocker_id": blocker.id,
        "blocker_title": blocker.title,
        "blocker_status": blocker.status,
        "blocker_severity": blocker.severity,
    }


def _plan_escalate_blocker(db: Session, company_id: int, action: Mapping[str, Any], ctx: Dict[str, Any]) -> ApplyPlan:
    kind = "escalate_blocker"
    generic = "Escalate a work-order blocker"
    named, blocker_id = _ref(action, "blocker_id", ctx["target_type"], ctx["target_id"], "work_order_blocker")
    if not named:
        return _blocked(kind, generic, "This recommendation does not name a blocker")
    blocker = (
        tenant_query(db, WorkOrderBlocker, company_id).filter(WorkOrderBlocker.id == blocker_id).first()
        if blocker_id
        else None
    )
    if blocker is None:
        return _blocked(kind, generic, "Blocker not found")
    title = blocker.title
    facts = _blocker_facts(blocker)
    if blocker.status in _CLOSED_BLOCKER_STATUSES:
        return _blocked(kind, generic, f"Blocker '{title}' is already {blocker.status}; nothing to escalate", facts)

    acknowledge = blocker.status == WorkOrderBlockerStatus.OPEN.value
    high_rank = _SEVERITY_RANK[WorkOrderBlockerSeverity.HIGH.value]
    raise_severity = _SEVERITY_RANK.get(blocker.severity or "", 0) < high_rank
    work_order = (
        tenant_query(db, WorkOrder, company_id).filter(WorkOrder.id == blocker.work_order_id).first()
        if blocker.work_order_id
        else None
    )
    current = _effective_priority(work_order) if work_order is not None else None
    expedite = bool(_wo_is_live_open(work_order) and current is not None and current > _EXPEDITE_PRIORITY)
    number = work_order.work_order_number if work_order is not None else None
    facts.update(
        {
            "acknowledge": acknowledge,
            "new_severity": WorkOrderBlockerSeverity.HIGH.value if raise_severity else None,
            "work_order_id": work_order.id if work_order is not None else None,
            "work_order_number": number,
            "current_priority": work_order.priority if work_order is not None else None,
            "expedite_to": _EXPEDITE_PRIORITY if expedite else None,
        }
    )

    clauses = []
    if acknowledge:
        clauses.append(f"acknowledge blocker '{title}'")
    if raise_severity:
        if acknowledge:
            clauses.append(f"raise its severity from {blocker.severity} to high")
        else:
            clauses.append(f"raise blocker '{title}' severity from {blocker.severity} to high")
    if expedite:
        clauses.append(f"expedite {number} from P{current} to P{_EXPEDITE_PRIORITY}")

    if not clauses:
        if work_order is None:
            wo_clause = ""
        elif work_order.is_deleted:
            wo_clause = f" and {number} has been deleted"
        elif work_order.status in TERMINAL_WO_STATUSES:
            wo_clause = f" and {number} is {_status_text(work_order.status)}"
        else:
            wo_clause = f" and {number} is already P{current}"
        reason = f"Blocker '{title}' is already acknowledged at {blocker.severity}{wo_clause}; nothing to escalate"
        return _blocked(kind, generic, reason, facts)

    summary = f"{_join_clauses(clauses)}. {_HOLD_DISCLAIMER}"
    return ApplyPlan(action_type=kind, summary=summary, facts=facts)


def _plan_acknowledge_blocker(
    db: Session, company_id: int, action: Mapping[str, Any], ctx: Dict[str, Any]
) -> ApplyPlan:
    kind = "acknowledge_blocker"
    generic = "Acknowledge a work-order blocker"
    named, blocker_id = _ref(action, "blocker_id", ctx["target_type"], ctx["target_id"], "work_order_blocker")
    if not named:
        return _blocked(kind, generic, "This recommendation does not name a blocker")
    blocker = (
        tenant_query(db, WorkOrderBlocker, company_id).filter(WorkOrderBlocker.id == blocker_id).first()
        if blocker_id
        else None
    )
    if blocker is None:
        return _blocked(kind, generic, "Blocker not found")
    title = blocker.title
    facts = _blocker_facts(blocker)
    facts["acknowledge"] = True
    if blocker.status in _CLOSED_BLOCKER_STATUSES:
        return _blocked(kind, generic, f"Blocker '{title}' is already {blocker.status}; nothing to acknowledge", facts)
    if blocker.status == WorkOrderBlockerStatus.ACKNOWLEDGED.value:
        return _blocked(kind, generic, f"Blocker '{title}' is already acknowledged", facts)
    return ApplyPlan(action_type=kind, summary=f"Acknowledge blocker '{title}'. {_HOLD_DISCLAIMER}", facts=facts)


def _plan_draft_ncr(db: Session, company_id: int, action: Mapping[str, Any], ctx: Dict[str, Any]) -> ApplyPlan:
    kind = "create_draft_ncr"
    generic = "Open an in-process NCR"
    facts: Dict[str, Any] = {"part_id": None, "part_number": None, "work_order_id": None, "work_order_number": None}

    named_part, part_id = _ref(action, "part_id", ctx["target_type"], ctx["target_id"], "part")
    if named_part:
        part = tenant_query(db, Part, company_id).filter(Part.id == part_id).first() if part_id else None
        if part is None:
            return _blocked(kind, generic, "Part not found")
        facts.update(part_id=part.id, part_number=part.part_number)
        if part.is_deleted:
            return _blocked(kind, generic, f"Part {part.part_number} has been deleted", facts)

    raw_wo = action.get("work_order_id")
    if raw_wo not in (None, ""):
        wo_id = _int_id(raw_wo)
        work_order = tenant_query(db, WorkOrder, company_id).filter(WorkOrder.id == wo_id).first() if wo_id else None
        if work_order is None:
            return _blocked(kind, generic, "Work order not found", facts)
        facts.update(work_order_id=work_order.id, work_order_number=work_order.work_order_number)
        if work_order.is_deleted:
            return _blocked(kind, generic, f"Work order {work_order.work_order_number} has been deleted", facts)

    raw_qty = action.get("quantity_affected")
    quantity = 1.0 if raw_qty in (None, "") else _positive_number(raw_qty)
    if quantity is None:
        return _blocked(
            kind,
            generic,
            f"The suggested NCR quantity ({raw_qty}) is not a positive number; open the NCR in Quality",
            facts,
        )

    title = str(action.get("title") or ctx.get("fallback_title") or "")[:200]
    if len(title) < 5:
        title = f"Quality review: {title}"
    description = str(action.get("description") or ctx.get("fallback_description") or ctx.get("fallback_title") or "")
    if len(description) < 5:
        description = f"{description} (opened from AI quality recommendation)"
    facts.update(quantity_affected=quantity, title=title, description=description[:4000])

    labels = [label for label in (facts["part_number"], facts["work_order_number"]) if label]
    target = f" for {' / '.join(labels)}" if labels else ""
    summary = f"Open an in-process NCR{target}: qty {quantity:g} — '{title}'"
    return ApplyPlan(action_type=kind, summary=summary, facts=facts)


def _plan_draft_po(db: Session, company_id: int, action: Mapping[str, Any], ctx: Dict[str, Any]) -> ApplyPlan:
    kind = "create_draft_po"
    generic = "Create a DRAFT purchase order"
    named, part_id = _ref(action, "part_id", ctx["target_type"], ctx["target_id"], "part")
    if not named:
        return _blocked(kind, generic, "This recommendation does not name a part; create the PO in Purchasing")
    part = tenant_query(db, Part, company_id).filter(Part.id == part_id).first() if part_id else None
    if part is None:
        return _blocked(kind, generic, "Part not found")
    number = part.part_number
    facts: Dict[str, Any] = {"part_id": part.id, "part_number": number}
    if part.is_deleted:
        return _blocked(kind, generic, f"Part {number} has been deleted", facts)

    # The part's CURRENT primary supplier -- never the mint-time vendor snapshot an old card
    # carried, and never an arbitrary "first vendor on file": a person can only meaningfully
    # approve a supplier the shop actually chose.
    vendor_id = part.primary_supplier_id
    if not vendor_id:
        return _blocked(
            kind,
            generic,
            f"{number} has no primary supplier; set one on the part, or create the PO in Purchasing",
            facts,
        )
    # Read RAW (tenant-scoped, no tombstone filter) so the two refusals get different
    # messages: "not found for this company" sends someone hunting a tenancy bug, while the
    # real remedy for a removed supplier is to repoint the part or restore the vendor.
    # Deleted is tested first because delete also forces is_active = False.
    vendor = tenant_query(db, Vendor, company_id).filter(Vendor.id == vendor_id).first()
    if vendor is None:
        return _blocked(kind, generic, "Vendor not found for this company", facts)
    facts.update(vendor_id=vendor.id, vendor_name=vendor.name, vendor_code=vendor.code)
    if vendor.is_deleted:
        return _blocked(
            kind,
            generic,
            f"Vendor {vendor.name} has been removed; update the part's primary supplier or restore the vendor",
            facts,
        )
    if not vendor.is_active:
        return _blocked(
            kind,
            generic,
            f"Vendor {vendor.name} is inactive; reactivate it or update the part's primary supplier",
            facts,
        )

    quantity = _positive_number(action.get("suggested_qty") or part.reorder_quantity)
    if quantity is None:
        return _blocked(kind, generic, f"{number} has no reorder quantity; create the PO in Purchasing", facts)
    unit_price = float(part.standard_cost or part.material_cost or 0.0)
    facts.update(quantity=quantity, unit_price=unit_price)

    open_numbers = [
        row[0]
        for row in db.query(PurchaseOrder.po_number)
        .join(PurchaseOrderLine, PurchaseOrderLine.purchase_order_id == PurchaseOrder.id)
        .filter(
            PurchaseOrder.company_id == company_id,
            PurchaseOrderLine.company_id == company_id,
            PurchaseOrder.is_deleted == False,  # noqa: E712
            PurchaseOrder.status.in_(_OPEN_PO_STATUSES),
            PurchaseOrderLine.part_id == part.id,
        )
        .distinct()
        .order_by(PurchaseOrder.po_number)
        .limit(6)
        .all()
    ]
    warnings: Tuple[str, ...] = ()
    if open_numbers:
        listed = ", ".join(open_numbers[:5]) + (" and more" if len(open_numbers) > 5 else "")
        warnings = (f"{number} already has open PO(s): {listed}",)

    summary = (
        f"Create a DRAFT purchase order to {vendor.name} ({vendor.code}) for {quantity:g} x {number} "
        f"at ${unit_price:.2f} each. Nothing is sent to the vendor."
    )
    return ApplyPlan(action_type=kind, summary=summary, facts=facts, warnings=warnings)


_PLAN_RESOLVERS: Dict[str, Callable[[Session, int, Mapping[str, Any], Dict[str, Any]], ApplyPlan]] = {
    "adjust_work_order_priority": _plan_adjust_priority,
    "escalate_blocker": _plan_escalate_blocker,
    "acknowledge_blocker": _plan_acknowledge_blocker,
    "create_draft_ncr": _plan_draft_ncr,
    "create_draft_po": _plan_draft_po,
}


def resolve_plan(
    db: Session,
    company_id: int,
    action: Any,
    *,
    target_entity_type: Optional[str] = None,
    target_entity_id: Optional[int] = None,
    fallback_title: Optional[str] = None,
    fallback_description: Optional[str] = None,
) -> ApplyPlan:
    """Resolve what applying ``action`` would do NOW. Read-only, tenant-scoped, never flushes."""
    action = _action_dict(action)
    action_type = action_type_of(action)
    resolver = _PLAN_RESOLVERS.get(action_type)
    if resolver is None:
        return _blocked(action_type, "No action", NOT_APPLYABLE_DETAIL)
    ctx = {
        "target_type": target_entity_type,
        "target_id": target_entity_id,
        "fallback_title": fallback_title,
        "fallback_description": fallback_description,
    }
    return resolver(db, company_id, action, ctx)


def plan_for_recommendation(db: Session, company_id: int, recommendation: AIRecommendation) -> ApplyPlan:
    return resolve_plan(
        db,
        company_id,
        recommendation.suggested_action,
        target_entity_type=recommendation.target_entity_type,
        target_entity_id=recommendation.target_entity_id,
        fallback_title=recommendation.title,
        fallback_description=recommendation.summary,
    )


# =============================================================================================
# Request policy -- the /apply gates, in order, and the list's per-row annotation
# =============================================================================================


@dataclass(frozen=True)
class ApplyRefusal:
    status_code: int
    detail: str
    kind: str  # state | not_applyable | credential | read_only | egress | role


_Lazy = Union[bool, str, Callable[[], Any], None]


def _resolve_lazy(value: _Lazy) -> Any:
    return value() if callable(value) else value


def apply_refusal(
    recommendation: AIRecommendation,
    user: User,
    *,
    egress_allowed: _Lazy,
    summary: _Lazy = None,
) -> Optional[ApplyRefusal]:
    """The /apply request gates before any write, in the documented order; None = may proceed.

    ``egress_allowed`` and ``summary`` may be zero-argument callables so the egress read and
    the plan resolution happen only when an earlier gate has not already refused.
    The target/fingerprint gates (409) run inside :meth:`AIActionApplier.apply`.
    """
    status = str(recommendation.status or "").strip().lower()
    if status != "pending":
        return ApplyRefusal(409, ALREADY_DECIDED_ACCEPT_DETAIL.format(status=status), "state")
    if not is_applyable(recommendation):
        return ApplyRefusal(409, NOT_APPLYABLE_DETAIL, "not_applyable")
    if not is_interactive_person(user):
        return ApplyRefusal(403, CREDENTIAL_DETAIL, "credential")
    if getattr(user, "_read_only_company_context", False):
        return ApplyRefusal(403, READ_ONLY_DETAIL, "read_only")
    if not _resolve_lazy(egress_allowed):
        return ApplyRefusal(403, EGRESS_DETAIL, "egress")
    action_type = action_type_of(recommendation.suggested_action)
    if not role_allows(user, action_type):
        text = str(_resolve_lazy(summary) or "this action").rstrip(".")
        detail = ROLE_DETAIL.format(role=role_label(user), summary=text, roles=required_roles_label(action_type))
        return ApplyRefusal(403, detail, "role")
    return None


def mark_handled_refusal(recommendation: AIRecommendation, user: User) -> Optional[ApplyRefusal]:
    """Record-only Accept ("Mark handled") on an ACTION CARD needs a person who could apply it.

    Suggest-only cards stay acceptable by anyone. Egress is not required: nothing executes.
    """
    if not is_applyable(recommendation):
        return None
    if not is_interactive_person(user):
        return ApplyRefusal(403, MARK_HANDLED_CREDENTIAL_DETAIL, "credential")
    action_type = action_type_of(recommendation.suggested_action)
    if not role_allows(user, action_type):
        detail = MARK_HANDLED_ROLE_DETAIL.format(role=role_label(user), roles=required_roles_label(action_type))
        return ApplyRefusal(403, detail, "role")
    return None


def annotate_apply_actions(
    db: Session,
    company_id: int,
    recommendations: Iterable[AIRecommendation],
    user: User,
    egress_fn: Callable[[], bool],
) -> None:
    """Attach ``apply_action`` to every row (None unless it is a pending action card).

    Egress is read lazily, at most once per request, and only when a row reaches that gate.
    """
    egress_cache: Dict[str, bool] = {}

    def egress() -> bool:
        if "value" not in egress_cache:
            egress_cache["value"] = bool(egress_fn())
        return egress_cache["value"]

    interactive = is_interactive_person(user)
    read_only = bool(getattr(user, "_read_only_company_context", False))
    for recommendation in recommendations:
        if not is_applyable(recommendation):
            setattr(recommendation, "apply_action", None)
            continue
        plan = plan_for_recommendation(db, company_id, recommendation)
        refusal = apply_refusal(recommendation, user, egress_allowed=egress, summary=plan.summary)
        if refusal is not None:
            blocked_kind: Optional[str] = refusal.kind
            blocked_reason: Optional[str] = refusal.detail
        elif plan.blocked:
            blocked_kind, blocked_reason = "target", plan.blocked_reason
        else:
            blocked_kind, blocked_reason = None, None
        can_mark_handled = (
            interactive
            and not read_only
            and role_allows(user, plan.action_type)
            and blocked_kind in (None, "egress", "target")
        )
        setattr(
            recommendation,
            "apply_action",
            {
                "action_type": plan.action_type,
                "summary": plan.summary,
                "warnings": list(plan.warnings),
                "fingerprint": plan.fingerprint,
                "can_apply": blocked_kind is None,
                "can_mark_handled": bool(can_mark_handled),
                "blocked_reason": blocked_reason,
                "blocked_kind": blocked_kind,
            },
        )


# =============================================================================================
# Provenance -- every audit row an applied action writes says it came from a recommendation
# =============================================================================================

_LOG_SIGNATURE = inspect.signature(AuditService.log)


class _AIProvenanceAudit(AuditService):
    """Delegate that stamps AI provenance into ``extra_data`` of every row it writes.

    Mirrors the ``_RequiredCreateAudit`` delegate in ``hank_task_service`` but stays
    BEST-EFFORT (the delegate's own contract): the draft-NCR path is a documented
    best-effort audit writer. Only ``log`` is overridden; ``log_create`` / ``log_update`` /
    ``log_status_change`` / ``log_required`` are inherited and all funnel into it, so no
    helper can skip the marker. The provenance keys win a key collision.
    """

    def __init__(self, delegate: AuditService, provenance: Dict[str, Any]):  # noqa: D107
        # Deliberately not calling super().__init__: every write goes through the delegate,
        # which carries the request, the tenant tag and the API-token credential marker.
        self._delegate = delegate
        self._provenance = dict(provenance)
        self.db = delegate.db
        self.user = delegate.user
        self.request = getattr(delegate, "request", None)
        self.company_id = getattr(delegate, "company_id", None)

    def log(self, *args: Any, **kwargs: Any):  # type: ignore[override]
        bound = _LOG_SIGNATURE.bind(self._delegate, *args, **kwargs)
        arguments = dict(bound.arguments)
        arguments.pop("self", None)
        arguments["extra_data"] = {**(arguments.get("extra_data") or {}), **self._provenance}
        return self._delegate.log(**arguments)


# =============================================================================================
# The applier -- constructed only by the /apply route, as the person who clicked
# =============================================================================================


class AIActionApplier:
    def __init__(
        self,
        db: Session,
        *,
        company_id: int,
        user: User,
        audit: Optional[AuditService] = None,
    ):
        self.db = db
        self.company_id = company_id
        self.user = user
        # Every applied mutation is audited: a caller without a request-scoped service
        # still gets one bound to the acting user.
        self.audit = audit if audit is not None else AuditService(db, user)

    def apply(self, recommendation: AIRecommendation, *, expected_fingerprint: Optional[str] = None) -> Dict[str, Any]:
        """Run the recommendation's allowlisted action as ``self.user``. Raises AIActionApplyError.

        Flushes but never commits; the caller owns the transaction and rolls it back on error.
        """
        if recommendation.company_id != self.company_id:
            raise AIActionApplyError("Recommendation not found for this company")

        action = dict(_action_dict(recommendation.suggested_action))
        action_type = action_type_of(action)
        if not action_type:
            raise AIActionApplyError("Recommendation has no suggested action type")
        if action_type not in DEFAULT_APPLY_ALLOWLIST:
            raise AIActionApplyError(f"Action type '{action_type}' is not allowlisted for apply")
        if effective_autonomy(action) != APPLY_ON_ACCEPT:
            raise AIActionApplyError("This recommendation is suggest-only; there is no action to apply")
        if not role_allows(self.user, action_type):
            raise AIActionApplyError(f"Insufficient role to apply '{action_type}'")

        handlers = {
            "escalate_blocker": self._escalate_blocker,
            "acknowledge_blocker": self._acknowledge_blocker,
            "adjust_work_order_priority": self._adjust_priority,
            "create_draft_ncr": self._create_draft_ncr,
            "create_draft_po": self._create_draft_po,
        }
        handler = handlers.get(action_type)
        if handler is None:
            raise AIActionApplyError(f"No applier registered for '{action_type}'")

        plan = plan_for_recommendation(self.db, self.company_id, recommendation)
        if plan.blocked:
            raise AIActionApplyError(plan.blocked_reason)
        if expected_fingerprint is not None and expected_fingerprint != plan.fingerprint:
            raise ApplyPlanChangedError(PLAN_CHANGED_DETAIL.format(summary=plan.summary.rstrip(".")))

        audit = _AIProvenanceAudit(
            self.audit,
            {
                "source": "ai_recommendation",
                "recommendation_id": recommendation.id,
                "recommendation_type": recommendation.recommendation_type,
                "source_module": recommendation.source_module,
                "action_type": action_type,
                "approved_by": self.user.id,
                "plan_fingerprint": plan.fingerprint,
                "prompt_version": recommendation.prompt_version,
                "model_version": recommendation.model_version,
            },
        )
        result = handler(recommendation, plan, audit)
        work_order_id = plan.facts.get("work_order_id")
        OperationalEventService(self.db).emit_best_effort(
            company_id=self.company_id,
            event_type="ai_recommendation_applied",
            source_module=recommendation.source_module,
            entity_type=recommendation.target_entity_type,
            entity_id=recommendation.target_entity_id,
            work_order_id=work_order_id if isinstance(work_order_id, int) else None,
            user_id=self.user.id,
            severity="info",
            event_payload={
                "recommendation_id": recommendation.id,
                "action_type": action_type,
                "plan_fingerprint": plan.fingerprint,
                "result": result,
            },
        )
        return {"action_type": action_type, "summary": plan.summary, **result}

    # -- handlers: flush right after each ORM mutation and BEFORE its audit call, so a
    # version_id_col StaleDataError surfaces here (-> app-wide 409) instead of inside
    # AuditService.log's catch-all, which would leave a pending-rollback session.

    def _who(self) -> str:
        return getattr(self.user, "full_name", None) or getattr(self.user, "email", None) or f"user #{self.user.id}"

    def _work_order(self, work_order_id: int) -> WorkOrder:
        work_order = tenant_query(self.db, WorkOrder, self.company_id).filter(WorkOrder.id == work_order_id).first()
        if work_order is None:  # pragma: no cover - the plan resolved it in this transaction
            raise AIActionApplyError("Work order not found")
        return work_order

    def _set_priority(self, work_order: WorkOrder, new_priority: int, audit: AuditService, description: str) -> int:
        old = work_order.priority
        work_order.priority = new_priority
        work_order.updated_at = datetime.utcnow()
        self.db.flush()
        audit.log_update(
            resource_type="work_order",
            resource_id=work_order.id,
            resource_identifier=work_order.work_order_number,
            old_values={"priority": old},
            new_values={"priority": new_priority},
            description=description,
        )
        return old

    def _adjust_priority(self, recommendation: AIRecommendation, plan: ApplyPlan, audit: AuditService):
        facts = plan.facts
        work_order = self._work_order(facts["work_order_id"])
        new_priority = int(facts["new_priority"])
        old = self._set_priority(
            work_order,
            new_priority,
            audit,
            f"AI recommendation #{recommendation.id} set priority {facts.get('current_priority')}→{new_priority} "
            f"(applied by {self._who()})",
        )
        return {
            "work_order_id": work_order.id,
            "work_order_number": work_order.work_order_number,
            "old_priority": old,
            "new_priority": new_priority,
        }

    def _escalate_blocker(self, recommendation: AIRecommendation, plan: ApplyPlan, audit: AuditService):
        facts = plan.facts
        blocker_id = int(facts["blocker_id"])
        changes: Dict[str, Any] = {}
        if facts.get("acknowledge"):
            changes["status"] = WorkOrderBlockerStatus.ACKNOWLEDGED
        if facts.get("new_severity"):
            changes["severity"] = WorkOrderBlockerSeverity.HIGH
        if changes:
            # The service writes the status-change row when the status changes; it audits
            # nothing else, so a severity change gets its own explicit row below.
            blocker = WorkOrderBlockerService(self.db).update_blocker(
                company_id=self.company_id,
                user=self.user,
                blocker_id=blocker_id,
                data=WorkOrderBlockerUpdate(**changes),
                audit=audit,
            )
            self.db.flush()
        else:
            blocker = (
                tenant_query(self.db, WorkOrderBlocker, self.company_id).filter(WorkOrderBlocker.id == blocker_id).one()
            )
        if facts.get("new_severity"):
            audit.log_update(
                resource_type="work_order_blocker",
                resource_id=blocker.id,
                resource_identifier=blocker.title,
                old_values={"severity": facts.get("blocker_severity")},
                new_values={"severity": WorkOrderBlockerSeverity.HIGH.value},
                description=(
                    f"Blocker '{blocker.title}' severity raised to high by AI recommendation "
                    f"#{recommendation.id}, applied by {self._who()}"
                ),
            )
        result: Dict[str, Any] = {"blocker_id": blocker.id, "status": blocker.status, "severity": blocker.severity}
        if facts.get("expedite_to"):
            work_order = self._work_order(facts["work_order_id"])
            new_priority = int(facts["expedite_to"])
            old = self._set_priority(
                work_order,
                new_priority,
                audit,
                f"Priority expedited by AI escalate_blocker on rec #{recommendation.id}, applied by {self._who()}",
            )
            result.update(work_order_id=work_order.id, old_priority=old, new_priority=new_priority)
        return result

    def _acknowledge_blocker(self, recommendation: AIRecommendation, plan: ApplyPlan, audit: AuditService):
        blocker = WorkOrderBlockerService(self.db).update_blocker(
            company_id=self.company_id,
            user=self.user,
            blocker_id=int(plan.facts["blocker_id"]),
            data=WorkOrderBlockerUpdate(status=WorkOrderBlockerStatus.ACKNOWLEDGED),
            audit=audit,
        )
        self.db.flush()
        return {"blocker_id": blocker.id, "status": blocker.status}

    def _create_draft_ncr(self, recommendation: AIRecommendation, plan: ApplyPlan, audit: AuditService):
        facts = plan.facts
        ncr = NonConformanceReport(
            ncr_number=generate_ncr_number(self.db, self.company_id),
            part_id=facts.get("part_id"),
            work_order_id=facts.get("work_order_id"),
            quantity_affected=float(facts["quantity_affected"]),
            source=NCRSource.IN_PROCESS,
            status=NCRStatus.OPEN,
            title=facts["title"],
            description=facts["description"],
            detected_by=self.user.id,
            detected_date=date.today(),
            company_id=self.company_id,
        )
        self.db.add(ncr)
        self.db.flush()
        audit.log_create(
            resource_type="ncr",
            resource_id=ncr.id,
            resource_identifier=ncr.ncr_number,
            new_values=ncr,
            description=f"Draft NCR created from AI recommendation #{recommendation.id}, applied by {self._who()}",
        )
        # Same event and payload as quality_record_service.create_ncr, so the ncr.created
        # notification fires for an AI-sourced NCR exactly as for a hand-raised one.
        OperationalEventService(self.db).emit_best_effort(
            company_id=self.company_id,
            event_type="ncr_created",
            source_module="quality",
            entity_type="ncr",
            entity_id=ncr.id,
            work_order_id=ncr.work_order_id,
            user_id=self.user.id,
            severity="high" if ncr.source == NCRSource.CUSTOMER_RETURN else "medium",
            event_payload={
                "ncr_number": ncr.ncr_number,
                "title": ncr.title,
                "source": _status_text(ncr.source),
                "status": _status_text(ncr.status),
                "disposition": _status_text(ncr.disposition) or None,
                "part_id": ncr.part_id,
                "quantity_affected": float(ncr.quantity_affected or 0),
            },
        )
        return {"ncr_id": ncr.id, "ncr_number": ncr.ncr_number, "status": "open"}

    def _create_draft_po(self, recommendation: AIRecommendation, plan: ApplyPlan, audit: AuditService):
        facts = plan.facts
        try:
            po_in = POCreate(
                vendor_id=int(facts["vendor_id"]),
                notes=f"Draft from AI recommendation #{recommendation.id}",
                lines=[
                    POLineCreate(
                        part_id=int(facts["part_id"]),
                        quantity_ordered=Decimal(str(facts["quantity"])),
                        unit_price=Decimal(str(facts["unit_price"])),
                    )
                ],
            )
        except ValidationError as exc:
            raise AIActionApplyError(f"The draft PO is not valid: {exc.errors()[0].get('msg')}") from exc
        try:
            # Commit-free: the advisory-locked PO number, the live+active vendor check, the
            # full log_create snapshot and the purchase_order_created event -- the same
            # command the Purchasing route and Hank use.
            po = create_purchase_order_command(self.db, po_in, self.user, self.company_id, audit)
        except PurchaseOrderNumberConflict as exc:
            raise AIActionApplyError(f"Draft PO number {exc.po_number} already exists; try again") from exc
        except HTTPException as exc:
            raise AIActionApplyError(str(exc.detail)) from exc
        return {
            "purchase_order_id": po.id,
            "po_number": po.po_number,
            "vendor_id": po.vendor_id,
            "part_id": int(facts["part_id"]),
            "quantity": float(facts["quantity"]),
            "status": "draft",
        }
