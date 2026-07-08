from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session, joinedload

from app.core.time_utils import to_utc_iso
from app.models.operational_event import OperationalEvent
from app.models.part import Part
from app.models.work_order import WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.models.work_order_blocker import WorkOrderBlocker, WorkOrderBlockerStatus


class AIContextService:
    """Tenant-scoped context assembly for copilots, NL search, and recommendations."""

    def __init__(self, db: Session):
        self.db = db

    def work_order_context(self, *, company_id: int, work_order_id: int) -> Dict[str, Any]:
        work_order = (
            self.db.query(WorkOrder)
            .options(
                joinedload(WorkOrder.part), joinedload(WorkOrder.operations).joinedload(WorkOrderOperation.work_center)
            )
            .filter(
                WorkOrder.id == work_order_id,
                WorkOrder.company_id == company_id,
                WorkOrder.is_deleted == False,  # noqa: E712 — WorkOrder is soft-delete
            )
            .first()
        )
        if not work_order:
            raise ValueError("Work order not found")

        blockers = (
            self.db.query(WorkOrderBlocker)
            .filter(
                WorkOrderBlocker.company_id == company_id,
                WorkOrderBlocker.work_order_id == work_order.id,
                WorkOrderBlocker.status.in_(
                    [WorkOrderBlockerStatus.OPEN.value, WorkOrderBlockerStatus.ACKNOWLEDGED.value]
                ),
            )
            .order_by(WorkOrderBlocker.reported_at.desc())
            .limit(20)
            .all()
        )
        events = (
            self.db.query(OperationalEvent)
            .filter(OperationalEvent.company_id == company_id, OperationalEvent.work_order_id == work_order.id)
            .order_by(OperationalEvent.occurred_at.desc())
            .limit(20)
            .all()
        )

        return {
            "work_order": {
                "id": work_order.id,
                "number": work_order.work_order_number,
                "status": work_order.status.value if hasattr(work_order.status, "value") else work_order.status,
                "priority": work_order.priority,
                "due_date": work_order.due_date.isoformat() if work_order.due_date else None,
                "is_late": bool(work_order.due_date and work_order.due_date < date.today()),
                "part": {
                    "id": work_order.part.id if work_order.part else None,
                    "part_number": work_order.part.part_number if work_order.part else None,
                    "name": work_order.part.name if work_order.part else None,
                },
            },
            "operations": [
                {
                    "id": op.id,
                    "sequence": op.sequence,
                    "name": op.name,
                    "status": op.status.value if hasattr(op.status, "value") else op.status,
                    "work_center": op.work_center.name if op.work_center else None,
                    "work_center_type": op.work_center.work_center_type if op.work_center else None,
                }
                for op in sorted(work_order.operations, key=lambda item: item.sequence)
            ],
            "open_blockers": [
                {
                    "id": blocker.id,
                    "category": blocker.category,
                    "severity": blocker.severity,
                    "title": blocker.title,
                    "note": blocker.note,
                    "reported_at": to_utc_iso(blocker.reported_at),
                }
                for blocker in blockers
            ],
            "recent_events": [
                {
                    "event_type": event.event_type,
                    "source_module": event.source_module,
                    "severity": event.severity,
                    "occurred_at": to_utc_iso(event.occurred_at),
                    "payload": event.event_payload or {},
                }
                for event in events
            ],
        }

    def compact_entity_context(
        self,
        *,
        company_id: int,
        entity_type: Optional[str] = None,
        entity_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        if entity_type == "work_order" and entity_id:
            ctx = self.work_order_context(company_id=company_id, work_order_id=entity_id)
            ctx["learned_preferences"] = self.learned_preferences(company_id=company_id, limit=10)
            ctx["pending_recommendations"] = self.pending_recommendations_for(
                company_id=company_id, entity_type="work_order", entity_id=entity_id, limit=5
            )
            return ctx

        if entity_type == "part" and entity_id:
            return {
                "scope": "part",
                "part_id": entity_id,
                "learned_preferences": self.learned_preferences(company_id=company_id, limit=10),
                "pending_recommendations": self.pending_recommendations_for(
                    company_id=company_id, entity_type="part", entity_id=entity_id, limit=5
                ),
                "estimate_calibration": self._latest_estimate_factor(company_id=company_id, part_id=entity_id),
            }

        counts = {
            "active_work_orders": self.db.query(WorkOrder)
            .filter(
                WorkOrder.company_id == company_id,
                WorkOrder.is_deleted == False,  # noqa: E712 — WorkOrder is soft-delete
                WorkOrder.status.in_([WorkOrderStatus.RELEASED, WorkOrderStatus.IN_PROGRESS, WorkOrderStatus.ON_HOLD]),
            )
            .count(),
            "open_blockers": self.db.query(WorkOrderBlocker)
            .filter(
                WorkOrderBlocker.company_id == company_id,
                WorkOrderBlocker.status.in_(
                    [WorkOrderBlockerStatus.OPEN.value, WorkOrderBlockerStatus.ACKNOWLEDGED.value]
                ),
            )
            .count(),
            "parts": self.db.query(Part).filter(Part.company_id == company_id, Part.is_active == True).count(),
        }
        return {
            "scope": "company",
            "counts": counts,
            "learned_preferences": self.learned_preferences(company_id=company_id, limit=15),
        }

    def learned_preferences(self, *, company_id: int, limit: int = 15) -> List[Dict[str, Any]]:
        from app.services.ai_learners.preferences import list_active_preferences

        return list_active_preferences(self.db, company_id, limit=limit)

    def pending_recommendations_for(
        self,
        *,
        company_id: int,
        entity_type: str,
        entity_id: int,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        from app.models.ai_learning import AIRecommendation

        rows = (
            self.db.query(AIRecommendation)
            .filter(
                AIRecommendation.company_id == company_id,
                AIRecommendation.status == "pending",
                AIRecommendation.target_entity_type == entity_type,
                AIRecommendation.target_entity_id == entity_id,
            )
            .order_by(AIRecommendation.priority.asc(), AIRecommendation.confidence_score.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "id": r.id,
                "type": r.recommendation_type,
                "title": r.title,
                "priority": r.priority,
                "confidence": r.confidence_score,
                "action_type": (r.suggested_action or {}).get("type"),
                "autonomy": (r.suggested_action or {}).get("autonomy"),
            }
            for r in rows
        ]

    def _latest_estimate_factor(self, *, company_id: int, part_id: int) -> Optional[Dict[str, Any]]:
        from app.models.ai_learning import AIRecommendation

        rec = (
            self.db.query(AIRecommendation)
            .filter(
                AIRecommendation.company_id == company_id,
                AIRecommendation.recommendation_type == "estimate_calibration",
                AIRecommendation.target_entity_type == "part",
                AIRecommendation.target_entity_id == part_id,
                AIRecommendation.status.in_(["pending", "accepted"]),
            )
            .order_by(AIRecommendation.created_at.desc())
            .first()
        )
        if not rec:
            return None
        action = rec.suggested_action or {}
        return {
            "suggested_factor": action.get("suggested_factor"),
            "confidence": rec.confidence_score,
            "status": rec.status,
            "summary": rec.summary,
        }

    def explain_context_sources(self) -> List[str]:
        return [
            "work_orders",
            "work_order_operations",
            "work_order_blockers",
            "operational_events",
            "parts",
            "work_centers",
            "learned_preferences",
            "pending_recommendations",
            "estimate_calibration",
        ]
