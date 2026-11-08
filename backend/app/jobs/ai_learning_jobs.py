import logging
import os

from app.db.session import SessionLocal
from app.models.ai_learning import AIRecommendation
from app.services.ai_action_applier import resolve_plan
from app.services.ai_learning_service import AILearningService
from app.services.operational_event_service import OperationalEventService
from app.services.work_order_blocker_service import WorkOrderBlockerService

logger = logging.getLogger(__name__)

# Env vars of the always-on auto-execute agent, removed by owner ruling 3 (2026-11-05:
# nothing AI-driven executes without a named person clicking Accept). They are IGNORED;
# setting one only produces a warning so an operator learns it no longer does anything.
RETIRED_AUTO_EXECUTE_ENV_VARS = (
    "AI_AUTO_EXECUTE_ENABLED",
    "AI_AUTO_EXECUTE_MIN_CONFIDENCE",
    "AI_AUTO_EXECUTE_FALLBACK_MIN_CONFIDENCE",
    "AI_AUTO_EXECUTE_MAX_BATCH",
    "AI_AUTO_EXECUTE_LLM_TIMEOUT",
    "ANTHROPIC_AUTO_EXECUTE_MODEL",
)

# One escalation card per blocker lifetime: a pending, snoozed, accepted or dismissed one
# all suppress a re-mint (and its critical notification). Only a STALE card -- retired by
# fact (closed target, superseded) -- does not, and those are re-checked by the no-op skip.
ESCALATION_DEDUPE_STATUSES = ("pending", "snoozed", "accepted", "dismissed")


def _warn_retired_auto_execute_env() -> None:
    present = [name for name in RETIRED_AUTO_EXECUTE_ENV_VARS if os.environ.get(name) is not None]
    if present:
        logger.warning(
            "Ignoring retired setting(s) %s: AI auto-execute was removed by owner ruling 2026-11-05 "
            "(nothing AI-driven runs without a person clicking Accept & apply). Delete these variables.",
            ", ".join(present),
        )


async def aggregate_ai_learning_task():
    """Mint and sweep AI recommendations nightly. Never applies one and mutates no ERP record.

    Runs the learning aggregation (expiry, snooze wake, closed-target sweep, friction and
    correction recs, sensors, morning brief, learners), then mints at most one
    ``stale_blocker_escalation`` card per blocker open for more than 24 hours on a live job
    -- skipped when escalating would change nothing. Every card waits for a person's
    Accept & apply (owner ruling 3).
    """
    _warn_retired_auto_execute_env()
    db = SessionLocal()
    try:
        summary = AILearningService(db).aggregate_learning_signals()
        stale_blockers = WorkOrderBlockerService(db).stale_open_blockers(older_than_hours=24)
        escalated = 0
        for blocker in stale_blockers:
            existing = (
                db.query(AIRecommendation.id)
                .filter(
                    AIRecommendation.company_id == blocker.company_id,
                    AIRecommendation.source_module == "shop_floor",
                    AIRecommendation.recommendation_type == "stale_blocker_escalation",
                    AIRecommendation.target_entity_type == "work_order_blocker",
                    AIRecommendation.target_entity_id == blocker.id,
                    AIRecommendation.status.in_(ESCALATION_DEDUPE_STATUSES),
                )
                .first()
            )
            if existing:
                continue
            suggested_action = {
                "type": "escalate_blocker",
                "blocker_id": blocker.id,
                "work_order_id": blocker.work_order_id,
                "href": f"/work-orders/{blocker.work_order_id}",
                "autonomy": "apply_on_accept",
            }
            plan = resolve_plan(
                db,
                blocker.company_id,
                suggested_action,
                target_entity_type="work_order_blocker",
                target_entity_id=blocker.id,
            )
            if plan.blocked:
                # Already acknowledged at high/critical with the job at P2 or better (or the
                # job is finished): a card would offer nothing, and the critical
                # notification below would be noise.
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
                    suggested_action=suggested_action,
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
            # Deterministic alert (not an AI mutation): fires only when a card is minted.
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
