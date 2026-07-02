"""Werco Copilot v1 — read-only, tool-use chat over the tenant's own ERP data.

Design rules (non-negotiable):

- **Read-only.** Every tool is a thin wrapper over an existing read path; no
  tool mutates anything. The copilot answers questions — it does not act.
- **Tenant scope is server-side.** ``company_id`` comes from the authenticated
  session and is injected into every tool handler by :meth:`CopilotService.execute_tool`.
  Tool input schemas never include a tenant identifier, and any extra keys the
  model supplies (including ``company_id``) are dropped before dispatch.
- **RBAC mirrors the source endpoints.** Each tool documents the endpoint it
  wraps and that endpoint's access rule. All v1 tools wrap any-authenticated
  endpoints. Data minimization: the ``search_erp`` tool excludes the employee
  directory entirely (``_SEARCH_ERP_TYPES`` carries no ``user`` type), so
  employee names/emails never enter model prompts — the Admin/Manager-gated
  user results remain available on ``GET /search`` only.
  ``CopilotToolSpec.allowed_roles`` exists for future role-restricted tools:
  restricted tools are not registered for (and politely refused to) other roles.
- **All Anthropic calls go through ``run_llm_task``** — one usage-telemetry row
  per loop iteration (task ``copilot_chat``), prompt caching on the stable
  prefix (deterministic tool schemas + versioned system prompt).
- **Bounded.** At most :data:`COPILOT_MAX_TOOL_ROUNDS` tool rounds per turn and
  a per-call output-token cap; on cap the model is forced (``tool_choice:
  none``) to answer from what it has gathered.
- **Every turn is recorded** through ``AILearningService.record_interaction``
  (source_module ``copilot``; that service's redaction rules apply).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Callable, Dict, FrozenSet, Generator, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.core.time_utils import to_utc_iso
from app.models.inventory import InventoryItem
from app.models.part import Part
from app.models.quote import Quote, QuoteStatus
from app.models.user import User, UserRole
from app.models.work_center import WorkCenter
from app.models.work_order import WorkOrder, WorkOrderStatus
from app.models.work_order_blocker import WorkOrderBlockerStatus
from app.schemas.ai_learning import AIEventType, AIInteractionEventCreate
from app.services.ai_context_service import AIContextService
from app.services.ai_learning_service import AILearningService
from app.services.llm_client import run_llm_task
from app.services.llm_model_router import LLMTaskContext
from app.services.prompts import COPILOT_CHAT_PROMPT
from app.services.scheduling_service import SchedulingService
from app.services.search_service import run_global_search
from app.services.work_order_blocker_service import WorkOrderBlockerService

logger = logging.getLogger(__name__)

COPILOT_MAX_TOOL_ROUNDS = int(os.getenv("COPILOT_MAX_TOOL_ROUNDS", "8"))
COPILOT_MAX_OUTPUT_TOKENS = int(os.getenv("COPILOT_MAX_OUTPUT_TOKENS", "1024"))
COPILOT_LLM_TIMEOUT_SECONDS = float(os.getenv("COPILOT_LLM_TIMEOUT_SECONDS", "45"))
_MAX_HISTORY_MESSAGES = 30
_MAX_MESSAGE_CHARS = 4000
_MAX_TOOL_RESULT_CHARS = 6000


def _escape_like(term: str) -> str:
    """Backslash-escape SQL LIKE wildcards in model/user-supplied text.

    Pair every use with ``.like(pattern, escape="\\\\")`` so ``%``/``_`` in part
    numbers and names match literally instead of acting as wildcards.
    """
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _enum_value(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CopilotToolSpec:
    """One read-only copilot tool.

    ``handler`` receives ``db``/``company_id``/``user`` injected server-side
    plus only the input keys declared in ``input_schema.properties``.
    ``allowed_roles`` of ``None`` means any authenticated user (matching the
    source endpoint's access rule); otherwise the tool is hidden from other
    roles and refuses politely if invoked anyway.
    """

    name: str
    description: str
    input_schema: Dict[str, Any]
    handler: Callable[..., Dict[str, Any]]
    allowed_roles: Optional[FrozenSet[UserRole]] = None


@dataclass
class ToolExecution:
    """Outcome of one tool call, ready for the model and the UI trace."""

    tool: str
    payload: Dict[str, Any]
    summary: str
    references: List[Dict[str, Any]] = field(default_factory=list)
    is_error: bool = False


# --- Tool handlers ----------------------------------------------------------
# Access decisions (source endpoint → rule → copilot decision) are documented
# on each handler; see also the registry table in docs once published.


def _tool_lookup_work_order(*, db: Session, company_id: int, user: User, number_or_id: str) -> Dict[str, Any]:
    """Source: ai_context_service.work_order_context (GET /work-orders/{id} is
    any-authenticated). Decision: available to all roles."""
    raw = str(number_or_id).strip()
    if not raw:
        return {"data": {"found": False}, "summary": "empty work-order lookup", "is_error": True}

    # Mirrors GET /work-orders/{id}: tenant-scoped AND excludes soft-deleted rows.
    base_query = db.query(WorkOrder).filter(
        WorkOrder.company_id == company_id, WorkOrder.is_deleted == False  # noqa: E712
    )
    work_order = None
    if raw.isdigit():
        work_order = base_query.filter(WorkOrder.id == int(raw)).first()
    if work_order is None:
        work_order = base_query.filter(func.lower(WorkOrder.work_order_number) == raw.lower()).first()
    if work_order is None:
        term = f"%{_escape_like(raw.lower())}%"
        candidates = (
            base_query.filter(func.lower(WorkOrder.work_order_number).like(term, escape="\\"))
            .order_by(WorkOrder.due_date)
            .limit(5)
            .all()
        )
        if len(candidates) == 1:
            work_order = candidates[0]
        elif candidates:
            return {
                "data": {
                    "found": False,
                    "multiple_matches": [
                        {
                            "id": wo.id,
                            "work_order_number": wo.work_order_number,
                            "status": _enum_value(wo.status),
                            "customer_name": wo.customer_name,
                        }
                        for wo in candidates
                    ],
                },
                "summary": f"found {len(candidates)} work orders matching '{raw}'",
                "references": [
                    {"type": "work_order", "id": wo.id, "label": wo.work_order_number, "url": f"/work-orders/{wo.id}"}
                    for wo in candidates
                ],
            }
        else:
            return {"data": {"found": False}, "summary": f"no work order matching '{raw}'"}

    context = AIContextService(db).work_order_context(company_id=company_id, work_order_id=work_order.id)
    return {
        "data": {"found": True, **context},
        "summary": f"looked up {work_order.work_order_number}",
        "references": [
            {
                "type": "work_order",
                "id": work_order.id,
                "label": work_order.work_order_number,
                "url": f"/work-orders/{work_order.id}",
            }
        ],
    }


# Entity types the copilot's search tool is allowed to touch. "user" is
# deliberately absent (data minimization): the employee directory never enters
# model prompts via the copilot, regardless of the caller's role. GET /search
# keeps its own Admin/Manager-gated user results — that gate is endpoint-only.
_SEARCH_ERP_TYPES = ("part", "work_order", "customer", "bom", "routing", "vendor", "purchase_order", "quote")


def _tool_search_erp(
    *, db: Session, company_id: int, user: User, query: str, types: Optional[List[str]] = None
) -> Dict[str, Any]:
    """Source: GET /search (any-authenticated). Decision: all roles, but the
    tool is restricted to ``_SEARCH_ERP_TYPES`` — employee ("user") results are
    excluded server-side even if the model requests them."""
    q = str(query or "").strip()[:100]
    if not q:
        return {"data": {"results": []}, "summary": "empty search", "is_error": True}
    requested = [str(t) for t in (types or []) if isinstance(t, str)]
    selected = [t for t in requested if t in _SEARCH_ERP_TYPES] or list(_SEARCH_ERP_TYPES)
    types_str = ",".join(selected)
    response = run_global_search(db=db, company_id=company_id, current_user=user, q=q, limit=20, types=types_str)
    return {
        "data": response.model_dump(),
        "summary": f'searched "{q}" ({response.total} results)',
        "references": [
            {"type": item.type, "id": item.id, "label": item.title, "url": item.url} for item in response.results[:10]
        ],
    }


def _tool_list_blocked_work_orders(*, db: Session, company_id: int, user: User) -> Dict[str, Any]:
    """Source: work_order_blocker_service.list_blockers (GET /work-order-blockers
    is any-authenticated). Decision: all roles."""
    service = WorkOrderBlockerService(db)
    blockers = service.list_blockers(company_id=company_id, status=WorkOrderBlockerStatus.OPEN.value, limit=50)
    blockers += service.list_blockers(company_id=company_id, status=WorkOrderBlockerStatus.ACKNOWLEDGED.value, limit=50)

    items = []
    references: List[Dict[str, Any]] = []
    seen_wo_ids = set()
    for blocker in blockers:
        work_order = blocker.work_order
        items.append(
            {
                "blocker_id": blocker.id,
                "title": blocker.title,
                "category": blocker.category,
                "severity": blocker.severity,
                "status": blocker.status,
                "work_order_number": work_order.work_order_number if work_order else None,
                "operation": blocker.operation.name if blocker.operation else None,
                "reported_at": to_utc_iso(blocker.reported_at),
            }
        )
        if work_order and work_order.id not in seen_wo_ids:
            seen_wo_ids.add(work_order.id)
            references.append(
                {
                    "type": "work_order",
                    "id": work_order.id,
                    "label": work_order.work_order_number,
                    "url": f"/work-orders/{work_order.id}",
                }
            )
    return {
        "data": {"open_blockers": items, "total": len(items)},
        "summary": f"found {len(items)} open blockers",
        "references": references[:10],
    }


def _tool_work_center_load(
    *, db: Session, company_id: int, user: User, work_center: Optional[str] = None, horizon_days: int = 14
) -> Dict[str, Any]:
    """Source: scheduling_service.get_load_chart (POST /scheduling/load-chart is
    any-authenticated). Decision: all roles."""
    try:
        horizon = max(1, min(int(horizon_days or 14), 60))
    except (TypeError, ValueError):
        horizon = 14

    wc_query = db.query(WorkCenter).filter(
        WorkCenter.company_id == company_id, WorkCenter.is_active == True
    )  # noqa: E712
    if work_center:
        term = f"%{_escape_like(str(work_center).lower())}%"
        wc_query = wc_query.filter(
            func.lower(WorkCenter.name).like(term, escape="\\")
            | func.lower(WorkCenter.code).like(term, escape="\\")
            | func.lower(WorkCenter.work_center_type).like(term, escape="\\")
        )
    work_centers = wc_query.order_by(WorkCenter.name).limit(20).all()
    if not work_centers:
        return {"data": {"work_centers": []}, "summary": "no matching work centers"}

    scheduling = SchedulingService(db, company_id)
    scheduling._initialize_capacity(work_centers, max(horizon, 1))  # same pattern as the /scheduling endpoints
    start = date.today()
    end = start + timedelta(days=horizon)

    summaries = []
    for wc in work_centers:
        load = scheduling.get_load_chart(wc.id, start, end)
        if not load:
            continue
        utilizations = [point["utilization_pct"] for point in load]
        peak = max(load, key=lambda point: point["utilization_pct"])
        summaries.append(
            {
                "work_center": wc.name,
                "code": wc.code,
                "horizon_days": horizon,
                "avg_utilization_pct": round(sum(utilizations) / len(utilizations), 1),
                "peak_day": peak["date"],
                "peak_utilization_pct": peak["utilization_pct"],
                "days_over_capacity": sum(1 for point in load if point["utilization_pct"] > 100),
            }
        )
    return {
        "data": {"work_centers": summaries},
        "summary": f"checked load for {len(summaries)} work centers over {horizon} days",
    }


def _tool_schedule_conflicts(*, db: Session, company_id: int, user: User) -> Dict[str, Any]:
    """Source: scheduling_service.detect_conflicts (GET /scheduling/conflicts is
    any-authenticated). Decision: all roles."""
    work_centers = (
        db.query(WorkCenter)
        .filter(WorkCenter.company_id == company_id, WorkCenter.is_active == True)  # noqa: E712
        .all()
    )
    scheduling = SchedulingService(db, company_id)
    scheduling._initialize_capacity(work_centers, 90)  # mirrors GET /scheduling/conflicts
    conflicts = scheduling.detect_conflicts(None)
    names = {wc.id: wc.name for wc in work_centers}
    conflicts.sort(key=lambda c: c.get("overload_hours", 0), reverse=True)
    top = [
        {**conflict, "work_center": names.get(conflict["work_center_id"], str(conflict["work_center_id"]))}
        for conflict in conflicts[:20]
    ]
    return {
        "data": {"conflicts": top, "total": len(conflicts)},
        "summary": f"found {len(conflicts)} schedule conflicts",
    }


def _tool_inventory_lookup(*, db: Session, company_id: int, user: User, part_number: str) -> Dict[str, Any]:
    """Source: GET /inventory list/summary (any-authenticated). Decision: all roles."""
    raw = str(part_number or "").strip()
    if not raw:
        return {"data": {"parts": []}, "summary": "empty inventory lookup", "is_error": True}
    term = f"%{_escape_like(raw.lower())}%"
    parts = (
        db.query(Part)
        .filter(
            Part.company_id == company_id,
            Part.is_deleted == False,  # noqa: E712 — Part is soft-delete; mirror the /parts endpoints
            func.lower(Part.part_number).like(term, escape="\\") | func.lower(Part.name).like(term, escape="\\"),
        )
        .limit(5)
        .all()
    )
    if not parts:
        return {"data": {"parts": []}, "summary": f"no parts matching '{raw}'"}

    results = []
    references = []
    for part in parts:
        items = (
            db.query(InventoryItem)
            .filter(InventoryItem.company_id == company_id, InventoryItem.part_id == part.id)
            .all()
        )
        results.append(
            {
                "part_number": part.part_number,
                "name": part.name,
                "total_on_hand": round(sum(item.quantity_on_hand or 0 for item in items), 3),
                "total_available": round(sum(item.quantity_available or 0 for item in items), 3),
                "locations": [
                    {
                        "location": item.location,
                        "on_hand": item.quantity_on_hand,
                        "available": item.quantity_available,
                        "lot_number": item.lot_number,
                    }
                    for item in items[:20]
                ],
            }
        )
        references.append({"type": "part", "id": part.id, "label": part.part_number, "url": f"/parts/{part.id}"})
    return {
        "data": {"parts": results},
        "summary": f"checked inventory for {len(parts)} part(s) matching '{raw}'",
        "references": references,
    }


_OPEN_WO_STATUSES = [WorkOrderStatus.RELEASED, WorkOrderStatus.IN_PROGRESS, WorkOrderStatus.ON_HOLD]
_ACTIVE_QUOTE_STATUSES = [QuoteStatus.DRAFT, QuoteStatus.PENDING, QuoteStatus.SENT]


def _tool_customer_open_orders(*, db: Session, company_id: int, user: User, customer: str) -> Dict[str, Any]:
    """Source: GET /work-orders + GET /quotes (both any-authenticated).
    Decision: all roles."""
    raw = str(customer or "").strip()
    if not raw:
        return {"data": {}, "summary": "empty customer lookup", "is_error": True}
    term = f"%{_escape_like(raw.lower())}%"

    work_orders = (
        db.query(WorkOrder)
        .options(joinedload(WorkOrder.part))
        .filter(
            WorkOrder.company_id == company_id,
            WorkOrder.is_deleted == False,  # noqa: E712 — WorkOrder is soft-delete
            func.lower(WorkOrder.customer_name).like(term, escape="\\"),
            WorkOrder.status.in_(_OPEN_WO_STATUSES),
        )
        .order_by(WorkOrder.due_date)
        .limit(25)
        .all()
    )
    quotes = (
        db.query(Quote)
        .filter(
            Quote.company_id == company_id,
            func.lower(Quote.customer_name).like(term, escape="\\"),
            Quote.status.in_(_ACTIVE_QUOTE_STATUSES),
        )
        .order_by(Quote.created_at.desc())
        .limit(10)
        .all()
    )

    references = [
        {"type": "work_order", "id": wo.id, "label": wo.work_order_number, "url": f"/work-orders/{wo.id}"}
        for wo in work_orders[:10]
    ] + [{"type": "quote", "id": q.id, "label": q.quote_number, "url": f"/quotes?id={q.id}"} for q in quotes[:5]]

    return {
        "data": {
            "open_work_orders": [
                {
                    "work_order_number": wo.work_order_number,
                    "status": _enum_value(wo.status),
                    "priority": wo.priority,
                    "due_date": wo.due_date.isoformat() if wo.due_date else None,
                    "part_number": wo.part.part_number if wo.part else None,
                    "quantity_ordered": wo.quantity_ordered,
                    "quantity_complete": wo.quantity_complete,
                    "customer_name": wo.customer_name,
                }
                for wo in work_orders
            ],
            "active_quotes": [
                {
                    "quote_number": q.quote_number,
                    "status": _enum_value(q.status),
                    "customer_name": q.customer_name,
                    "total": q.total,
                }
                for q in quotes
            ],
        },
        "summary": f"found {len(work_orders)} open WOs and {len(quotes)} active quotes for '{raw}'",
        "references": references,
    }


def _tool_company_snapshot(*, db: Session, company_id: int, user: User) -> Dict[str, Any]:
    """Source: ai_context_service.compact_entity_context (aggregate counts over
    any-authenticated data). Decision: all roles."""
    context = AIContextService(db).compact_entity_context(company_id=company_id)
    return {"data": context, "summary": "pulled company snapshot"}


# Deterministic order matters: the serialized tool list is part of the cached
# prompt prefix — never reorder casually, and keep schemas stable.
TOOL_REGISTRY: List[CopilotToolSpec] = [
    CopilotToolSpec(
        name="lookup_work_order",
        description=(
            "Look up one work order (job) by its number or id. Returns status, part, operations with "
            "work centers, open blockers, and recent shop-floor events. Call this whenever the user "
            "mentions a specific job or work-order number."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "number_or_id": {
                    "type": "string",
                    "description": "Work-order number (e.g. 'WO-2024-0512') or numeric id. Partial numbers allowed.",
                }
            },
            "required": ["number_or_id"],
        },
        handler=_tool_lookup_work_order,
    ),
    CopilotToolSpec(
        name="search_erp",
        description=(
            "Free-text search across parts, work orders, customers, BOMs, routings, vendors, purchase "
            "orders, and quotes. Call this for name/number lookups when no more specific tool fits."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search text (1-100 characters)."},
                "types": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        # No "user" here: the copilot's search excludes the employee directory.
                        "enum": list(_SEARCH_ERP_TYPES),
                    },
                    "description": "Optional entity types to restrict the search to.",
                },
            },
            "required": ["query"],
        },
        handler=_tool_search_erp,
    ),
    CopilotToolSpec(
        name="list_blocked_work_orders",
        description=(
            "List all currently open (and acknowledged) shop-floor blockers with their work orders, "
            "categories, and severities. Call this for 'what's blocked / stuck / waiting' questions."
        ),
        input_schema={"type": "object", "properties": {}},
        handler=_tool_list_blocked_work_orders,
    ),
    CopilotToolSpec(
        name="work_center_load",
        description=(
            "Summarize scheduled load/utilization per work center over a horizon (default 14 days): "
            "average and peak utilization plus days over capacity. Optionally filter to one work center "
            "by name, code, or type (e.g. 'laser')."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "work_center": {
                    "type": "string",
                    "description": "Optional work-center name/code/type filter, e.g. 'laser' or 'weld'.",
                },
                "horizon_days": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 60,
                    "description": "Days ahead to summarize (default 14).",
                },
            },
        },
        handler=_tool_work_center_load,
    ),
    CopilotToolSpec(
        name="schedule_conflicts",
        description=(
            "Detect over-capacity days across all work centers in the next 90 days. Call this for "
            "'are we overbooked / where are the conflicts' questions."
        ),
        input_schema={"type": "object", "properties": {}},
        handler=_tool_schedule_conflicts,
    ),
    CopilotToolSpec(
        name="inventory_lookup",
        description=(
            "Look up on-hand and available inventory for a part number (partial match), broken down "
            "by location and lot."
        ),
        input_schema={
            "type": "object",
            "properties": {"part_number": {"type": "string", "description": "Part number or part name (partial OK)."}},
            "required": ["part_number"],
        },
        handler=_tool_inventory_lookup,
    ),
    CopilotToolSpec(
        name="customer_open_orders",
        description=(
            "List open work orders (released / in progress / on hold) and active quotes for a customer "
            "name (partial match). Call this for 'what's open for <customer>' questions."
        ),
        input_schema={
            "type": "object",
            "properties": {"customer": {"type": "string", "description": "Customer name (partial OK)."}},
            "required": ["customer"],
        },
        handler=_tool_customer_open_orders,
    ),
    CopilotToolSpec(
        name="company_snapshot",
        description=(
            "High-level snapshot of the active company: counts of active work orders, open blockers, "
            "and active parts. Call this for broad 'how are we doing' questions with no specific entity."
        ),
        input_schema={"type": "object", "properties": {}},
        handler=_tool_company_snapshot,
    ),
]


def anthropic_tool_definitions(specs: List[CopilotToolSpec]) -> List[Dict[str, Any]]:
    """Serialize tool specs to Anthropic ``tools`` format (deterministic order)."""
    return [{"name": spec.name, "description": spec.description, "input_schema": spec.input_schema} for spec in specs]


def _block_attr(block: Any, name: str) -> Any:
    if isinstance(block, dict):
        return block.get(name)
    return getattr(block, name, None)


def _serialize_assistant_blocks(blocks: List[Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for block in blocks or []:
        block_type = _block_attr(block, "type")
        if block_type == "text":
            out.append({"type": "text", "text": _block_attr(block, "text") or ""})
        elif block_type == "tool_use":
            out.append(
                {
                    "type": "tool_use",
                    "id": _block_attr(block, "id"),
                    "name": _block_attr(block, "name"),
                    "input": _block_attr(block, "input") or {},
                }
            )
    return out


class CopilotService:
    """Stateless chat turn executor: tool registry + bounded tool-use loop."""

    def __init__(self, db: Session, *, company_id: int, user: User):
        self.db = db
        self.company_id = company_id
        self.user = user

    # -- registry ------------------------------------------------------------
    def tool_specs_for_user(self) -> List[CopilotToolSpec]:
        """Tools visible to this user (role-restricted tools are not registered)."""
        role = self.user.role
        return [spec for spec in TOOL_REGISTRY if spec.allowed_roles is None or role in spec.allowed_roles]

    # -- execution -----------------------------------------------------------
    def execute_tool(self, name: str, tool_input: Optional[Dict[str, Any]]) -> ToolExecution:
        """Run one tool with server-side tenant injection.

        The model NEVER controls the tenant: ``company_id`` comes from this
        service (i.e. the authenticated session), and only input keys declared
        in the tool's schema are forwarded — a model-supplied ``company_id`` /
        ``tenant_id`` / anything else is silently dropped.
        """
        spec = next((s for s in TOOL_REGISTRY if s.name == name), None)
        if spec is None:
            return ToolExecution(
                tool=name, payload={"error": f"unknown tool '{name}'"}, summary=f"unknown tool {name}", is_error=True
            )
        if spec.allowed_roles is not None and self.user.role not in spec.allowed_roles:
            return ToolExecution(
                tool=name,
                payload={"error": "This information is not available for your role."},
                summary=f"{name} not available for role {_enum_value(self.user.role)}",
                is_error=True,
            )

        declared = set((spec.input_schema.get("properties") or {}).keys())
        safe_input = {k: v for k, v in (tool_input or {}).items() if k in declared}
        try:
            result = spec.handler(db=self.db, company_id=self.company_id, user=self.user, **safe_input)
        except Exception as exc:
            logger.warning("Copilot tool %s failed: %s", name, exc)
            return ToolExecution(
                tool=name,
                payload={"error": f"Tool failed: {type(exc).__name__}"},
                summary=f"{name} failed",
                is_error=True,
            )
        return ToolExecution(
            tool=name,
            payload=result.get("data", {}),
            summary=result.get("summary") or f"ran {name}",
            references=result.get("references") or [],
            is_error=bool(result.get("is_error")),
        )

    # -- message shaping -----------------------------------------------------
    def _build_messages(self, messages: List[Dict[str, str]], context_hint: Optional[str]) -> List[Dict[str, Any]]:
        cleaned: List[Dict[str, Any]] = []
        for message in messages[-_MAX_HISTORY_MESSAGES:]:
            role = message.get("role")
            content = str(message.get("content") or "").strip()
            if role not in ("user", "assistant") or not content:
                continue
            cleaned.append({"role": role, "content": content[:_MAX_MESSAGE_CHARS]})
        while cleaned and cleaned[0]["role"] != "user":
            cleaned.pop(0)
        if not cleaned:
            raise ValueError("Conversation must contain at least one user message")
        if cleaned[-1]["role"] != "user":
            raise ValueError("The last message must be from the user")

        if context_hint:
            last = cleaned[-1]
            # Volatile context goes in the (uncached) suffix, never the system prompt.
            last["content"] = [
                {"type": "text", "text": f"<context_hint>{str(context_hint)[:500]}</context_hint>"},
                {"type": "text", "text": last["content"]},
            ]
        return cleaned

    def _system_blocks(self) -> List[Dict[str, Any]]:
        # Stable prefix: deterministic tool defs render first, then this block;
        # the single cache_control breakpoint caches tools + system together.
        return [
            {
                "type": "text",
                "text": COPILOT_CHAT_PROMPT.text,
                "cache_control": {"type": "ephemeral"},
            }
        ]

    # -- chat loop -----------------------------------------------------------
    def stream_chat(
        self, *, messages: List[Dict[str, str]], context_hint: Optional[str] = None
    ) -> Generator[Dict[str, Any], None, None]:
        """Run one chat turn, yielding UI events as the loop progresses.

        Events: ``{"type": "tool_use", ...}`` per executed tool,
        ``{"type": "delta", "text": ...}`` chunks of the final answer, then a
        single ``{"type": "final", ...}`` frame with the full payload.
        """
        specs = self.tool_specs_for_user()
        tool_defs = anthropic_tool_definitions(specs)
        system_blocks = self._system_blocks()
        api_messages = self._build_messages(messages, context_hint)
        last_user_text = messages[-1].get("content", "") if messages else ""

        input_chars = sum(len(str(m.get("content") or "")) for m in messages)
        tool_trace: List[Dict[str, str]] = []
        references: List[Dict[str, Any]] = []
        seen_refs = set()
        rounds = 0
        truncated = False
        answer = ""
        model_used: Optional[str] = None

        # Up to COPILOT_MAX_TOOL_ROUNDS tool rounds, plus one forced final
        # answer call (tool_choice "none" keeps the cached tool prefix intact).
        for call_index in range(COPILOT_MAX_TOOL_ROUNDS + 1):
            force_final = call_index == COPILOT_MAX_TOOL_ROUNDS
            result = run_llm_task(
                LLMTaskContext(
                    task="copilot_chat", input_chars=input_chars, max_output_tokens=COPILOT_MAX_OUTPUT_TOKENS
                ),
                messages=api_messages,
                system=system_blocks,
                tools=tool_defs,
                tool_choice={"type": "none"} if force_final else None,
                max_tokens=COPILOT_MAX_OUTPUT_TOKENS,
                company_id=self.company_id,
                feature="copilot_panel",
                prompt_version=COPILOT_CHAT_PROMPT.version,
                timeout=COPILOT_LLM_TIMEOUT_SECONDS,
                max_retries=1,  # one retry for transient overload (529s), bounded so a turn can't stall
            )
            model_used = result.model
            response = result.raw_response
            blocks = getattr(response, "content", None) or []
            text_parts = [_block_attr(b, "text") or "" for b in blocks if _block_attr(b, "type") == "text"]
            tool_uses = [b for b in blocks if _block_attr(b, "type") == "tool_use"]

            if not tool_uses or force_final:
                answer = "\n".join(part for part in text_parts if part).strip()
                # Truncated when we forced the final call (round cap) OR the
                # model ran out of output tokens mid-answer.
                truncated = force_final or getattr(response, "stop_reason", None) == "max_tokens"
                break

            rounds += 1
            api_messages.append({"role": "assistant", "content": _serialize_assistant_blocks(blocks)})
            result_blocks = []
            for tool_use in tool_uses:
                execution = self.execute_tool(_block_attr(tool_use, "name"), _block_attr(tool_use, "input"))
                tool_trace.append({"tool": execution.tool, "summary": execution.summary})
                for ref in execution.references:
                    key = (ref.get("type"), ref.get("id"))
                    if key not in seen_refs:
                        seen_refs.add(key)
                        references.append(ref)
                yield {"type": "tool_use", "tool": execution.tool, "summary": execution.summary}
                content = json.dumps(execution.payload, default=str)
                if len(content) > _MAX_TOOL_RESULT_CHARS:
                    content = content[:_MAX_TOOL_RESULT_CHARS] + " ...[truncated]"
                result_blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": _block_attr(tool_use, "id"),
                        "content": content,
                        "is_error": execution.is_error,
                    }
                )
            api_messages.append({"role": "user", "content": result_blocks})

        if not answer:
            answer = (
                "I hit the lookup limit before finishing — here's what I gathered so far: "
                + "; ".join(entry["summary"] for entry in tool_trace[-5:])
                if tool_trace
                else "I wasn't able to find an answer for that."
            )

        for chunk in _chunk_text(answer):
            yield {"type": "delta", "text": chunk}

        interaction_id = self._record_interaction(
            question=last_user_text,
            answer=answer,
            tool_trace=tool_trace,
            rounds=rounds,
            truncated=truncated,
            model=model_used,
            context_hint_present=bool(context_hint),
        )

        yield {
            "type": "final",
            "answer": answer,
            "references": references[:20],
            "tool_trace": tool_trace,
            "interaction_id": interaction_id,
            "rounds": rounds,
            "truncated": truncated,
        }

    def run_chat(self, *, messages: List[Dict[str, str]], context_hint: Optional[str] = None) -> Dict[str, Any]:
        """Non-streaming variant: drain the event stream, return the final payload."""
        final: Dict[str, Any] = {}
        for event in self.stream_chat(messages=messages, context_hint=context_hint):
            if event.get("type") == "final":
                final = event
        final.pop("type", None)
        return final

    # -- learning loop ---------------------------------------------------------
    def _record_interaction(
        self,
        *,
        question: str,
        answer: str,
        tool_trace: List[Dict[str, str]],
        rounds: int,
        truncated: bool,
        model: Optional[str],
        context_hint_present: bool,
    ) -> Optional[int]:
        """Record this turn via the shared learning fabric (redaction applies there)."""
        try:
            event = AILearningService(self.db).record_interaction(
                company_id=self.company_id,
                user=self.user,
                data=AIInteractionEventCreate(
                    event_type=AIEventType.SUGGESTION_SHOWN,
                    source_module="copilot",
                    ai_feature="copilot_chat",
                    surface="copilot_panel",
                    context_summary=str(question)[:1000],
                    event_payload={
                        "rounds": rounds,
                        "truncated": truncated,
                        "tools_used": [entry["tool"] for entry in tool_trace],
                        "answer_preview": answer[:400],
                        "context_hint_present": context_hint_present,
                    },
                    prompt_version=COPILOT_CHAT_PROMPT.version,
                    model_version=model,
                ),
            )
            return event.id
        except Exception as exc:
            logger.warning("Failed to record copilot interaction: %s", exc)
            return None


def _chunk_text(text: str, size: int = 120) -> List[str]:
    """Split the answer into small frames so the panel renders progressively."""
    if not text:
        return []
    words = text.split(" ")
    chunks: List[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}" if current else word
        if len(candidate) >= size:
            chunks.append(candidate + " ")
            current = ""
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks
