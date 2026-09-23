"""Preview the authorized cancellation of replaced WO 105; --apply commits it.

Run from backend: python -m scripts.cancel_replaced_work_order_105 --actor-id 7
Add --apply only after reviewing the guarded preview. Operations, labor, the
original receipt, and historical audits are preserved.
"""

import argparse
import json
from datetime import datetime

from sqlalchemy.orm import Session

from app.db.ledger_filter import work_order_ledger_filter
from app.models.audit_log import AuditLog
from app.models.inventory import InventoryItem, InventoryTransaction, TransactionType
from app.models.job_costing import JobCost
from app.models.shipping import Shipment
from app.models.time_entry import TimeEntry
from app.models.user import User, UserRole
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation, WorkOrderStatus
from app.models.work_order_material import WorkOrderMaterialAllocation
from app.services.audit_service import AuditService
from app.services.completion_receipt_correction_service import reverse_erroneous_finished_goods_receipt

COMPANY_ID = 1
WORK_ORDER_ID = 105
SOURCE_OPERATION_ID = 842
RECEIPT_ID = 518
STOCK_ID = 257
OPERATION_IDS = set(range(842, 863))
OPERATION_QUANTITIES = [8, 64, 64, 8, 16, 16, 32, 32, 128, 144, 32, 16, 8, 8, 16, 8, 16, 24, 8, 8, 8]
COMPLETION_AT = datetime(2026, 9, 23, 14, 41, 7, 274470)
ACTION = "INCIDENT_REPLACED_WORK_ORDER_CANCEL"
REASON = (
    "User-authorized cancellation of replaced work order WO-20260923-002 (105) "
    "after the shared-sequence completion defect. Reverse its false 8-unit "
    "finished-goods receipt; preserve operation 842 and all original labor and history."
)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(f"Cancellation refused: {message}")


def cancel(db: Session, *, apply: bool = False, actor_id: int | None = None) -> dict:
    """Validate this incident only, joining the caller's atomic transaction."""
    ops_query = db.query(WorkOrderOperation).filter_by(company_id=COMPANY_ID, work_order_id=WORK_ORDER_ID)
    wo_query = db.query(WorkOrder).filter_by(company_id=COMPANY_ID, id=WORK_ORDER_ID)
    if apply:
        ops_query, wo_query = ops_query.with_for_update(), wo_query.with_for_update()
    operations = ops_query.order_by(WorkOrderOperation.id).all()
    work_order = wo_query.one()
    _require(work_order.work_order_number == "WO-20260923-002" and not work_order.is_deleted, "identity changed")
    previous = (
        db.query(AuditLog)
        .filter_by(company_id=COMPANY_ID, action=ACTION, resource_type="work_order", resource_id=WORK_ORDER_ID)
        .first()
    )
    if previous:
        _require(
            work_order.status == WorkOrderStatus.CANCELLED and work_order.quantity_complete == 0,
            "previous cancellation no longer matches the work order",
        )
        return {"work_order_id": WORK_ORDER_ID, "already_cancelled": True, "audit_id": previous.id}
    _require(
        work_order.status == WorkOrderStatus.COMPLETE
        and work_order.quantity_ordered == 8
        and work_order.quantity_complete == 8,
        "status or quantities changed",
    )
    _require(
        {op.id for op in operations} == OPERATION_IDS
        and all(
            op.status == OperationStatus.COMPLETE
            and op.quantity_complete == OPERATION_QUANTITIES[op.id - 842]
            and op.actual_end == COMPLETION_AT
            for op in operations
        ),
        "operation membership or status changed",
    )
    source = next((op for op in operations if op.id == SOURCE_OPERATION_ID), None)
    _require(
        source is not None
        and source.quantity_complete == 8
        and source.actual_end is not None
        and source.completed_by is not None,
        "source operation evidence changed",
    )
    entries = db.query(TimeEntry).filter_by(company_id=COMPANY_ID, work_order_id=WORK_ORDER_ID).all()
    _require(len(entries) == 1, "labor entries changed")
    entry = entries[0]
    _require(
        entry.id == 495
        and entry.operation_id == SOURCE_OPERATION_ID
        and entry.quantity_produced == 8
        and not entry.quantity_scrapped
        and entry.clock_out == datetime(2026, 9, 23, 14, 41, 7, 496684),
        "source labor evidence changed",
    )
    for model, label in [
        (Shipment, "shipments"),
        (WorkOrderMaterialAllocation, "material allocations"),
        (JobCost, "job costing"),
    ]:
        _require(
            not db.query(model).filter_by(company_id=COMPANY_ID, work_order_id=WORK_ORDER_ID).first(),
            f"{label} require a separately reviewed correction",
        )
    ledger = (
        db.query(InventoryTransaction)
        .filter(InventoryTransaction.company_id == COMPANY_ID, work_order_ledger_filter(WORK_ORDER_ID, COMPANY_ID))
        .all()
    )
    _require(
        len(ledger) == 1 and ledger[0].id == RECEIPT_ID and ledger[0].transaction_type == TransactionType.RECEIVE,
        "inventory ledger changed",
    )
    receipt = ledger[0]
    _require(
        receipt.quantity == 8 and receipt.inventory_item_id == STOCK_ID and receipt.part_id == work_order.part_id,
        "receipt identity or quantity changed",
    )
    stock = db.query(InventoryItem).filter_by(company_id=COMPANY_ID, id=STOCK_ID).one()
    _require(
        stock.quantity_on_hand == 8
        and not stock.quantity_allocated
        and stock.part_id == receipt.part_id
        and stock.lot_number == receipt.lot_number
        and stock.location == receipt.to_location,
        "stock changed or was reserved",
    )
    _require(
        not db.query(InventoryTransaction)
        .filter(
            InventoryTransaction.company_id == COMPANY_ID,
            InventoryTransaction.inventory_item_id == STOCK_ID,
            InventoryTransaction.id != RECEIPT_ID,
        )
        .first(),
        "stock has subsequent movements",
    )
    summary = {
        "work_order_id": WORK_ORDER_ID,
        "work_order_status": "cancelled",
        "work_order_quantity_complete": 0,
        "preserved_operation_id": SOURCE_OPERATION_ID,
        "preserved_operation_count": len(operations),
        "finished_goods_receipt_to_reverse": RECEIPT_ID,
        "finished_goods_quantity_to_reverse": 8,
        "applied": apply,
    }
    if not apply:
        return summary
    actor = db.query(User).filter_by(company_id=COMPANY_ID, id=actor_id).one_or_none()
    _require(actor is not None and actor.is_active and actor.role == UserRole.ADMIN, "active requesting admin required")
    audit = AuditService(db, user=actor, company_id=COMPANY_ID)
    correction = reverse_erroneous_finished_goods_receipt(
        db,
        work_order,
        expected_receipt_id=RECEIPT_ID,
        expected_quantity=8,
        reason=REASON,
        user_id=actor.id,
        company_id=COMPANY_ID,
        audit=audit,
    )
    before = {
        "status": work_order.status.value,
        "quantity_complete": work_order.quantity_complete,
        "actual_end": work_order.actual_end.isoformat() if work_order.actual_end else None,
        "current_operation_id": work_order.current_operation_id,
    }
    work_order.status = WorkOrderStatus.CANCELLED
    work_order.quantity_complete = 0
    work_order.actual_end = work_order.current_operation_id = None
    audit.log_required(
        ACTION,
        "work_order",
        resource_id=WORK_ORDER_ID,
        resource_identifier=work_order.work_order_number,
        description=REASON,
        old_values=before,
        new_values={"status": "cancelled", "quantity_complete": 0, "actual_end": None, "current_operation_id": None},
        extra_data={**summary, "inventory_correction_id": correction.id},
    )
    db.flush()
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--actor-id", type=int, help="Active requesting administrator, required with --apply")
    args = parser.parse_args()
    from app.db.database import SessionLocal

    with SessionLocal() as db:
        try:
            result = cancel(db, apply=args.apply, actor_id=args.actor_id)
            db.commit() if args.apply else db.rollback()
            print(json.dumps(result, indent=2))
        except Exception:
            db.rollback()
            raise


if __name__ == "__main__":
    main()
