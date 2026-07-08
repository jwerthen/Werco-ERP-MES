"""Promote repeated field corrections into explicit learned-preference recommendations."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.models.ai_learning import AICorrection
from app.services.ai_learning_service import AILearningService
from app.services.ai_sensors.common import mint_recommendation, recommendation_open

WINDOW_DAYS = 60
MIN_CORRECTIONS = 3
MAX_RECS = 10


def _stable_value(value: Any) -> str:
    return repr(value)


def run_correction_preference_learner(db: Session, company_id: int) -> int:
    """When users keep correcting the same field to the same final value, teach it."""
    learning = AILearningService(db)
    cutoff = datetime.utcnow() - timedelta(days=WINDOW_DAYS)

    corrections = (
        db.query(AICorrection)
        .filter(AICorrection.company_id == company_id, AICorrection.created_at >= cutoff)
        .order_by(AICorrection.created_at.desc())
        .limit(500)
        .all()
    )

    # (source_module, field_path) -> Counter of final values
    buckets: dict[tuple[str, str], Counter] = {}
    for c in corrections:
        key = (c.source_module, c.field_path)
        buckets.setdefault(key, Counter())[_stable_value(c.final_value)] += 1

    created = 0
    for (source_module, field_path), counter in sorted(
        buckets.items(), key=lambda item: sum(item[1].values()), reverse=True
    ):
        if created >= MAX_RECS:
            break
        total = sum(counter.values())
        if total < MIN_CORRECTIONS:
            continue
        preferred_repr, count = counter.most_common(1)[0]
        if count < MIN_CORRECTIONS:
            continue
        # Recover one real final_value object matching the preferred repr
        preferred_value = None
        for c in corrections:
            if c.source_module == source_module and c.field_path == field_path and _stable_value(c.final_value) == preferred_repr:
                preferred_value = c.final_value
                break

        dedupe = f"learned_preference:{source_module}:{field_path}"
        if recommendation_open(
            learning,
            company_id=company_id,
            recommendation_type="learned_preference",
            source_module=source_module,
            target_entity_type="field_path",
            target_entity_id=None,
            dedupe_key=dedupe,
        ):
            continue

        mint_recommendation(
            db,
            company_id=company_id,
            source_module=source_module,
            recommendation_type="learned_preference",
            priority="medium",
            title=f"Learned preference: {field_path}",
            summary=(
                f"Users chose the same final value for `{field_path}` in {source_module} "
                f"{count}/{total} times in the last {WINDOW_DAYS} days. "
                "Use this as the default on the next AI draft (still human-reviewed)."
            ),
            rationale="Correction-preference learner from AICorrection history.",
            target_entity_type="field_path",
            target_entity_id=None,
            suggested_action={
                "type": "apply_preference_default",
                "source_module": source_module,
                "field_path": field_path,
                "preferred_value": preferred_value,
                "autonomy": "suggest_only",
                "dedupe_key": dedupe,
            },
            evidence=[
                {
                    "type": "correction_preference",
                    "field_path": field_path,
                    "preferred_count": count,
                    "total_corrections": total,
                    "window_days": WINDOW_DAYS,
                    "preferred_value": preferred_value,
                }
            ],
            impact={"expected": "Fewer repeated edits on AI drafts."},
            confidence_score=min(0.95, 0.5 + count * 0.08),
            expires_days=60,
        )
        created += 1
    return created


def list_active_preferences(db: Session, company_id: int, *, limit: int = 20) -> list[dict]:
    """Compact preference list for Copilot / RFQ context (accepted or pending learned prefs)."""
    from app.models.ai_learning import AIRecommendation

    rows = (
        db.query(AIRecommendation)
        .filter(
            AIRecommendation.company_id == company_id,
            AIRecommendation.recommendation_type == "learned_preference",
            AIRecommendation.status.in_(["pending", "accepted"]),
        )
        .order_by(AIRecommendation.confidence_score.desc(), AIRecommendation.created_at.desc())
        .limit(limit)
        .all()
    )
    prefs = []
    for rec in rows:
        action = rec.suggested_action or {}
        prefs.append(
            {
                "field_path": action.get("field_path"),
                "source_module": rec.source_module,
                "preferred_value": action.get("preferred_value"),
                "confidence": rec.confidence_score,
                "status": rec.status,
            }
        )
    return prefs
