"""Morning brief recommendation + in-app notifications for managers (Phase 3)."""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy.orm import Session

from app.models.ai_learning import AIRecommendation
from app.models.notification import NotificationLog
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder, WorkOrderStatus
from app.models.work_order_blocker import WorkOrderBlocker, WorkOrderBlockerStatus
from app.services.ai_learning_service import AILearningService
from app.services.ai_sensors.common import mint_recommendation, recommendation_open

OPEN_WO = (WorkOrderStatus.RELEASED, WorkOrderStatus.IN_PROGRESS, WorkOrderStatus.ON_HOLD)


def run_morning_brief_sensor(db: Session, company_id: int) -> int:
    """Create one daily morning_brief recommendation if not already open for today."""
    learning = AILearningService(db)
    today_key = date.today().isoformat()
    dedupe = f"morning_brief:{today_key}"

    if recommendation_open(
        learning,
        company_id=company_id,
        recommendation_type="morning_brief",
        source_module="operations",
        target_entity_type="company",
        target_entity_id=company_id,
        dedupe_key=dedupe,
    ):
        return 0

    today = date.today()
    late_wos = (
        db.query(WorkOrder)
        .filter(
            WorkOrder.company_id == company_id,
            WorkOrder.is_deleted == False,  # noqa: E712
            WorkOrder.status.in_(OPEN_WO),
            WorkOrder.due_date.isnot(None),
            WorkOrder.due_date < today,
        )
        .order_by(WorkOrder.due_date.asc())
        .limit(5)
        .all()
    )
    at_risk = (
        db.query(WorkOrder)
        .filter(
            WorkOrder.company_id == company_id,
            WorkOrder.is_deleted == False,  # noqa: E712
            WorkOrder.status.in_(OPEN_WO),
            WorkOrder.due_date.isnot(None),
            WorkOrder.due_date >= today,
            WorkOrder.due_date <= today + timedelta(days=3),
        )
        .count()
    )
    open_blockers = (
        db.query(WorkOrderBlocker)
        .filter(
            WorkOrderBlocker.company_id == company_id,
            WorkOrderBlocker.status.in_([WorkOrderBlockerStatus.OPEN.value, WorkOrderBlockerStatus.ACKNOWLEDGED.value]),
        )
        .count()
    )
    pending_ai = (
        db.query(AIRecommendation)
        .filter(
            AIRecommendation.company_id == company_id,
            AIRecommendation.status == "pending",
            AIRecommendation.recommendation_type != "morning_brief",
        )
        .count()
    )
    high_ai = (
        db.query(AIRecommendation)
        .filter(
            AIRecommendation.company_id == company_id,
            AIRecommendation.status == "pending",
            AIRecommendation.priority == "high",
            AIRecommendation.recommendation_type != "morning_brief",
        )
        .count()
    )

    late_lines = [f"• {wo.work_order_number} due {wo.due_date.isoformat()} (P{wo.priority})" for wo in late_wos] or [
        "• No late work orders"
    ]

    summary = (
        f"Plant brief for {today_key}: {len(late_wos)} late WO(s), {at_risk} due within 3 days, "
        f"{open_blockers} open blocker(s), {pending_ai} pending AI actions ({high_ai} high)."
    )
    detail = summary + "\n\nLate jobs:\n" + "\n".join(late_lines)

    mint_recommendation(
        db,
        company_id=company_id,
        source_module="operations",
        recommendation_type="morning_brief",
        priority="high" if late_wos or high_ai else "medium",
        title=f"Morning brief — {today_key}",
        summary=detail[:2000],
        rationale="Scheduled always-on operations brief (deterministic).",
        target_entity_type="company",
        target_entity_id=company_id,
        suggested_action={
            "type": "open_action_inbox",
            "href": "/action-inbox",
            "autonomy": "suggest_only",
            "dedupe_key": dedupe,
            "metrics": {
                "late_count": len(late_wos),
                "at_risk_count": at_risk,
                "open_blockers": open_blockers,
                "pending_ai": pending_ai,
                "high_ai": high_ai,
            },
        },
        evidence=[
            {
                "type": "morning_metrics",
                "late_work_orders": [wo.work_order_number for wo in late_wos],
                "at_risk_count": at_risk,
                "open_blockers": open_blockers,
                "pending_ai": pending_ai,
            }
        ],
        impact={"expected": "Focus leadership attention before the shift starts.", "magnitude": 1.2},
        confidence_score=0.9,
        expires_days=1,
    )

    # In-app notifications for supervisors+
    recipients = (
        db.query(User)
        .filter(
            User.company_id == company_id,
            User.is_active == True,  # noqa: E712
            User.role.in_([UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]),
        )
        .all()
    )
    for user in recipients:
        db.add(
            NotificationLog(
                company_id=company_id,
                user_id=user.id,
                event_type="AI_MORNING_BRIEF",
                channel="in_app",
                subject=f"Morning brief — {today_key}",
                body=summary,
                sent=True,
                related_type="ai_recommendation",
                related_id=None,
            )
        )
    return 1
