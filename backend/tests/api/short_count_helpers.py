"""Shared factories for the short-count (owner ruling 1) test files.

``test_operation_short_count*.py`` build multi-operation routings, seed labor evidence and
read back the rows a refusal must leave untouched. Natural keys route through a module
counter so rows stay unique across tests sharing a worker DB under ``-n auto``. Users,
work centers and headers come from ``kiosk_test_helpers`` (RELEASED work orders by default).
"""

from datetime import datetime, timedelta
from typing import Optional, Sequence

from sqlalchemy.orm import Session

from app.models.audit_log import AuditLog
from app.models.inventory import InventoryItem, InventoryTransaction
from app.models.part import Part
from app.models.scrap_reason import ScrapReasonCode
from app.models.time_entry import TimeEntry, TimeEntryType
from app.models.user import User
from app.models.work_center import WorkCenter
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.models.work_order_material import AllocationSource, AllocationStatus, WorkOrderMaterialAllocation
from tests.api.kiosk_test_helpers import COMPANY_A, ensure_company

COMPLETE_URL = "/api/v1/shop-floor/operations/{op}/complete"
CLOCK_OUT_URL = "/api/v1/shop-floor/clock-out/{entry}"
PRODUCTION_URL = "/api/v1/shop-floor/operations/{op}/production"

_seq = {"n": 70000}


def _next() -> int:
    _seq["n"] += 1
    return _seq["n"]


def make_part(db: Session, *, company_id: int = COMPANY_A, part_type: str = "manufactured") -> Part:
    ensure_company(db, company_id)
    n = _next()
    part = Part(
        part_number=f"SC-{n:05d}",
        name=f"Short-count part {n}",
        part_type=part_type,
        unit_of_measure="each",
        standard_cost=5.0,
        is_active=True,
        company_id=company_id,
    )
    db.add(part)
    db.commit()
    db.refresh(part)
    return part


def make_routing(
    db: Session,
    *,
    work_centers: Sequence[WorkCenter],
    quantity_ordered: float = 10,
    sequential: bool = True,
    statuses: Optional[Sequence[OperationStatus]] = None,
    wo_status: WorkOrderStatus = WorkOrderStatus.RELEASED,
    company_id: int = COMPANY_A,
    component_parts: Optional[Sequence[Optional[Part]]] = None,
    component_quantities: Optional[Sequence[Optional[float]]] = None,
    serial_numbers: Optional[str] = None,
) -> tuple[WorkOrder, list[WorkOrderOperation]]:
    """A work order with one operation per work center, sequences 10, 20, 30, ...

    ``statuses`` defaults to the first operation READY and the rest PENDING.
    ``component_parts`` / ``component_quantities`` make component operations.
    """
    part = make_part(db, company_id=company_id)
    n = _next()
    work_order = WorkOrder(
        work_order_number=f"SCWO-{n:05d}",
        part_id=part.id,
        sequential_operations=sequential,
        quantity_ordered=quantity_ordered,
        status=wo_status,
        priority=2,
        serial_numbers=serial_numbers,
        company_id=company_id,
    )
    db.add(work_order)
    db.flush()
    statuses = list(statuses or [OperationStatus.READY] + [OperationStatus.PENDING] * (len(work_centers) - 1))
    operations = []
    for index, work_center in enumerate(work_centers):
        sequence = (index + 1) * 10
        component = component_parts[index] if component_parts else None
        operation = WorkOrderOperation(
            work_order_id=work_order.id,
            work_center_id=work_center.id,
            sequence=sequence,
            operation_number=str(sequence),
            name=f"Step {sequence}",
            status=statuses[index],
            quantity_complete=0.0,
            quantity_scrapped=0.0,
            component_part_id=component.id if component is not None else None,
            component_quantity=(component_quantities[index] if component_quantities else None),
            company_id=company_id,
        )
        db.add(operation)
        operations.append(operation)
    db.commit()
    db.refresh(work_order)
    for operation in operations:
        db.refresh(operation)
    return work_order, operations


def set_op(db: Session, operation: WorkOrderOperation, **fields) -> WorkOrderOperation:
    for name, value in fields.items():
        setattr(operation, name, value)
    db.commit()
    db.refresh(operation)
    return operation


def add_entry(
    db: Session,
    user: User,
    operation: WorkOrderOperation,
    *,
    produced: float = 0.0,
    scrapped: float = 0.0,
    open_entry: bool = False,
    approved: bool = False,
) -> TimeEntry:
    """A labor entry on ``operation`` (closed unless ``open_entry``)."""
    now = datetime.utcnow()
    entry = TimeEntry(
        user_id=user.id,
        work_order_id=operation.work_order_id,
        operation_id=operation.id,
        work_center_id=operation.work_center_id,
        entry_type=TimeEntryType.RUN,
        clock_in=now - timedelta(hours=1),
        clock_out=None if open_entry else now - timedelta(minutes=5),
        duration_hours=None if open_entry else 55 / 60,
        quantity_produced=produced,
        quantity_scrapped=scrapped,
        approved=now if approved else None,
        company_id=user.company_id,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def make_scrap_code(db: Session, *, company_id: int = COMPANY_A, is_active: bool = True) -> ScrapReasonCode:
    n = _next()
    code = ScrapReasonCode(
        code=f"SC{n}",
        name=f"Short-count reason {n}",
        category="other",
        is_active=is_active,
        company_id=company_id,
    )
    db.add(code)
    db.commit()
    db.refresh(code)
    return code


def tie_material(db: Session, work_order: WorkOrder, operation: WorkOrderOperation, *, on_hand: float = 100.0) -> Part:
    """An operation-scoped tie, one sheet per run (good + scrap), with stock to consume."""
    n = _next()
    sheet = Part(
        part_number=f"SC-SHEET-{n}",
        name=f"Short-count sheet {n}",
        part_type="raw_material",
        unit_of_measure="sheets",
        standard_cost=50.0,
        is_active=True,
        company_id=work_order.company_id,
    )
    db.add(sheet)
    db.flush()
    db.add(
        InventoryItem(
            part_id=sheet.id,
            location="RAW-A",
            warehouse="MAIN",
            quantity_on_hand=on_hand,
            quantity_allocated=0.0,
            quantity_available=on_hand,
            lot_number=f"SC-LOT-{n}",
            unit_cost=50.0,
            status="available",
            is_active=True,
            company_id=work_order.company_id,
        )
    )
    db.add(
        WorkOrderMaterialAllocation(
            company_id=work_order.company_id,
            work_order_id=work_order.id,
            work_order_operation_id=operation.id,
            part_id=sheet.id,
            source=AllocationSource.NEST,
            status=AllocationStatus.OPEN,
            qty_per_run=1.0,
            qty_planned=float(work_order.quantity_ordered or 0),
            unit_of_measure="sheets",
            qty_consumed=0.0,
        )
    )
    db.commit()
    return sheet


def op_audit_rows(db: Session, operation_id: int) -> list[AuditLog]:
    return (
        db.query(AuditLog)
        .filter(AuditLog.resource_type == "work_order_operation", AuditLog.resource_id == operation_id)
        .order_by(AuditLog.id)
        .all()
    )


def ledger_rows(db: Session, company_id: int = COMPANY_A) -> list[InventoryTransaction]:
    return db.query(InventoryTransaction).filter(InventoryTransaction.company_id == company_id).all()


def fresh(db: Session, model, row_id: int):
    """Re-read a row from the database (drops the identity map first)."""
    db.expire_all()
    return db.query(model).filter(model.id == row_id).one()
