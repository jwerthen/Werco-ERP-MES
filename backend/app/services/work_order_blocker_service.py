from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Optional

from sqlalchemy.orm import Session, joinedload

from app.models.ai_learning import AIRecommendation
from app.models.notification import NotificationLog
from app.models.user import User, UserRole
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation
from app.models.work_order_blocker import (
    WorkOrderBlocker,
    WorkOrderBlockerCategory,
    WorkOrderBlockerStatus,
)
from app.schemas.work_order_blocker import WorkOrderBlockerCreate, WorkOrderBlockerUpdate
from app.services.audit_service import AuditService
from app.services.operational_event_service import OperationalEventService, redact_event_payload


def _enum_value(value) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _blocker_default_title(category: str, work_order: WorkOrder, operation: Optional[WorkOrderOperation]) -> str:
    label = category.replace("_", " ").title()
    target = operation.name if operation else work_order.work_order_number
    return f"{label}: {target}"


class WorkOrderBlockerService:
    """First-class shop-floor blocker service with notifications and AI signals."""

    def __init__(self, db: Session):
        self.db = db

    def list_blockers(
        self,
        *,
        company_id: int,
        work_order_id: Optional[int] = None,
        status: Optional[str] = None,
        category: Optional[str] = None,
        limit: int = 100,
    ) -> List[WorkOrderBlocker]:
        query = (
            self.db.query(WorkOrderBlocker)
            .options(
                joinedload(WorkOrderBlocker.work_order),
                joinedload(WorkOrderBlocker.operation),
                joinedload(WorkOrderBlocker.material_part),
            )
            .filter(WorkOrderBlocker.company_id == company_id)
        )
        if work_order_id is not None:
            query = query.filter(WorkOrderBlocker.work_order_id == work_order_id)
        if status:
            query = query.filter(WorkOrderBlocker.status == status)
        if category:
            query = query.filter(WorkOrderBlocker.category == category)
        return query.order_by(WorkOrderBlocker.reported_at.desc()).limit(limit).all()

    def create_blocker(
        self,
        *,
        company_id: int,
        user: User,
        work_order_id: int,
        data: WorkOrderBlockerCreate,
        audit: Optional[AuditService] = None,
        source: Optional[str] = None,
    ) -> WorkOrderBlocker:
        # ``source`` is the A0.1 adoption-telemetry client channel
        # (kiosk/desktop/scanner/import/backfill) when the triggering request
        # supplied one; None means unknown/not reported (e.g. office paths).
        # Passed through to the work_order_blocker_created event payload only.
        work_order = (
            self.db.query(WorkOrder)
            .options(joinedload(WorkOrder.operations))
            .filter(WorkOrder.id == work_order_id, WorkOrder.company_id == company_id)
            .first()
        )
        if not work_order:
            raise ValueError("Work order not found")

        operation = None
        if data.operation_id:
            operation = (
                self.db.query(WorkOrderOperation)
                .filter(
                    WorkOrderOperation.id == data.operation_id,
                    WorkOrderOperation.work_order_id == work_order.id,
                    WorkOrderOperation.company_id == company_id,
                )
                .first()
            )
            if not operation:
                raise ValueError("Operation not found for this work order")

        category = _enum_value(data.category)
        severity = _enum_value(data.severity)
        blocker = WorkOrderBlocker(
            company_id=company_id,
            work_order_id=work_order.id,
            operation_id=operation.id if operation else None,
            material_part_id=data.material_part_id,
            category=category,
            severity=severity,
            status=WorkOrderBlockerStatus.OPEN.value,
            title=data.title or _blocker_default_title(category, work_order, operation),
            note=redact_event_payload(data.note),
            reported_by=user.id,
            assigned_to=data.assigned_to,
        )
        self.db.add(blocker)
        self.db.flush()

        operation_previous_status = None
        if data.put_operation_on_hold and operation and operation.status != OperationStatus.COMPLETE:
            operation_previous_status = _enum_value(operation.status)
            operation.status = OperationStatus.ON_HOLD
            operation.updated_at = datetime.utcnow()

        OperationalEventService(self.db).emit(
            company_id=company_id,
            event_type="work_order_blocker_created",
            source_module="work_orders",
            entity_type="work_order_blocker",
            entity_id=blocker.id,
            work_order_id=work_order.id,
            operation_id=operation.id if operation else None,
            user_id=user.id,
            severity=severity,
            event_payload={
                "category": category,
                "title": blocker.title,
                "work_order_number": work_order.work_order_number,
                "operation_name": operation.name if operation else None,
                # A0.1 adoption telemetry: client channel (None = not reported).
                "source": source,
            },
        )
        self._create_notification_logs(company_id=company_id, blocker=blocker, work_order=work_order)
        self._create_blocker_recommendation(company_id=company_id, user=user, blocker=blocker, work_order=work_order)

        # Tamper-evident audit trail (hash chain): the blocker creation and any
        # operation status mutation it triggers. Flushed (not committed) so the
        # audit rows commit atomically with the blocker via the caller's commit.
        if audit is not None:
            audit.log_create(
                "work_order_blocker",
                blocker.id,
                blocker.title,
                new_values=blocker,
                description=(
                    f"Reported {category} blocker on work order {work_order.work_order_number}"
                    + (f" operation {operation.name}" if operation else "")
                ),
            )
            if operation is not None and operation_previous_status is not None:
                audit.log_status_change(
                    "work_order_operation",
                    operation.id,
                    operation.name or str(operation.id),
                    operation_previous_status,
                    _enum_value(operation.status),
                    description=(
                        f"Operation {operation.name} put on hold by blocker {blocker.title} "
                        f"on work order {work_order.work_order_number}"
                    ),
                )

        self.db.flush()
        return blocker

    def update_blocker(
        self,
        *,
        company_id: int,
        user: User,
        blocker_id: int,
        data: WorkOrderBlockerUpdate,
        audit: Optional[AuditService] = None,
    ) -> WorkOrderBlocker:
        blocker = self._get_blocker(company_id=company_id, blocker_id=blocker_id)
        previous_status = blocker.status
        update_data = data.model_dump(exclude_unset=True)
        for field, value in update_data.items():
            if value is None:
                continue
            setattr(blocker, field, _enum_value(value) if hasattr(value, "value") else value)

        resumed_operation = None
        resumed_operation_previous_status = None
        if blocker.status == WorkOrderBlockerStatus.ACKNOWLEDGED.value and previous_status != blocker.status:
            blocker.acknowledged_at = datetime.utcnow()
        if blocker.status in {WorkOrderBlockerStatus.RESOLVED.value, WorkOrderBlockerStatus.DISMISSED.value}:
            blocker.resolved_at = blocker.resolved_at or datetime.utcnow()
            blocker.resolved_by = blocker.resolved_by or user.id
            resumed_operation, resumed_operation_previous_status = self._resume_operation_if_no_open_blockers(blocker)
        blocker.updated_at = datetime.utcnow()

        OperationalEventService(self.db).emit(
            company_id=company_id,
            event_type="work_order_blocker_updated",
            source_module="work_orders",
            entity_type="work_order_blocker",
            entity_id=blocker.id,
            work_order_id=blocker.work_order_id,
            operation_id=blocker.operation_id,
            user_id=user.id,
            severity=blocker.severity,
            event_payload={"status": blocker.status, "previous_status": previous_status},
        )

        # Tamper-evident audit trail (hash chain): the blocker status transition
        # and any operation resume it triggers. Flushed (not committed) so the
        # audit rows commit atomically with the blocker via the caller's commit.
        if audit is not None and blocker.status != previous_status:
            audit.log_status_change(
                "work_order_blocker",
                blocker.id,
                blocker.title,
                previous_status,
                blocker.status,
                description=f"Blocker '{blocker.title}' status changed from '{previous_status}' to '{blocker.status}'",
            )
        if audit is not None and resumed_operation is not None and resumed_operation_previous_status is not None:
            audit.log_status_change(
                "work_order_operation",
                resumed_operation.id,
                resumed_operation.name or str(resumed_operation.id),
                resumed_operation_previous_status,
                _enum_value(resumed_operation.status),
                description=(
                    f"Operation {resumed_operation.name} resumed after blocker '{blocker.title}' was "
                    f"{blocker.status}"
                ),
            )

        self.db.flush()
        return blocker

    def resolve_blocker(
        self,
        *,
        company_id: int,
        user: User,
        blocker_id: int,
        resolution_note: Optional[str] = None,
        audit: Optional[AuditService] = None,
    ) -> WorkOrderBlocker:
        return self.update_blocker(
            company_id=company_id,
            user=user,
            blocker_id=blocker_id,
            data=WorkOrderBlockerUpdate(
                status=WorkOrderBlockerStatus.RESOLVED,
                resolution_note=resolution_note,
            ),
            audit=audit,
        )

    def stale_open_blockers(
        self, *, company_id: Optional[int] = None, older_than_hours: int = 24
    ) -> List[WorkOrderBlocker]:
        cutoff = datetime.utcnow() - timedelta(hours=older_than_hours)
        query = self.db.query(WorkOrderBlocker).filter(
            WorkOrderBlocker.status.in_([WorkOrderBlockerStatus.OPEN.value, WorkOrderBlockerStatus.ACKNOWLEDGED.value]),
            WorkOrderBlocker.reported_at <= cutoff,
        )
        if company_id is not None:
            query = query.filter(WorkOrderBlocker.company_id == company_id)
        return query.order_by(WorkOrderBlocker.reported_at.asc()).all()

    def _get_blocker(self, *, company_id: int, blocker_id: int) -> WorkOrderBlocker:
        blocker = (
            self.db.query(WorkOrderBlocker)
            .options(
                joinedload(WorkOrderBlocker.work_order),
                joinedload(WorkOrderBlocker.operation),
                joinedload(WorkOrderBlocker.material_part),
            )
            .filter(WorkOrderBlocker.id == blocker_id, WorkOrderBlocker.company_id == company_id)
            .first()
        )
        if not blocker:
            raise ValueError("Blocker not found")
        return blocker

    def _resume_operation_if_no_open_blockers(
        self, blocker: WorkOrderBlocker
    ) -> tuple[Optional[WorkOrderOperation], Optional[str]]:
        """Resume the operation if no other open blockers remain.

        Returns ``(operation, previous_status)`` when an operation was actually
        resumed (for audit logging by the caller), otherwise ``(None, None)``.
        """
        if not blocker.operation_id:
            return None, None
        open_count = (
            self.db.query(WorkOrderBlocker)
            .filter(
                WorkOrderBlocker.company_id == blocker.company_id,
                WorkOrderBlocker.operation_id == blocker.operation_id,
                WorkOrderBlocker.id != blocker.id,
                WorkOrderBlocker.status.in_(
                    [WorkOrderBlockerStatus.OPEN.value, WorkOrderBlockerStatus.ACKNOWLEDGED.value]
                ),
            )
            .count()
        )
        if open_count:
            return None, None
        operation = (
            self.db.query(WorkOrderOperation)
            .filter(
                WorkOrderOperation.id == blocker.operation_id,
                WorkOrderOperation.company_id == blocker.company_id,
            )
            .first()
        )
        if operation and operation.status == OperationStatus.ON_HOLD:
            previous_status = _enum_value(operation.status)
            operation.status = OperationStatus.IN_PROGRESS if operation.actual_start else OperationStatus.READY
            operation.updated_at = datetime.utcnow()
            return operation, previous_status
        return None, None

    def _create_notification_logs(self, *, company_id: int, blocker: WorkOrderBlocker, work_order: WorkOrder) -> None:
        roles = [UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]
        departments = []
        if blocker.category == WorkOrderBlockerCategory.MATERIAL_MISSING.value:
            departments = ["Purchasing", "Inventory"]

        query = self.db.query(User).filter(User.company_id == company_id, User.is_active == True)
        recipients = query.filter(User.role.in_(roles)).all()
        if departments:
            recipients.extend(
                self.db.query(User)
                .filter(User.company_id == company_id, User.is_active == True, User.department.in_(departments))
                .all()
            )
        unique_recipients = {user.id: user for user in recipients}

        for recipient in unique_recipients.values():
            self.db.add(
                NotificationLog(
                    company_id=company_id,
                    user_id=recipient.id,
                    event_type="WO_BLOCKED",
                    channel="in_app",
                    subject=f"Blocked: {work_order.work_order_number}",
                    body=f"{blocker.title}{': ' + blocker.note if blocker.note else ''}",
                    sent=True,
                    related_type="WorkOrderBlocker",
                    related_id=blocker.id,
                )
            )

    def _create_blocker_recommendation(
        self,
        *,
        company_id: int,
        user: User,
        blocker: WorkOrderBlocker,
        work_order: WorkOrder,
    ) -> None:
        recommendation_type = (
            "material_blocker_triage"
            if blocker.category == WorkOrderBlockerCategory.MATERIAL_MISSING.value
            else "shop_floor_blocker_triage"
        )
        existing = (
            self.db.query(AIRecommendation)
            .filter(
                AIRecommendation.company_id == company_id,
                AIRecommendation.source_module == "shop_floor",
                AIRecommendation.recommendation_type == recommendation_type,
                AIRecommendation.target_entity_type == "work_order_blocker",
                AIRecommendation.target_entity_id == blocker.id,
            )
            .first()
        )
        if existing:
            return

        self.db.add(
            AIRecommendation(
                company_id=company_id,
                source_module="shop_floor",
                recommendation_type=recommendation_type,
                status="pending",
                priority="high" if blocker.severity in {"high", "critical"} else "medium",
                title=f"Clear blocker on {work_order.work_order_number}",
                summary=(
                    "Material is blocking this work order. Review inventory, open POs, and alternate stock."
                    if blocker.category == WorkOrderBlockerCategory.MATERIAL_MISSING.value
                    else "A shop-floor blocker was reported. Review ownership and next action."
                ),
                rationale=blocker.note,
                target_entity_type="work_order_blocker",
                target_entity_id=blocker.id,
                suggested_action={
                    "type": "review_blocker",
                    "work_order_id": work_order.id,
                    "blocker_id": blocker.id,
                    "category": blocker.category,
                },
                evidence=[
                    {
                        "type": "operator_signal",
                        "label": blocker.title,
                        "detail": blocker.note,
                    }
                ],
                impact={"expected": "Reduce stuck WIP and improve schedule reliability."},
                confidence_score=0.78,
                created_by=user.id,
            )
        )
