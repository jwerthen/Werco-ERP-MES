"""
Global Search API Endpoint

Searches across all major entities in the system:
- Parts
- Work Orders
- Customers
- BOMs
- Routings
- Users
- Inventory Items
- Purchase Orders
- Quotes

The entity-search core lives in ``app/services/search_service.py`` (shared
with Werco Copilot's ``search_erp`` tool); this module keeps the HTTP contract
and the natural-language search interpreter.
"""

import json
import logging
import re
from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session, joinedload

from app.api.deps import get_current_company_id, get_current_user
from app.db.database import get_db
from app.models.part import Part
from app.models.user import User
from app.models.work_center import WorkCenter
from app.models.work_order import WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.models.work_order_blocker import WorkOrderBlocker, WorkOrderBlockerCategory, WorkOrderBlockerStatus
from app.services.search_service import SearchResponse, SearchResult, run_global_search

logger = logging.getLogger(__name__)

router = APIRouter()

__all__ = ["router", "SearchResult", "SearchResponse"]


class NaturalLanguageSearchRequest(BaseModel):
    query: str
    limit: int = 20


class NaturalLanguageSearchResult(SearchResult):
    explanation: str
    matched_filters: List[str] = []


class NaturalLanguageSearchResponse(BaseModel):
    query: str
    confidence: float
    interpreted_filters: dict
    used_fallback: bool
    results: List[NaturalLanguageSearchResult]


@router.get("/", response_model=SearchResponse)
def global_search(
    q: str = Query(..., min_length=1, max_length=100, description="Search query"),
    limit: int = Query(default=20, le=50, description="Maximum results"),
    types: Optional[str] = Query(default=None, description="Comma-separated types to search"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """
    Global search across all entities.

    Search types:
    - part: Parts by part number, name, description
    - work_order: Work orders by number, customer PO, lot number
    - customer: Customers by name, code
    - bom: BOMs by name
    - routing: Routings by name
    - user: Users by name, email, employee ID
    - inventory: Inventory items by location, lot
    - purchase_order: POs by number, vendor
    - quote: Quotes by number, customer
    - vendor: Vendors/Suppliers by name, code
    """
    return run_global_search(db=db, company_id=company_id, current_user=current_user, q=q, limit=limit, types=types)


def _contains_any(query: str, terms: List[str]) -> bool:
    return any(term in query for term in terms)


def _parse_nl_search(query: str) -> dict:
    normalized = " ".join(query.lower().strip().split())
    work_center_terms = []
    for term in ["laser", "weld", "welding", "brake", "press brake", "bend", "saw", "machining", "paint"]:
        if term in normalized:
            work_center_terms.append(term)

    filters = {
        "late": _contains_any(normalized, ["late", "overdue", "past due", "behind"]),
        "blocked": _contains_any(normalized, ["blocked", "waiting", "stuck", "hold", "on hold"]),
        "material_missing": _contains_any(
            normalized,
            ["waiting on material", "no material", "missing material", "material missing", "short material"],
        ),
        "hot": _contains_any(normalized, ["hot", "expedite", "rush", "critical"]),
        "work_center_terms": work_center_terms,
        "active_jobs": _contains_any(normalized, ["job", "jobs", "work order", "work orders", "wo"]),
    }
    filters["filter_count"] = _count_filters(filters)
    filters["parser"] = "rules"
    return filters


def _count_filters(filters: dict) -> int:
    boolean_keys = ("late", "blocked", "material_missing", "hot", "active_jobs")
    return sum(1 for key in boolean_keys if filters.get(key)) + len(filters.get("work_center_terms") or [])


# Rule-parser confidence is 0.35 + 0.15 per filter; at >= 2 filters (0.65) the
# rules alone are confident enough that the LLM adds latency without value.
_NL_HIGH_CONFIDENCE_FILTER_COUNT = 2
_NL_LLM_TIMEOUT_SECONDS = 3.0
_NL_MAX_WORK_CENTER_TERMS = 5


def _sanitize_work_center_term(term: str) -> Optional[str]:
    """Lowercase and strip a model-supplied term before it reaches a SQL LIKE."""
    if not isinstance(term, str):
        return None
    cleaned = re.sub(r"[^a-z0-9 \-]", "", term.lower().strip())[:40].strip()
    return cleaned or None


def _llm_interpret_nl_search(query: str, *, company_id: int) -> Optional[dict]:
    """Fast-tier (Haiku via router) intent parse emitting the rule-parser filter shape.

    Returns None on any failure (LLM unconfigured, API error, timeout, invalid
    JSON) so the caller can fall back to the rule parser. Telemetry is recorded
    per call under task="nl_search".
    """
    from app.services.llm_client import LLMNotConfiguredError, run_llm_task
    from app.services.llm_model_router import LLMTaskContext
    from app.services.prompts import NL_SEARCH_INTENT_PROMPT

    try:
        result = run_llm_task(
            LLMTaskContext(task="nl_search", input_chars=len(query), max_output_tokens=300),
            messages=[{"role": "user", "content": query[:500]}],
            system=[
                {
                    "type": "text",
                    "text": NL_SEARCH_INTENT_PROMPT.text,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            max_tokens=300,
            company_id=company_id,
            feature="nl_search",
            prompt_version=NL_SEARCH_INTENT_PROMPT.version,
            timeout=_NL_LLM_TIMEOUT_SECONDS,
        )
    except LLMNotConfiguredError:
        return None
    except Exception as exc:  # API error / timeout — rule parser covers us
        logger.warning("NL search LLM intent parse failed; using rule parser: %s", exc)
        return None

    try:
        raw = json.loads(result.text)
    except (TypeError, ValueError):
        logger.warning("NL search LLM returned non-JSON output; using rule parser")
        return None
    if not isinstance(raw, dict):
        return None

    terms = []
    for term in raw.get("work_center_terms") or []:
        cleaned = _sanitize_work_center_term(term)
        if cleaned:
            terms.append(cleaned)
        if len(terms) >= _NL_MAX_WORK_CENTER_TERMS:
            break

    filters = {
        "late": bool(raw.get("late")),
        "blocked": bool(raw.get("blocked")),
        "material_missing": bool(raw.get("material_missing")),
        "hot": bool(raw.get("hot")),
        "work_center_terms": terms,
        "active_jobs": bool(raw.get("active_jobs")),
    }
    filters["filter_count"] = _count_filters(filters)
    filters["parser"] = "llm"
    return filters


def _interpret_nl_search(query: str, *, company_id: int) -> dict:
    """Cheap-first NL interpretation: rules always run; LLM only when rules are weak.

    The LLM path emits the SAME filter structure as ``_parse_nl_search`` so the
    downstream query builder and the frontend contract are unchanged. Any LLM
    failure falls back to the rule result.
    """
    rule_filters = _parse_nl_search(query)
    if rule_filters["filter_count"] >= _NL_HIGH_CONFIDENCE_FILTER_COUNT:
        return rule_filters

    llm_filters = _llm_interpret_nl_search(query, company_id=company_id)
    if llm_filters is not None and llm_filters["filter_count"] > rule_filters["filter_count"]:
        return llm_filters
    return rule_filters


def _literal_work_order_fallback(
    *,
    db: Session,
    company_id: int,
    query: str,
    limit: int,
) -> List[NaturalLanguageSearchResult]:
    search_term = f"%{query.lower()}%"
    rows = (
        db.query(WorkOrder)
        .options(joinedload(WorkOrder.part))
        .outerjoin(Part, WorkOrder.part_id == Part.id)
        .filter(
            WorkOrder.company_id == company_id,
            or_(
                func.lower(WorkOrder.work_order_number).like(search_term),
                func.lower(WorkOrder.customer_po).like(search_term),
                func.lower(WorkOrder.lot_number).like(search_term),
                func.lower(WorkOrder.customer_name).like(search_term),
                func.lower(Part.part_number).like(search_term),
                func.lower(Part.name).like(search_term),
            ),
        )
        .order_by(WorkOrder.priority, WorkOrder.due_date)
        .limit(limit)
        .all()
    )
    return [
        NaturalLanguageSearchResult(
            id=wo.id,
            type="work_order",
            title=wo.work_order_number,
            subtitle=f"{wo.part.part_number if wo.part else ''} - {wo.status.value}".strip(" -"),
            url=f"/work-orders/{wo.id}",
            icon="clipboard",
            explanation="Matched literal work-order, customer, or part text.",
            matched_filters=["literal_text"],
        )
        for wo in rows
    ]


@router.post("/nl", response_model=NaturalLanguageSearchResponse)
def natural_language_search(
    request: NaturalLanguageSearchRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Interpret natural-language operational searches into explainable ERP filters.

    Interpretation is cheap-first: the deterministic rule parser always runs,
    and a fast-tier LLM intent parse (same filter structure, ~3s timeout,
    telemetry task ``nl_search``) is consulted only when the rules score low
    confidence. The LLM never changes the response contract; on any LLM
    failure the rule result is used unchanged.
    """
    limit = max(1, min(request.limit or 20, 50))
    filters = _interpret_nl_search(request.query, company_id=company_id)
    if filters["filter_count"] == 0:
        return NaturalLanguageSearchResponse(
            query=request.query,
            confidence=0.35,
            interpreted_filters=filters,
            used_fallback=True,
            results=_literal_work_order_fallback(db=db, company_id=company_id, query=request.query, limit=limit),
        )

    query = (
        db.query(WorkOrder)
        .options(joinedload(WorkOrder.part))
        .filter(
            WorkOrder.company_id == company_id,
            WorkOrder.status.in_([WorkOrderStatus.RELEASED, WorkOrderStatus.IN_PROGRESS, WorkOrderStatus.ON_HOLD]),
        )
    )

    matched_filters: List[str] = []
    if filters["late"]:
        query = query.filter(WorkOrder.due_date < date.today())
        matched_filters.append("late")
    if filters["hot"]:
        query = query.filter(WorkOrder.priority <= 2)
        matched_filters.append("hot_priority")
    if filters["work_center_terms"]:
        query = query.join(WorkOrderOperation, WorkOrderOperation.work_order_id == WorkOrder.id).join(
            WorkCenter, WorkOrderOperation.work_center_id == WorkCenter.id
        )
        wc_clauses = []
        for term in filters["work_center_terms"]:
            term_filter = f"%{term}%"
            wc_clauses.extend(
                [
                    func.lower(WorkCenter.name).like(term_filter),
                    func.lower(WorkCenter.code).like(term_filter),
                    func.lower(WorkCenter.work_center_type).like(term_filter),
                    func.lower(WorkOrderOperation.name).like(term_filter),
                    func.lower(WorkOrderOperation.operation_group).like(term_filter),
                ]
            )
        query = query.filter(or_(*wc_clauses))
        matched_filters.append("work_center:" + ",".join(filters["work_center_terms"]))

    if filters["material_missing"]:
        query = query.join(
            WorkOrderBlocker,
            and_(
                WorkOrderBlocker.work_order_id == WorkOrder.id,
                WorkOrderBlocker.company_id == company_id,
            ),
        ).filter(
            WorkOrderBlocker.status.in_([WorkOrderBlockerStatus.OPEN.value, WorkOrderBlockerStatus.ACKNOWLEDGED.value]),
            WorkOrderBlocker.category == WorkOrderBlockerCategory.MATERIAL_MISSING.value,
        )
        matched_filters.append("material_missing_blocker")
    elif filters["blocked"]:
        query = query.outerjoin(
            WorkOrderBlocker,
            and_(
                WorkOrderBlocker.work_order_id == WorkOrder.id,
                WorkOrderBlocker.company_id == company_id,
                WorkOrderBlocker.status.in_(
                    [WorkOrderBlockerStatus.OPEN.value, WorkOrderBlockerStatus.ACKNOWLEDGED.value]
                ),
            ),
        ).filter(or_(WorkOrder.status == WorkOrderStatus.ON_HOLD, WorkOrderBlocker.id.isnot(None)))
        matched_filters.append("blocked")

    rows = query.distinct().order_by(WorkOrder.priority, WorkOrder.due_date).limit(limit).all()
    confidence = min(0.95, 0.35 + (0.15 * filters["filter_count"]))
    results = [
        NaturalLanguageSearchResult(
            id=wo.id,
            type="work_order",
            title=wo.work_order_number,
            subtitle=f"{wo.part.part_number if wo.part else ''} - {wo.status.value}".strip(" -"),
            url=f"/work-orders/{wo.id}",
            icon="clipboard",
            explanation=f"Matched operational filters: {', '.join(matched_filters)}.",
            matched_filters=matched_filters,
        )
        for wo in rows
    ]

    return NaturalLanguageSearchResponse(
        query=request.query,
        confidence=round(confidence, 2),
        interpreted_filters=filters,
        used_fallback=False,
        results=results,
    )


@router.get("/recent")
def get_recent_items(
    limit: int = Query(default=10, le=20),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get recently accessed/created items for quick access."""
    results = []

    # Recent work orders (last 5)
    recent_wos = (
        db.query(WorkOrder)
        .filter(WorkOrder.company_id == company_id)
        .order_by(WorkOrder.updated_at.desc())
        .limit(5)
        .all()
    )

    for wo in recent_wos:
        results.append(
            SearchResult(
                id=wo.id,
                type="work_order",
                title=wo.work_order_number,
                subtitle=wo.customer_name,
                url=f"/work-orders/{wo.id}",
                icon="clipboard",
            )
        )

    # Recent parts (last 5)
    recent_parts = (
        db.query(Part)
        .filter(Part.company_id == company_id, Part.is_active == True)  # noqa: E712
        .order_by(Part.updated_at.desc())
        .limit(5)
        .all()
    )

    for part in recent_parts:
        results.append(
            SearchResult(
                id=part.id,
                type="part",
                title=part.part_number,
                subtitle=part.name,
                url=f"/parts/{part.id}",
                icon="cube",
            )
        )

    return results[:limit]
