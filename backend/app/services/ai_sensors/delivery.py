"""At-risk / late work-order delivery sensor."""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy.orm import Session, joinedload

from app.models.work_order import WorkOrder, WorkOrderStatus
from app.services.ai_learning_service import AILearningService
from app.services.ai_sensors.common import mint_recommendation, recommendation_open

OPEN_STATUSES = (
    WorkOrderStatus.RELEASED,
    WorkOrderStatus.IN_PROGRESS,
    WorkOrderStatus.ON_HOLD,
)

# Horizon for "at risk" (not yet late): due within this many days
AT_RISK_HORIZON_DAYS = 3
MAX_RECS_PER_RUN = 25


def run_at_risk_delivery_sensor(db: Session, company_id: int) -> int:
    """Mint recommend-only items for late and near-due open work orders."""
    learning = AILearningService(db)
    today = date.today()
    horizon = today + timedelta(days=AT_RISK_HORIZON_DAYS)

    work_orders = (
        db.query(WorkOrder)
        .options(joinedload(WorkOrder.part))
        .filter(
            WorkOrder.company_id == company_id,
            WorkOrder.is_deleted == False,  # noqa: E712
            WorkOrder.status.in_(OPEN_STATUSES),
            WorkOrder.due_date.isnot(None),
            WorkOrder.due_date <= horizon,
        )
        .order_by(WorkOrder.due_date.asc(), WorkOrder.priority.asc())
        .limit(MAX_RECS_PER_RUN * 2)
        .all()
    )

    created = 0
    for wo in work_orders:
        if created >= MAX_RECS_PER_RUN:
            break
        if recommendation_open(
            learning,
            company_id=company_id,
            recommendation_type="at_risk_delivery",
            source_module="scheduling",
            target_entity_type="work_order",
            target_entity_id=wo.id,
        ):
            continue

        days_until = (wo.due_date - today).days
        is_late = days_until < 0
        priority = "high" if is_late or wo.priority <= 2 or wo.status == WorkOrderStatus.ON_HOLD else "medium"
        part_number = wo.part.part_number if wo.part else "unknown"
        status_value = wo.status.value if hasattr(wo.status, "value") else str(wo.status)

        if is_late:
            title = f"Late work order {wo.work_order_number}"
            summary = (
                f"{wo.work_order_number} ({part_number}) is {-days_until} day(s) past due "
                f"(status {status_value}). Review schedule, blockers, and customer commitment."
            )
            confidence = min(0.95, 0.75 + min(-days_until, 10) * 0.02)
        else:
            title = f"At-risk delivery {wo.work_order_number}"
            summary = (
                f"{wo.work_order_number} ({part_number}) is due in {days_until} day(s) "
                f"(status {status_value}, priority P{wo.priority}). Confirm it can still ship on time."
            )
            confidence = 0.7 if days_until <= 1 else 0.6

        mint_recommendation(
            db,
            company_id=company_id,
            source_module="scheduling",
            recommendation_type="at_risk_delivery",
            priority=priority,
            title=title,
            summary=summary,
            rationale="Deterministic due-date sensor on open work orders (no LLM).",
            target_entity_type="work_order",
            target_entity_id=wo.id,
            suggested_action={
                "type": "adjust_work_order_priority",
                "work_order_id": wo.id,
                "work_order_number": wo.work_order_number,
                "priority": 1 if is_late else 2,
                "href": f"/work-orders/{wo.id}",
                "autonomy": "auto_execute",
                "dedupe_key": f"at_risk_delivery:wo:{wo.id}",
            },
            evidence=[
                {
                    "type": "due_date",
                    "due_date": wo.due_date.isoformat(),
                    "days_until_due": days_until,
                    "status": status_value,
                    "priority": wo.priority,
                    "quantity_ordered": float(wo.quantity_ordered or 0),
                    "quantity_complete": float(wo.quantity_complete or 0),
                }
            ],
            impact={
                "expected": "Protect on-time delivery and reduce expedite cost.",
                "magnitude": 1.5 if is_late else 1.0,
            },
            confidence_score=confidence,
            expires_days=7 if is_late else 5,
        )
        created += 1

    return created
