"""Flag systematic estimate vs actual cost variance for quote calibration."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.part import Part
from app.models.work_order import WorkOrder, WorkOrderStatus
from app.services.ai_learning_service import AILearningService
from app.services.ai_sensors.common import mint_recommendation, recommendation_open

WINDOW_DAYS = 90
MIN_JOBS = 3
VARIANCE_THRESHOLD = 0.20  # 20%
MAX_RECS = 10


def run_estimate_calibration_learner(db: Session, company_id: int) -> int:
    learning = AILearningService(db)
    cutoff = datetime.utcnow() - timedelta(days=WINDOW_DAYS)

    rows = (
        db.query(
            WorkOrder.part_id,
            func.count(WorkOrder.id).label("job_count"),
            func.avg(WorkOrder.actual_cost).label("avg_actual"),
            func.avg(WorkOrder.estimated_cost).label("avg_estimated"),
        )
        .filter(
            WorkOrder.company_id == company_id,
            WorkOrder.is_deleted == False,  # noqa: E712
            WorkOrder.status.in_([WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED]),
            WorkOrder.actual_end.isnot(None),
            WorkOrder.actual_end >= cutoff,
            WorkOrder.estimated_cost.isnot(None),
            WorkOrder.actual_cost.isnot(None),
            WorkOrder.estimated_cost > 0,
            WorkOrder.actual_cost > 0,
        )
        .group_by(WorkOrder.part_id)
        .having(func.count(WorkOrder.id) >= MIN_JOBS)
        .all()
    )

    created = 0
    for part_id, job_count, avg_actual, avg_estimated in rows:
        if created >= MAX_RECS:
            break
        est = float(avg_estimated or 0)
        act = float(avg_actual or 0)
        if est <= 0:
            continue
        variance = (act - est) / est
        if abs(variance) < VARIANCE_THRESHOLD:
            continue
        if recommendation_open(
            learning,
            company_id=company_id,
            recommendation_type="estimate_calibration",
            source_module="quoting",
            target_entity_type="part",
            target_entity_id=part_id,
        ):
            continue

        part = db.query(Part).filter(Part.id == part_id, Part.company_id == company_id).first()
        pn = part.part_number if part else f"part#{part_id}"
        direction = "under" if variance > 0 else "over"
        factor = round(act / est, 3)
        mint_recommendation(
            db,
            company_id=company_id,
            source_module="quoting",
            recommendation_type="estimate_calibration",
            priority="high" if abs(variance) >= 0.35 else "medium",
            title=f"Calibrate estimates for {pn}",
            summary=(
                f"{pn} actual cost averages {act:.0f} vs estimated {est:.0f} "
                f"({variance:+.0%}) over {int(job_count)} jobs. Quotes appear to {direction}-estimate. "
                f"Suggested cost factor ≈ {factor}."
            ),
            rationale="Estimate calibration learner from completed job costs (no LLM).",
            target_entity_type="part",
            target_entity_id=part_id,
            suggested_action={
                "type": "review_estimate_factor",
                "part_id": part_id,
                "suggested_factor": factor,
                "href": f"/parts/{part_id}",
                "autonomy": "suggest_only",
                "dedupe_key": f"estimate_calibration:part:{part_id}",
            },
            evidence=[
                {
                    "type": "cost_variance",
                    "window_days": WINDOW_DAYS,
                    "job_count": int(job_count),
                    "avg_actual": round(act, 2),
                    "avg_estimated": round(est, 2),
                    "variance": round(variance, 4),
                    "suggested_factor": factor,
                }
            ],
            impact={
                "expected": "Improve quote win-rate and margin accuracy.",
                "magnitude": min(2.0, 1 + abs(variance)),
            },
            confidence_score=min(0.92, 0.55 + min(job_count, 15) * 0.02),
            expires_days=45,
        )
        created += 1
    return created
