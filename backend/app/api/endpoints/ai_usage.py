"""AI usage telemetry endpoints.

Read-only aggregates over ``ai_usage_events`` for the ACTIVE company. This is
cost/latency observability — not audit data and not learning data.
"""

import logging
from datetime import datetime, timedelta
from typing import Any, List

from fastapi import APIRouter, Depends, Query
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.api.deps import get_current_company_id, require_role
from app.db.database import get_db
from app.db.tenant_filter import tenant_filter
from app.models.ai_usage import AIUsageEvent
from app.models.user import User, UserRole
from app.schemas.ai_usage import (
    AIUsageAggregate,
    AIUsageModelSummary,
    AIUsageSummaryResponse,
    AIUsageTaskSummary,
)

logger = logging.getLogger(__name__)
router = APIRouter()

_AGGREGATE_COLUMNS = (
    func.count(AIUsageEvent.id).label("calls"),
    func.coalesce(func.sum(AIUsageEvent.input_tokens), 0).label("input_tokens"),
    func.coalesce(func.sum(AIUsageEvent.output_tokens), 0).label("output_tokens"),
    func.coalesce(func.sum(AIUsageEvent.cache_creation_tokens), 0).label("cache_creation_tokens"),
    func.coalesce(func.sum(AIUsageEvent.cache_read_tokens), 0).label("cache_read_tokens"),
    func.sum(AIUsageEvent.estimated_cost_usd).label("estimated_cost_usd"),
    func.avg(AIUsageEvent.latency_ms).label("avg_latency_ms"),
    func.sum(case((AIUsageEvent.success.is_(False), 1), else_=0)).label("error_count"),
)


def _aggregate_from_row(row: Any) -> AIUsageAggregate:
    calls = int(row.calls or 0)
    error_count = int(row.error_count or 0)
    return AIUsageAggregate(
        calls=calls,
        input_tokens=int(row.input_tokens or 0),
        output_tokens=int(row.output_tokens or 0),
        cache_creation_tokens=int(row.cache_creation_tokens or 0),
        cache_read_tokens=int(row.cache_read_tokens or 0),
        estimated_cost_usd=float(row.estimated_cost_usd) if row.estimated_cost_usd is not None else None,
        avg_latency_ms=float(row.avg_latency_ms) if row.avg_latency_ms is not None else None,
        error_rate=(error_count / calls) if calls else 0.0,
    )


@router.get("/summary", response_model=AIUsageSummaryResponse)
def get_ai_usage_summary(
    days: int = Query(30, ge=1, le=365, description="Aggregation window in days"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
):
    """Per-task and per-model AI usage aggregates for the active company."""
    since = datetime.utcnow() - timedelta(days=days)

    def scoped(query):  # tenant scoping + time window for every aggregate query
        return tenant_filter(query, AIUsageEvent, company_id).filter(AIUsageEvent.created_at >= since)

    totals_row = scoped(db.query(*_AGGREGATE_COLUMNS)).one()

    by_task: List[AIUsageTaskSummary] = []
    task_rows = scoped(db.query(AIUsageEvent.task.label("task"), *_AGGREGATE_COLUMNS)).group_by(AIUsageEvent.task).all()
    for row in sorted(task_rows, key=lambda r: ((r.estimated_cost_usd is None), -(r.estimated_cost_usd or 0))):
        by_task.append(AIUsageTaskSummary(task=row.task, **_aggregate_from_row(row).model_dump()))

    by_model: List[AIUsageModelSummary] = []
    model_rows = (
        scoped(db.query(AIUsageEvent.model.label("model"), *_AGGREGATE_COLUMNS)).group_by(AIUsageEvent.model).all()
    )
    for row in sorted(model_rows, key=lambda r: ((r.estimated_cost_usd is None), -(r.estimated_cost_usd or 0))):
        by_model.append(AIUsageModelSummary(model=row.model, **_aggregate_from_row(row).model_dump()))

    return AIUsageSummaryResponse(
        window_days=days,
        since=since,
        totals=_aggregate_from_row(totals_row),
        by_task=by_task,
        by_model=by_model,
    )
