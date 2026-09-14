"""Tenant-scoped FAI authoring and immutable final inspection evidence."""

from contextlib import contextmanager
from datetime import date, datetime

from fastapi import HTTPException
from sqlalchemy import inspect
from sqlalchemy.orm import Session, selectinload

from app.db.database import atomic_transaction
from app.db.locks import acquire_generator_lock
from app.db.tenant_filter import tenant_query
from app.models.part import Part
from app.models.quality import FAICharacteristic, FAIStatus, FirstArticleInspection
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder
from app.schemas.quality import FAICharacteristicCreate, FAICharacteristicUpdate, FAICreate, FAIUpdate
from app.services.audit_service import AuditService, AuditWriteError
from app.services.operational_event_service import OperationalEventService

WRITE_ROLES = [UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR, UserRole.QUALITY]
APPROVE_ROLES = [UserRole.ADMIN, UserRole.MANAGER, UserRole.QUALITY]
FINAL_STATUSES = {FAIStatus.PASSED, FAIStatus.FAILED, FAIStatus.CONDITIONAL}


@contextmanager
def fai_transaction(db: Session):
    """Required evidence failure rolls back and reports a retryable service error."""
    try:
        with atomic_transaction(db):
            yield
    except AuditWriteError as exc:
        raise HTTPException(status_code=503, detail="Unable to save audit record") from exc


def fai_query(db: Session, company_id: int):
    # Scope relationship loads too: legacy malformed foreign keys must not expose
    # another tenant's part or characteristic through an otherwise-owned parent.
    # Historical part labels deliberately remain readable after part soft deletion.
    return (
        tenant_query(db, FirstArticleInspection, company_id)
        .options(
            selectinload(FirstArticleInspection.part.and_(Part.company_id == company_id)),
            selectinload(FirstArticleInspection.characteristics.and_(FAICharacteristic.company_id == company_id)),
        )
        .populate_existing()
    )


def get_fai(db: Session, company_id: int, fai_id: int) -> FirstArticleInspection:
    fai = fai_query(db, company_id).filter(FirstArticleInspection.id == fai_id).first()
    if fai is None:
        raise HTTPException(status_code=404, detail="FAI not found")
    return fai


def editable_fai(db: Session, company_id: int, fai_id: int) -> FirstArticleInspection:
    # Every writer, including prefill and final disposition, locks this same row
    # before checking its state. Finalization cannot race an evidence edit.
    fai = (
        tenant_query(db, FirstArticleInspection, company_id)
        .filter(FirstArticleInspection.id == fai_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if fai is None:
        raise HTTPException(status_code=404, detail="FAI not found")
    if fai.status in FINAL_STATUSES or fai.completed_date is not None:
        raise HTTPException(status_code=409, detail="Completed FAI evidence cannot be changed; create a new inspection")
    return fai


def _characteristic(db: Session, company_id: int, fai_id: int, char_id: int) -> FAICharacteristic:
    char = (
        tenant_query(db, FAICharacteristic, company_id)
        .filter(FAICharacteristic.fai_id == fai_id, FAICharacteristic.id == char_id)
        .populate_existing()
        .first()
    )
    if char is None:
        raise HTTPException(status_code=404, detail="Characteristic not found")
    return char


def _snapshot(row):
    values = {}
    for column in inspect(type(row)).columns:
        value = getattr(row, column.key)
        if isinstance(value, (date, datetime)):
            value = value.isoformat()
        elif hasattr(value, "value"):
            value = value.value
        values[column.key] = value
    return values


def _audit(audit, action, fai, *, char=None, old=None, extra=None):
    record = char if char is not None else fai
    audit.log_required(
        action,
        "fai_characteristic" if char is not None else "fai",
        resource_id=record.id,
        resource_identifier=f"{fai.fai_number} / {char.char_number}" if char is not None else fai.fai_number,
        company_id=fai.company_id,
        old_values=old,
        new_values=None if action == "DELETE" else _snapshot(record),
        extra_data={"fai_id": fai.id, **(extra or {})},
    )


def _counts(fai):
    return {
        field: getattr(fai, field)
        for field in ("total_characteristics", "characteristics_passed", "characteristics_failed")
    }


def _number(db, company_id):
    acquire_generator_lock(db, "fai_number", company_id)
    prefix = f"FAI-{datetime.now():%Y%m%d}-"
    last = (
        tenant_query(db, FirstArticleInspection, company_id)
        .filter(FirstArticleInspection.fai_number.like(f"{prefix}%"))
        .order_by(FirstArticleInspection.fai_number.desc())
        .first()
    )
    number = int(last.fai_number.rsplit("-", 1)[-1]) + 1 if last else 1
    return f"{prefix}{number:03d}"


def create_fai(db: Session, company_id: int, data: FAICreate, user: User, audit: AuditService):
    with fai_transaction(db):
        part = tenant_query(db, Part, company_id).filter(Part.id == data.part_id, Part.is_deleted.is_(False)).first()
        if part is None:
            raise HTTPException(status_code=404, detail="Part not found")
        if data.work_order_id is not None:
            work_order = (
                tenant_query(db, WorkOrder, company_id)
                .filter(WorkOrder.id == data.work_order_id, WorkOrder.is_deleted.is_(False))
                .first()
            )
            if work_order is None:
                raise HTTPException(status_code=404, detail="Work order not found")
        fai = FirstArticleInspection(company_id=company_id, fai_number=_number(db, company_id), **data.model_dump())
        db.add(fai)
        db.flush()
        _audit(audit, "CREATE", fai)
        OperationalEventService(db).emit_best_effort(
            company_id=company_id,
            event_type="fai_created",
            source_module="quality",
            entity_type="fai",
            entity_id=fai.id,
            work_order_id=fai.work_order_id,
            user_id=user.id,
            severity="medium",
            event_payload={
                "fai_number": fai.fai_number,
                "part_id": fai.part_id,
                "status": fai.status.value,
                "fai_type": fai.fai_type,
                "reason": fai.reason,
                "due_date": fai.due_date.isoformat() if fai.due_date else None,
            },
        )
    return get_fai(db, company_id, fai.id)


def update_fai(db: Session, company_id: int, fai_id: int, data: FAIUpdate, user: User, audit: AuditService):
    with fai_transaction(db):
        fai = editable_fai(db, company_id, fai_id)
        changes = data.model_dump(exclude_unset=True, exclude={"version"})
        finalizing = changes.get("status") in FINAL_STATUSES
        if finalizing and not (user.is_superuser or user.role == UserRole.PLATFORM_ADMIN or user.role in APPROVE_ROLES):
            raise HTTPException(status_code=403, detail="FAI approval requires Admin, Manager or Quality")
        if "status" in changes and changes["status"] is None:
            raise HTTPException(status_code=422, detail="FAI status cannot be null")
        if changes.get("inspector_id") is not None:
            inspector = tenant_query(db, User, company_id).filter(User.id == changes["inspector_id"]).first()
            if inspector is None:
                raise HTTPException(status_code=404, detail="Inspector not found")
        old = _snapshot(fai)
        for field, value in changes.items():
            setattr(fai, field, value)
        if finalizing:
            fai.completed_date = date.today()
            fai.approved_by = user.id
        if _snapshot(fai) != old:
            action = "STATUS_CHANGE" if old["status"] != fai.status.value else "UPDATE"
            _audit(audit, action, fai, old=old)
            OperationalEventService(db).emit_best_effort(
                company_id=company_id,
                event_type="fai_updated",
                source_module="quality",
                entity_type="fai",
                entity_id=fai.id,
                work_order_id=fai.work_order_id,
                user_id=user.id,
                severity="high" if fai.status == FAIStatus.FAILED else "info",
                event_payload={
                    "fai_number": fai.fai_number,
                    "changed_fields": list(changes),
                    "previous_status": old["status"],
                    "status": fai.status.value,
                    "characteristics_passed": fai.characteristics_passed,
                    "characteristics_failed": fai.characteristics_failed,
                },
            )
    return get_fai(db, company_id, fai.id)


def add_characteristic(db: Session, company_id: int, fai_id: int, data: FAICharacteristicCreate, audit: AuditService):
    with fai_transaction(db):
        fai = editable_fai(db, company_id, fai_id)
        old_counts = _counts(fai)
        char = FAICharacteristic(company_id=company_id, fai_id=fai.id, **data.model_dump())
        db.add(char)
        fai.total_characteristics += 1
        db.flush()
        _audit(audit, "CREATE", fai, char=char, extra={"old_counts": old_counts, "new_counts": _counts(fai)})
    db.refresh(char)
    return char


def update_characteristic(
    db: Session,
    company_id: int,
    fai_id: int,
    char_id: int,
    data: FAICharacteristicUpdate,
    user: User,
    audit: AuditService,
):
    with fai_transaction(db):
        fai = editable_fai(db, company_id, fai_id)
        char = _characteristic(db, company_id, fai_id, char_id)
        old = _snapshot(char)
        old_counts = _counts(fai)
        changes = data.model_dump(exclude_unset=True)
        for field, value in changes.items():
            setattr(char, field, value)
        # Null means unrecorded, not failed. Treat all three dispositions explicitly.
        fai.characteristics_passed += int(char.is_conforming is True) - int(old["is_conforming"] is True)
        fai.characteristics_failed += int(char.is_conforming is False) - int(old["is_conforming"] is False)
        if _snapshot(char) != old:
            _audit(
                audit, "UPDATE", fai, char=char, old=old, extra={"old_counts": old_counts, "new_counts": _counts(fai)}
            )
            if "is_conforming" in changes:
                OperationalEventService(db).emit_best_effort(
                    company_id=company_id,
                    event_type="fai_characteristic_recorded",
                    source_module="quality",
                    entity_type="fai_characteristic",
                    entity_id=char.id,
                    work_order_id=fai.work_order_id,
                    user_id=user.id,
                    severity="high" if char.is_conforming is False else "info",
                    event_payload={
                        "fai_id": fai.id,
                        "fai_number": fai.fai_number,
                        "char_number": char.char_number,
                        "is_conforming": char.is_conforming,
                        "characteristics_passed": fai.characteristics_passed,
                        "characteristics_failed": fai.characteristics_failed,
                    },
                )
    db.refresh(char)
    return char


def delete_characteristic(db: Session, company_id: int, fai_id: int, char_id: int, audit: AuditService):
    with fai_transaction(db):
        fai = editable_fai(db, company_id, fai_id)
        char = _characteristic(db, company_id, fai_id, char_id)
        old_counts = _counts(fai)
        fai.total_characteristics -= 1
        fai.characteristics_passed -= int(char.is_conforming is True)
        fai.characteristics_failed -= int(char.is_conforming is False)
        _audit(
            audit,
            "DELETE",
            fai,
            char=char,
            old=_snapshot(char),
            extra={"old_counts": old_counts, "new_counts": _counts(fai), "soft_delete": False},
        )
        # This model has no SoftDeleteMixin. Unfinished characteristic deletion is
        # allowed and preserves its full prior contents in the required audit row.
        db.delete(char)
    return {"message": "Characteristic deleted"}
