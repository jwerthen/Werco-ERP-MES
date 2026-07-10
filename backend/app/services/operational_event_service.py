from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.models.operational_event import OperationalEvent
from app.models.work_order import WorkOrder, WorkOrderOperation

logger = logging.getLogger(__name__)

SENSITIVE_EVENT_KEYS = {
    "api_key",
    "authorization",
    "bearer",
    "cookie",
    "cui",
    "document_text",
    "drawing_text",
    "encrypted_api_key",
    "file_path",
    "password",
    "raw_text",
    "secret",
    "ssn",
    "token",
}


def redact_event_payload(value: Any, *, key_hint: str = "") -> Any:
    normalized = key_hint.lower().replace("-", "_")
    if normalized and any(part in normalized for part in SENSITIVE_EVENT_KEYS):
        return "[redacted]"
    if isinstance(value, dict):
        return {str(key): redact_event_payload(item, key_hint=str(key)) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_event_payload(item) for item in value[:50]]
    if isinstance(value, str) and len(value) > 1000:
        return f"{value[:1000]}...[truncated]"
    return value


class OperationalEventService:
    """Append-only operational signal store for AI context and real-time workflows."""

    def __init__(self, db: Session):
        self.db = db

    def emit(
        self,
        *,
        company_id: int,
        event_type: str,
        source_module: str,
        entity_type: Optional[str] = None,
        entity_id: Optional[int] = None,
        work_order_id: Optional[int] = None,
        operation_id: Optional[int] = None,
        user_id: Optional[int] = None,
        severity: str = "info",
        event_payload: Optional[Dict[str, Any]] = None,
        occurred_at: Optional[datetime] = None,
    ) -> OperationalEvent:
        if work_order_id is not None:
            exists = (
                self.db.query(WorkOrder.id)
                .filter(WorkOrder.id == work_order_id, WorkOrder.company_id == company_id)
                .first()
            )
            if not exists:
                raise ValueError("Work order not found for operational event")
        if operation_id is not None:
            exists = (
                self.db.query(WorkOrderOperation.id)
                .filter(WorkOrderOperation.id == operation_id, WorkOrderOperation.company_id == company_id)
                .first()
            )
            if not exists:
                raise ValueError("Operation not found for operational event")
        event = OperationalEvent(
            company_id=company_id,
            event_type=event_type,
            source_module=source_module,
            entity_type=entity_type,
            entity_id=entity_id,
            work_order_id=work_order_id,
            operation_id=operation_id,
            user_id=user_id,
            severity=severity,
            event_payload=redact_event_payload(event_payload or {}),
            occurred_at=occurred_at or datetime.utcnow(),
        )
        self.db.add(event)
        self.db.flush()
        return event

    def emit_best_effort(
        self,
        *,
        company_id: int,
        event_type: str,
        source_module: str,
        entity_type: Optional[str] = None,
        entity_id: Optional[int] = None,
        work_order_id: Optional[int] = None,
        operation_id: Optional[int] = None,
        user_id: Optional[int] = None,
        severity: str = "info",
        event_payload: Optional[Dict[str, Any]] = None,
        occurred_at: Optional[datetime] = None,
    ) -> Optional[OperationalEvent]:
        """``emit``, but a failure can never propagate to the caller.

        Operational events are telemetry, not audit data: when an event is emitted
        as a side signal of a business operation (WO release, ship, clock-in, ...),
        an event-store failure must not fail that operation. This mirrors the guard
        semantics of the ``emit_*_event`` helpers in
        ``services/completion_signal_service.py``: catch everything (tenant
        validation, event construction, the flush), log with context, and continue.
        Like ``emit`` it only flushes -- never commits -- so the caller's unit of
        work is unaffected and still commits.

        Use plain ``emit`` only where recording the event IS the operation (e.g.
        ``POST /operational-events``) and a failure should surface to the caller.
        """
        try:
            return self.emit(
                company_id=company_id,
                event_type=event_type,
                source_module=source_module,
                entity_type=entity_type,
                entity_id=entity_id,
                work_order_id=work_order_id,
                operation_id=operation_id,
                user_id=user_id,
                severity=severity,
                event_payload=event_payload,
                occurred_at=occurred_at,
            )
        except Exception:  # noqa: BLE001 - telemetry must never fail the business operation
            logger.warning(
                "operational event emit failed (best-effort, continuing): "
                "event_type=%s source_module=%s work_order_id=%s entity=%s/%s company_id=%s",
                event_type,
                source_module,
                work_order_id,
                entity_type,
                entity_id,
                company_id,
                exc_info=True,
            )
            return None

    def list_events(
        self,
        *,
        company_id: int,
        source_module: Optional[str] = None,
        event_type: Optional[str] = None,
        work_order_id: Optional[int] = None,
        limit: int = 100,
    ) -> List[OperationalEvent]:
        query = self.db.query(OperationalEvent).filter(OperationalEvent.company_id == company_id)
        if source_module:
            query = query.filter(OperationalEvent.source_module == source_module)
        if event_type:
            query = query.filter(OperationalEvent.event_type == event_type)
        if work_order_id is not None:
            query = query.filter(OperationalEvent.work_order_id == work_order_id)
        return query.order_by(OperationalEvent.occurred_at.desc()).limit(limit).all()
