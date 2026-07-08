"""Best-effort AI outcome capture on lifecycle events.

Phase 0 of the always-on AI roadmap: close the learning loop by recording
downstream business results (OTD, scrap, cost variance, quote win/loss)
without requiring a human to POST ``/ai/outcomes``.

Hard rules:
- Never raise into the caller (completion / quote mutation must not fail).
- Tenant-scoped only.
- No controlled ERP mutations — outcomes are learning telemetry only.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.models.quote import Quote, QuoteStatus
from app.models.work_order import WorkOrder
from app.schemas.ai_learning import AIOutcomeCreate
from app.services.ai_learning_service import AILearningService

logger = logging.getLogger(__name__)


def _safe_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def record_work_order_completion_outcomes(
    db: Session,
    *,
    company_id: int,
    work_order: WorkOrder,
    user_id: Optional[int] = None,
    source_module: str = "production",
) -> None:
    """Record OTD / scrap / cost outcomes when a WO reaches COMPLETE.

    Best-effort: failures are logged and swallowed.
    """
    try:
        learning = AILearningService(db)
        observed = datetime.utcnow()
        ordered = _safe_float(work_order.quantity_ordered) or 0.0
        complete = _safe_float(work_order.quantity_complete) or 0.0
        scrapped = _safe_float(work_order.quantity_scrapped) or 0.0
        produced = complete + scrapped

        # --- On-time delivery (1.0 = on time, 0.0 = late) -----------------------
        otd: Optional[float] = None
        days_late: Optional[float] = None
        if work_order.due_date:
            end_day = work_order.actual_end.date() if isinstance(work_order.actual_end, datetime) else date.today()
            days_late = float((end_day - work_order.due_date).days)
            otd = 1.0 if days_late <= 0 else 0.0
            learning.record_outcome(
                company_id=company_id,
                user=None,
                data=AIOutcomeCreate(
                    source_module=source_module,
                    outcome_type="on_time_delivery",
                    entity_type="work_order",
                    entity_id=work_order.id,
                    metric_name="on_time",
                    metric_value=otd,
                    baseline_value=1.0,
                    target_value=1.0,
                    outcome_payload={
                        "work_order_number": work_order.work_order_number,
                        "due_date": work_order.due_date.isoformat(),
                        "actual_end": end_day.isoformat(),
                        "days_late": days_late,
                        "user_id": user_id,
                    },
                    observed_at=observed,
                ),
            )

        # --- Scrap rate --------------------------------------------------------
        scrap_rate = (scrapped / produced) if produced > 0 else 0.0
        learning.record_outcome(
            company_id=company_id,
            user=None,
            data=AIOutcomeCreate(
                source_module=source_module,
                outcome_type="scrap_rate",
                entity_type="work_order",
                entity_id=work_order.id,
                metric_name="scrap_rate",
                metric_value=scrap_rate,
                baseline_value=0.0,
                target_value=0.0,
                outcome_payload={
                    "work_order_number": work_order.work_order_number,
                    "quantity_ordered": ordered,
                    "quantity_complete": complete,
                    "quantity_scrapped": scrapped,
                    "part_id": work_order.part_id,
                    "user_id": user_id,
                },
                observed_at=observed,
            ),
        )

        # --- Cost variance (actual vs estimated; lower actual is better) -------
        estimated = _safe_float(work_order.estimated_cost)
        actual = _safe_float(work_order.actual_cost)
        if estimated is not None and actual is not None and estimated > 0:
            # metric_value as efficiency ratio: estimated/actual (>=1 means under budget)
            efficiency = estimated / actual if actual > 0 else 1.0
            learning.record_outcome(
                company_id=company_id,
                user=None,
                data=AIOutcomeCreate(
                    source_module=source_module,
                    outcome_type="cost_variance",
                    entity_type="work_order",
                    entity_id=work_order.id,
                    metric_name="cost_efficiency",
                    metric_value=efficiency,
                    baseline_value=1.0,
                    target_value=1.0,
                    outcome_payload={
                        "work_order_number": work_order.work_order_number,
                        "estimated_cost": estimated,
                        "actual_cost": actual,
                        "variance": actual - estimated,
                        "user_id": user_id,
                    },
                    observed_at=observed,
                ),
            )
    except Exception:  # pragma: no cover - must never fail completion
        logger.exception(
            "AI outcome capture failed for WO %s (company %s)",
            getattr(work_order, "id", None),
            company_id,
        )


def record_quote_status_outcome(
    db: Session,
    *,
    company_id: int,
    quote: Quote,
    previous_status: Optional[str] = None,
    user_id: Optional[int] = None,
) -> None:
    """Record win/loss/convert outcomes when a quote reaches a terminal-ish status.

    Best-effort: failures are logged and swallowed.
    """
    try:
        status_value = quote.status.value if hasattr(quote.status, "value") else str(quote.status)
        terminal = {
            QuoteStatus.ACCEPTED.value,
            QuoteStatus.REJECTED.value,
            QuoteStatus.CONVERTED.value,
            QuoteStatus.EXPIRED.value,
        }
        if status_value not in terminal:
            return

        # Avoid duplicate noise when status is set to the same value twice
        if previous_status and previous_status == status_value:
            return

        win = 1.0 if status_value in {QuoteStatus.ACCEPTED.value, QuoteStatus.CONVERTED.value} else 0.0
        learning = AILearningService(db)
        learning.record_outcome(
            company_id=company_id,
            user=None,
            data=AIOutcomeCreate(
                source_module="quoting",
                outcome_type="quote_result",
                entity_type="quote",
                entity_id=quote.id,
                metric_name="win",
                metric_value=win,
                baseline_value=1.0,
                target_value=1.0,
                outcome_payload={
                    "quote_number": quote.quote_number,
                    "status": status_value,
                    "previous_status": previous_status,
                    "total": _safe_float(quote.total),
                    "lead_time_days": quote.lead_time_days,
                    "work_order_id": quote.work_order_id,
                    "user_id": user_id,
                },
                observed_at=datetime.utcnow(),
            ),
        )
    except Exception:  # pragma: no cover - must never fail quote mutation
        logger.exception(
            "AI outcome capture failed for quote %s (company %s)",
            getattr(quote, "id", None),
            company_id,
        )


def summarize_payload_for_log(payload: Dict[str, Any]) -> str:
    """Tiny helper for tests/debug — keep keys only."""
    return ",".join(sorted(str(k) for k in payload.keys()))
