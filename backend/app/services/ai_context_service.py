from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session, joinedload

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
            .options(joinedload(WorkOrder.part), joinedload(WorkOrder.operations).joinedload(WorkOrderOperation.work_center))
            .filter(WorkOrder.id == work_order_id, WorkOrder.company_id == company_id)
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
                    "reported_at": blocker.reported_at.isoformat() if blocker.reported_at else None,
                }
                for blocker in blockers
            ],
            "recent_events": [
                {
                    "event_type": event.event_type,
                    "source_module": event.source_module,
                    "severity": event.severity,
                    "occurred_at": event.occurred_at.isoformat() if event.occurred_at else None,
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
            return self.work_order_context(company_id=company_id, work_order_id=entity_id)

        counts = {
            "active_work_orders": self.db.query(WorkOrder)
            .filter(
                WorkOrder.company_id == company_id,
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
        return {"scope": "company", "counts": counts}

    def explain_context_sources(self) -> List[str]:
        return [
            "work_orders",
            "work_order_operations",
            "work_order_blockers",
            "operational_events",
            "parts",
            "work_centers",
        ]
