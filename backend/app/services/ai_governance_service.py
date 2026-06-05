from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.ai_learning import AIInteractionEvent, AIRecommendation
from app.models.operational_event import OperationalEvent
from app.models.work_order_blocker import WorkOrderBlocker, WorkOrderBlockerStatus


class AIGovernanceService:
    """Default AI autonomy and trust controls for the ERP."""

    CONTROLLED_RECORDS = {
        "production",
        "quality",
        "pricing",
        "purchasing",
        "compliance",
        "security",
        "scheduling",
    }

    AUTONOMY_TIERS = [
        {
            "tier": "observe",
            "description": "AI may read tenant-scoped context and summarize it.",
            "requires_approval": False,
        },
        {
            "tier": "draft",
            "description": "AI may prefill forms, draft text, and propose actions.",
            "requires_approval": True,
        },
        {
            "tier": "recommend",
            "description": "AI may create Action Inbox recommendations with evidence and confidence.",
            "requires_approval": True,
        },
        {
            "tier": "execute_controlled",
            "description": "AI cannot directly mutate controlled ERP records in v1.",
            "requires_approval": True,
            "enabled": False,
        },
    ]

    def __init__(self, db: Session):
        self.db = db

    def governance_snapshot(self, *, company_id: int) -> Dict[str, Any]:
        since = datetime.utcnow() - timedelta(days=30)
        pending_recommendations = (
            self.db.query(AIRecommendation)
            .filter(AIRecommendation.company_id == company_id, AIRecommendation.status == "pending")
            .count()
        )
        accepted_recommendations = (
            self.db.query(AIRecommendation)
            .filter(
                AIRecommendation.company_id == company_id,
                AIRecommendation.status == "accepted",
                AIRecommendation.acted_at >= since,
            )
            .count()
        )
        dismissed_recommendations = (
            self.db.query(AIRecommendation)
            .filter(
                AIRecommendation.company_id == company_id,
                AIRecommendation.status == "dismissed",
                AIRecommendation.acted_at >= since,
            )
            .count()
        )
        learning_events = (
            self.db.query(AIInteractionEvent)
            .filter(AIInteractionEvent.company_id == company_id, AIInteractionEvent.created_at >= since)
            .count()
        )
        open_blockers = (
            self.db.query(WorkOrderBlocker)
            .filter(
                WorkOrderBlocker.company_id == company_id,
                WorkOrderBlocker.status.in_(
                    [WorkOrderBlockerStatus.OPEN.value, WorkOrderBlockerStatus.ACKNOWLEDGED.value]
                ),
            )
            .count()
        )
        events_by_module = (
            self.db.query(OperationalEvent.source_module, func.count(OperationalEvent.id))
            .filter(OperationalEvent.company_id == company_id, OperationalEvent.occurred_at >= since)
            .group_by(OperationalEvent.source_module)
            .all()
        )

        return {
            "mode": "suggest_only",
            "controlled_records": sorted(self.CONTROLLED_RECORDS),
            "autonomy_tiers": self.AUTONOMY_TIERS,
            "approval_rules": [
                "AI recommendations never apply production, quality, pricing, purchasing, compliance, scheduling, or security changes by themselves.",
                "Every recommendation must include confidence, evidence, expected impact, and a target entity when applicable.",
                "Prompt/model versions and redacted context are retained for learning events and recommendations.",
            ],
            "metrics": {
                "window_days": 30,
                "pending_recommendations": pending_recommendations,
                "accepted_recommendations": accepted_recommendations,
                "dismissed_recommendations": dismissed_recommendations,
                "learning_events": learning_events,
                "open_blockers": open_blockers,
                "operational_events_by_module": {module: count for module, count in events_by_module},
            },
        }
