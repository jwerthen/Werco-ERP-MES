from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.ai_learning import AIInteractionEvent, AIRecommendation
from app.models.operational_event import OperationalEvent
from app.models.work_order_blocker import WorkOrderBlocker, WorkOrderBlockerStatus
from app.services import llm_client
from app.services.ai_action_applier import DEFAULT_APPLY_ALLOWLIST


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

    # Ruling 3 (2026-11-05): there is no auto tier. ``apply_on_accept`` is built per call,
    # because whether it is enabled is the company's AI egress switch.
    BASE_AUTONOMY_TIERS: List[Dict[str, Any]] = [
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
    ]

    APPLY_ON_ACCEPT_DESCRIPTION = (
        "A named person clicks Accept & apply; the server shows what will run, then runs it as that "
        "person, audit-logged. Never automatic. Off when company AI egress is off."
    )

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

        egress_enabled = llm_client.ai_egress_allowed(company_id)
        autonomy_tiers = [dict(tier) for tier in self.BASE_AUTONOMY_TIERS] + [
            {
                "tier": "apply_on_accept",
                "description": self.APPLY_ON_ACCEPT_DESCRIPTION,
                "requires_approval": True,
                "enabled": egress_enabled,
                "allowlist": sorted(DEFAULT_APPLY_ALLOWLIST),
            }
        ]

        return {
            "mode": "apply_on_accept",
            "ai_egress_enabled": egress_enabled,
            "controlled_records": sorted(self.CONTROLLED_RECORDS),
            "autonomy_tiers": autonomy_tiers,
            "apply_allowlist": sorted(DEFAULT_APPLY_ALLOWLIST),
            "approval_rules": [
                "Nothing AI-driven executes without a named person clicking Accept & apply (owner ruling 2026-11-05).",
                "Accept & apply runs only from a signed-in person's session -- never from the nightly job, an "
                "agent or an API token -- and only when that person's role could perform the action.",
                "The server shows exactly what will run before the click and refuses if it changed since.",
                "When company AI egress is off, nothing is applied; cards can still be marked handled.",
                "Only allowlisted action types may apply; review-only types stay in Action Inbox.",
                "Every recommendation must include confidence, evidence, expected impact, and a target entity when applicable.",
                "Prompt/model versions and redacted context are retained for learning events and recommendations.",
                "Applied actions are audit-logged as the accepting person, marked with the recommendation they came from.",
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
