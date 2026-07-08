"""Deterministic domain sensors that mint Action Inbox recommendations.

Phase 1 of the always-on AI roadmap. Sensors run inside the nightly
``aggregate_ai_learning`` job (and the admin ``POST /ai/aggregate`` endpoint).
They use live ERP facts only — no LLM calls — so they stay CMMC-safe when
AI egress is off.
"""

from __future__ import annotations

from typing import Dict

from sqlalchemy.orm import Session

from app.services.ai_sensors.delivery import run_at_risk_delivery_sensor
from app.services.ai_sensors.inventory import run_inventory_risk_sensor
from app.services.ai_sensors.quality import run_quality_trend_sensor


def run_domain_sensors(db: Session, company_id: int) -> Dict[str, int]:
    """Run all Phase-1 sensors for one tenant. Returns per-type create counts."""
    return {
        "at_risk_delivery": run_at_risk_delivery_sensor(db, company_id),
        "inventory_risk": run_inventory_risk_sensor(db, company_id),
        "quality_trend": run_quality_trend_sensor(db, company_id),
    }
