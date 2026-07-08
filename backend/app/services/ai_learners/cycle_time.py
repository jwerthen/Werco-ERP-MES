"""Propose routing/standard time updates from actual vs estimated hours."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrderOperation
from app.services.ai_learning_service import AILearningService
from app.services.ai_sensors.common import mint_recommendation, recommendation_open

WINDOW_DAYS = 90
MIN_OPS = 5
# Flag when average actual/estimated is outside [0.75, 1.35]
LOW_RATIO = 0.75
HIGH_RATIO = 1.35
MAX_RECS = 10


def run_cycle_time_learner(db: Session, company_id: int) -> int:
    learning = AILearningService(db)
    cutoff = datetime.utcnow() - timedelta(days=WINDOW_DAYS)

    rows = (
        db.query(
            WorkOrderOperation.work_center_id,
            func.count(WorkOrderOperation.id).label("op_count"),
            func.avg(
                (WorkOrderOperation.actual_run_hours + WorkOrderOperation.actual_setup_hours)
                / func.nullif(
                    WorkOrderOperation.setup_time_hours + WorkOrderOperation.run_time_hours,
                    0,
                )
            ).label("avg_ratio"),
            func.avg(WorkOrderOperation.actual_run_hours + WorkOrderOperation.actual_setup_hours).label("avg_actual"),
            func.avg(WorkOrderOperation.setup_time_hours + WorkOrderOperation.run_time_hours).label("avg_standard"),
        )
        .filter(
            WorkOrderOperation.company_id == company_id,
            WorkOrderOperation.status == OperationStatus.COMPLETE,
            WorkOrderOperation.actual_end.isnot(None),
            WorkOrderOperation.actual_end >= cutoff,
            WorkOrderOperation.work_center_id.isnot(None),
        )
        .group_by(WorkOrderOperation.work_center_id)
        .having(func.count(WorkOrderOperation.id) >= MIN_OPS)
        .all()
    )

    created = 0
    for wc_id, op_count, avg_ratio, avg_actual, avg_standard in rows:
        if created >= MAX_RECS:
            break
        if avg_ratio is None:
            continue
        ratio = float(avg_ratio)
        if LOW_RATIO <= ratio <= HIGH_RATIO:
            continue
        if recommendation_open(
            learning,
            company_id=company_id,
            recommendation_type="standard_update",
            source_module="routing",
            target_entity_type="work_center",
            target_entity_id=wc_id,
        ):
            continue

        wc = db.query(WorkCenter).filter(WorkCenter.id == wc_id, WorkCenter.company_id == company_id).first()
        name = wc.name if wc else f"WC #{wc_id}"
        direction = "over" if ratio > 1 else "under"
        suggested_factor = round(ratio, 2)
        mint_recommendation(
            db,
            company_id=company_id,
            source_module="routing",
            recommendation_type="standard_update",
            priority="medium" if 0.6 < ratio < 1.6 else "high",
            title=f"Review time standards for {name}",
            summary=(
                f"{name} runs average {ratio:.0%} of standard times "
                f"({float(avg_actual or 0):.1f}h actual vs {float(avg_standard or 0):.1f}h standard) "
                f"across {int(op_count)} ops in {WINDOW_DAYS} days — consistently {direction}. "
                "Propose a routing standard update (draft only; not auto-applied)."
            ),
            rationale="Cycle-time learner from completed operations (no LLM).",
            target_entity_type="work_center",
            target_entity_id=wc_id,
            suggested_action={
                "type": "review_time_standards",
                "work_center_id": wc_id,
                "suggested_factor": suggested_factor,
                "href": "/work-centers",
                "autonomy": "suggest_only",
                "dedupe_key": f"standard_update:wc:{wc_id}",
            },
            evidence=[
                {
                    "type": "cycle_time_ratio",
                    "window_days": WINDOW_DAYS,
                    "op_count": int(op_count),
                    "avg_ratio": round(ratio, 3),
                    "avg_actual_hours": round(float(avg_actual or 0), 2),
                    "avg_standard_hours": round(float(avg_standard or 0), 2),
                }
            ],
            impact={"expected": "More accurate schedules and quotes.", "magnitude": min(2.0, abs(ratio - 1) + 1)},
            confidence_score=min(0.9, 0.5 + min(op_count, 20) * 0.02),
            expires_days=45,
        )
        created += 1
    return created
