"""Continuous learners that turn plant outcomes into draft improvement proposals (Phase 4)."""

from __future__ import annotations

from typing import Dict

from sqlalchemy.orm import Session

from app.services.ai_learners.cycle_time import run_cycle_time_learner
from app.services.ai_learners.estimate_calibration import run_estimate_calibration_learner
from app.services.ai_learners.preferences import run_correction_preference_learner


def run_domain_learners(db: Session, company_id: int) -> Dict[str, int]:
    return {
        "cycle_time": run_cycle_time_learner(db, company_id),
        "estimate_calibration": run_estimate_calibration_learner(db, company_id),
        "correction_preference": run_correction_preference_learner(db, company_id),
    }
