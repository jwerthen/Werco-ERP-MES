"""Scrap / quality-trend sensor over recent completed work orders."""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.part import Part
from app.models.work_order import WorkOrder, WorkOrderStatus
from app.services.ai_learning_service import AILearningService
from app.services.ai_sensors.common import mint_recommendation, recommendation_open

WINDOW_DAYS = 30
MIN_JOBS = 3
SCRAP_RATE_THRESHOLD = 0.05  # 5%
MAX_RECS_PER_RUN = 15


def run_quality_trend_sensor(db: Session, company_id: int) -> int:
    """Mint recommend-only items when a part's recent scrap rate spikes."""
    learning = AILearningService(db)
    cutoff = datetime.utcnow() - timedelta(days=WINDOW_DAYS)

    rows = (
        db.query(
            WorkOrder.part_id,
            func.count(WorkOrder.id).label("job_count"),
            func.coalesce(func.sum(WorkOrder.quantity_complete), 0.0).label("qty_complete"),
            func.coalesce(func.sum(WorkOrder.quantity_scrapped), 0.0).label("qty_scrapped"),
        )
        .filter(
            WorkOrder.company_id == company_id,
            WorkOrder.is_deleted == False,  # noqa: E712
            WorkOrder.status.in_([WorkOrderStatus.COMPLETE, WorkOrderStatus.CLOSED]),
            WorkOrder.actual_end.isnot(None),
            WorkOrder.actual_end >= cutoff,
        )
        .group_by(WorkOrder.part_id)
        .having(func.count(WorkOrder.id) >= MIN_JOBS)
        .all()
    )

    created = 0
    for part_id, job_count, qty_complete, qty_scrapped in rows:
        if created >= MAX_RECS_PER_RUN:
            break
        complete = float(qty_complete or 0)
        scrapped = float(qty_scrapped or 0)
        produced = complete + scrapped
        if produced <= 0:
            continue
        scrap_rate = scrapped / produced
        if scrap_rate < SCRAP_RATE_THRESHOLD:
            continue

        if recommendation_open(
            learning,
            company_id=company_id,
            recommendation_type="quality_trend",
            source_module="quality",
            target_entity_type="part",
            target_entity_id=part_id,
        ):
            continue

        part = db.query(Part).filter(Part.id == part_id, Part.company_id == company_id).first()
        part_number = part.part_number if part else f"part#{part_id}"
        part_name = part.name if part else ""
        priority = "high" if scrap_rate >= 0.15 else "medium"

        mint_recommendation(
            db,
            company_id=company_id,
            source_module="quality",
            recommendation_type="quality_trend",
            priority=priority,
            title=f"Elevated scrap on {part_number}",
            summary=(
                f"{part_number} {f'({part_name}) ' if part_name else ''}"
                f"scrapped {scrapped:g} of {produced:g} pcs "
                f"({scrap_rate:.0%}) across {int(job_count)} jobs in the last {WINDOW_DAYS} days. "
                "Review process sheet, gauges, and recent NCRs."
            ),
            rationale="Deterministic scrap-rate sensor on completed work orders (no LLM).",
            target_entity_type="part",
            target_entity_id=part_id,
            suggested_action={
                "type": "create_draft_ncr",
                "part_id": part_id,
                "part_number": part_number,
                "title": f"Elevated scrap trend on {part_number}",
                "description": (
                    f"AI quality sensor flagged elevated scrap on {part_number} "
                    f"({scrap_rate:.0%} over {int(job_count)} jobs). Investigate process and gauges."
                ),
                "quantity_affected": max(scrapped, 1.0),
                "href": f"/quality?part={part_number}",
                "autonomy": "auto_execute",
                "dedupe_key": f"quality_trend:part:{part_id}",
            },
            evidence=[
                {
                    "type": "scrap_trend",
                    "window_days": WINDOW_DAYS,
                    "job_count": int(job_count),
                    "quantity_complete": complete,
                    "quantity_scrapped": scrapped,
                    "scrap_rate": round(scrap_rate, 4),
                    "threshold": SCRAP_RATE_THRESHOLD,
                }
            ],
            impact={
                "expected": "Reduce scrap cost and prevent quality escapes.",
                "magnitude": min(2.0, 1.0 + scrap_rate * 4),
            },
            confidence_score=min(0.95, 0.55 + scrap_rate + min(job_count, 10) * 0.02),
            expires_days=21,
        )
        created += 1

    return created
