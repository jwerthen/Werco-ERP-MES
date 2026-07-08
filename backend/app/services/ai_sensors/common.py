"""Shared helpers for domain sensors."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.models.ai_learning import AIRecommendation
from app.services.ai_learning_service import AILearningService


def recommendation_open(
    learning: AILearningService,
    *,
    company_id: int,
    recommendation_type: str,
    source_module: str,
    target_entity_type: Optional[str],
    target_entity_id: Optional[int],
    dedupe_key: Optional[str] = None,
) -> bool:
    return learning._open_recommendation_exists(  # noqa: SLF001 — intentional sensor seam
        company_id,
        recommendation_type,
        source_module,
        target_entity_type,
        target_entity_id,
        dedupe_key,
    )


def mint_recommendation(
    db: Session,
    *,
    company_id: int,
    source_module: str,
    recommendation_type: str,
    priority: str,
    title: str,
    summary: str,
    rationale: Optional[str],
    target_entity_type: Optional[str],
    target_entity_id: Optional[int],
    suggested_action: Dict[str, Any],
    evidence: List[Dict[str, Any]],
    impact: Dict[str, Any],
    confidence_score: float,
    expires_days: int = 14,
) -> AIRecommendation:
    rec = AIRecommendation(
        company_id=company_id,
        source_module=source_module,
        recommendation_type=recommendation_type,
        status="pending",
        priority=priority,
        title=title,
        summary=summary,
        rationale=rationale,
        target_entity_type=target_entity_type,
        target_entity_id=target_entity_id,
        suggested_action=suggested_action,
        evidence=evidence,
        impact=impact,
        confidence_score=max(0.0, min(float(confidence_score), 1.0)),
        expires_at=datetime.utcnow() + timedelta(days=expires_days),
    )
    db.add(rec)
    return rec
