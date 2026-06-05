import logging

from app.db.session import SessionLocal
from app.models.ai_learning import AIRecommendation
from app.services.ai_learning_service import AILearningService
from app.services.operational_event_service import OperationalEventService
from app.services.work_order_blocker_service import WorkOrderBlockerService

logger = logging.getLogger(__name__)


async def aggregate_ai_learning_task():
    """Aggregate AI feedback signals into suggest-only improvement recommendations."""
    db = SessionLocal()
    try:
        summary = AILearningService(db).aggregate_learning_signals()
        stale_blockers = WorkOrderBlockerService(db).stale_open_blockers(older_than_hours=24)
        escalated = 0
        for blocker in stale_blockers:
            existing = (
                db.query(AIRecommendation)
                .filter(
                    AIRecommendation.company_id == blocker.company_id,
                    AIRecommendation.source_module == "shop_floor",
                    AIRecommendation.recommendation_type == "stale_blocker_escalation",
                    AIRecommendation.target_entity_type == "work_order_blocker",
                    AIRecommendation.target_entity_id == blocker.id,
                    AIRecommendation.status == "pending",
                )
                .first()
            )
            if existing:
                continue
            work_order_number = (
                blocker.work_order.work_order_number if blocker.work_order else f"WO #{blocker.work_order_id}"
            )
            db.add(
                AIRecommendation(
                    company_id=blocker.company_id,
                    source_module="shop_floor",
                    recommendation_type="stale_blocker_escalation",
                    status="pending",
                    priority="high",
                    title=f"Escalate stale blocker on {work_order_number}",
                    summary="This blocker has been open for more than 24 hours and may be holding up the schedule.",
                    rationale=blocker.note,
                    target_entity_type="work_order_blocker",
                    target_entity_id=blocker.id,
                    suggested_action={
                        "type": "escalate_blocker",
                        "blocker_id": blocker.id,
                        "work_order_id": blocker.work_order_id,
                    },
                    evidence=[
                        {
                            "type": "stale_blocker",
                            "label": blocker.title,
                            "detail": blocker.note,
                            "reported_at": blocker.reported_at.isoformat() if blocker.reported_at else None,
                        }
                    ],
                    impact={"expected": "Reduce aging WIP and improve schedule confidence."},
                    confidence_score=0.82,
                )
            )
            OperationalEventService(db).emit(
                company_id=blocker.company_id,
                event_type="work_order_blocker_escalated",
                source_module="shop_floor",
                entity_type="work_order_blocker",
                entity_id=blocker.id,
                work_order_id=blocker.work_order_id,
                operation_id=blocker.operation_id,
                severity="high",
                event_payload={"category": blocker.category, "title": blocker.title},
            )
            escalated += 1

        summary["stale_blockers_escalated"] = escalated
        db.commit()
        logger.info("AI learning aggregation complete: %s", summary)
        return summary
    except Exception as exc:
        logger.error("AI learning aggregation failed: %s", exc)
        db.rollback()
        raise
    finally:
        db.close()
