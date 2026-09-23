"""Guarded repair for the 2026-09-23 operation-identity incident on WO 106.

Run from backend with ``python -m scripts.repair_work_order_106_completion`` to
preview. Only use ``--apply`` after the operation-identity fix is live. This is
deliberately incident-specific: changed evidence aborts instead of guessing.
"""

import argparse
import json

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

COMPANY_ID = 1
WORK_ORDER_ID = 106
SOURCE_OPERATION_ID = 882
RECEIPT_ID = 519
SIBLING_IDS = set(range(863, 884)) - {SOURCE_OPERATION_ID}
REASON = (
    "Correct the 2026-09-23 shared-sequence reconciliation defect on WO-20260923-003: "
    "only operation 882 had production evidence; preserve its 8 completed inlets "
    "and reverse the 20 copied completions and false finished-goods receipt."
)
ACTION = "INCIDENT_COMPLETION_REPAIR"


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(f"Repair refused: {message}")


def _snapshot(row, fields):
    result = {}
    for field in fields:
        value = getattr(row, field)
        if hasattr(value, "value"):
            value = value.value
        elif hasattr(value, "isoformat"):
            value = value.isoformat()
        result[field] = value
    return result


def repair(db: Session, *, apply: bool = False, actor_id: int | None = None) -> dict:
    """Validate the exact evidence, then join the caller's atomic transaction."""
    work_order_query = db.query(WorkOrder).filter(WorkOrder.company_id == COMPANY_ID, WorkOrder.id == WORK_ORDER_ID)
    operation_query = db.query(WorkOrderOperation).filter(
        WorkOrderOperation.company_id == COMPANY_ID,
        WorkOrderOperation.work_order_id == WORK_ORDER_ID,
    )
    if apply:
        work_order_query = work_order_query.with_for_update()
        operation_query = operation_query.with_for_update()
    operations = operation_query.order_by(WorkOrderOperation.id).all()
    work_order = work_order_query.one()
    previous_repair = (
        db.query(AuditLog)
        .filter(
            AuditLog.company_id == COMPANY_ID,
            AuditLog.action == ACTION,
            AuditLog.resource_type == "work_order",
            AuditLog.resource_id == WORK_ORDER_ID,
        )
        .first()
    )
    if previous_repair:
        return {"work_order_id": WORK_ORDER_ID, "already_repaired": True, "audit_id": previous_repair.id}

    _require(work_order.work_order_number == "WO-20260923-003", "work order identity changed")
    _require(work_order.status == WorkOrderStatus.COMPLETE, "work order is no longer complete")
    _require(not work_order.is_deleted and not work_order.sequential_operations, "work order mode changed")
    _require(work_order.quantity_ordered == 8 and work_order.quantity_complete == 8, "work order quantity changed")
    _require({op.id for op in operations} == SIBLING_IDS | {SOURCE_OPERATION_ID}, "operation membership changed")
    source = next(op for op in operations if op.id == SOURCE_OPERATION_ID)
    _require(source.status == OperationStatus.COMPLETE and source.quantity_complete == 8, "inlet evidence changed")
    _require(source.actual_end is not None and source.completed_by is not None, "inlet completion is missing")
    entries = (
        db.query(TimeEntry).filter(TimeEntry.company_id == COMPANY_ID, TimeEntry.work_order_id == WORK_ORDER_ID).all()
    )
    _require(len(entries) == 1, "labor entries changed")
    entry = entries[0]
    _require(
        entry.id == 496
        and entry.operation_id == SOURCE_OPERATION_ID
        and entry.clock_out is not None
        and entry.quantity_produced == 8
        and not entry.quantity_scrapped,
        "recorded inlet production changed",
    )
    _require(
        not db.query(JobCost).filter(JobCost.company_id == COMPANY_ID, JobCost.work_order_id == WORK_ORDER_ID).first(),
        "job costing now requires a separate reviewed correction",
    )
    _require(
        not db.query(Shipment)
        .filter(Shipment.company_id == COMPANY_ID, Shipment.work_order_id == WORK_ORDER_ID)
        .first(),
        "shipment activity now requires a separate reviewed correction",
    )
    _require(
        not db.query(WorkOrderMaterialAllocation)
        .filter(
            WorkOrderMaterialAllocation.company_id == COMPANY_ID,
            WorkOrderMaterialAllocation.work_order_id == WORK_ORDER_ID,
        )
        .first(),
        "material allocations now require a separate reviewed correction",
    )
    ledger = (
        db.query(InventoryTransaction)
        .filter(
            InventoryTransaction.company_id == COMPANY_ID,
            work_order_ledger_filter(WORK_ORDER_ID, COMPANY_ID),
        )
        .all()
    )
    _require(
        len(ledger) == 1 and ledger[0].id == RECEIPT_ID and ledger[0].transaction_type == TransactionType.RECEIVE,
        "work order inventory ledger changed",
    )
    receipt = ledger[0]
    _require(receipt.quantity == 8 and receipt.inventory_item_id == 258, "receipt identity or quantity changed")
    stock = db.query(InventoryItem).filter(InventoryItem.company_id == COMPANY_ID, InventoryItem.id == 258).one()
    _require(stock.quantity_on_hand == 8 and not stock.quantity_allocated, "finished goods changed or were reserved")
    _require(
        db.query(InventoryTransaction)
        .filter(
            InventoryTransaction.company_id == COMPANY_ID,
            InventoryTransaction.inventory_item_id == stock.id,
            InventoryTransaction.id != RECEIPT_ID,
        )
        .first()
        is None,
        "finished goods have subsequent inventory movements",
    )
    siblings = [op for op in operations if op.id in SIBLING_IDS]
    for op in siblings:
        _require(
            op.sequence == 10
            and op.component_part_id is not None
            and op.status == OperationStatus.COMPLETE
            and op.quantity_complete == op.component_quantity
            and op.actual_start == source.actual_start
            and op.actual_end == source.actual_end
            and op.started_by == source.started_by
            and op.completed_by == source.completed_by,
            f"operation {op.id} no longer matches copied completion",
        )
        _require(
            not any(
                (
                    op.quantity_scrapped,
                    op.quantity_reworked,
                    op.actual_setup_hours,
                    op.actual_run_hours,
                    op.last_reported_at,
                    op.scheduled_start,
                    op.scheduled_end,
                )
            ),
            f"operation {op.id} has additional production or schedule evidence",
        )
        audits = (
            db.query(AuditLog)
            .filter(
                AuditLog.company_id == COMPANY_ID,
                AuditLog.resource_type == "work_order_operation",
                AuditLog.resource_id == op.id,
            )
            .all()
        )
        _require(
            len(audits) == 1
            and audits[0].action == "STATUS_CHANGE"
            and audits[0].old_values == {"status": "ready"}
            and audits[0].new_values == {"status": "complete"}
            and (audits[0].extra_data or {}).get("source") == "reconcile_on_read"
            and (audits[0].extra_data or {}).get("time_entry_ids") == [],
            f"operation {op.id} audit history changed",
        )

    summary = {
        "work_order_id": WORK_ORDER_ID,
        "preserved_operation_id": SOURCE_OPERATION_ID,
        "restored_ready_operation_ids": sorted(SIBLING_IDS),
        "finished_goods_receipt_to_reverse": RECEIPT_ID,
        "finished_goods_quantity_to_reverse": 8,
        "work_order_status": "in_progress",
        "work_order_quantity_complete": 0,
        "applied": apply,
    }
    if not apply:
        return summary

    from app.services.completion_receipt_correction_service import reverse_erroneous_finished_goods_receipt

    actor = db.query(User).filter(User.id == actor_id, User.company_id == COMPANY_ID).one_or_none()
    _require(
        actor is not None and actor.is_active and actor.role == UserRole.ADMIN, "an active requesting admin is required"
    )
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
    fields = ("status", "quantity_complete", "actual_start", "actual_end", "started_by", "completed_by")
    for op in siblings:
        before = _snapshot(op, fields)
        op.status = OperationStatus.READY
        op.quantity_complete = 0
        op.actual_start = op.actual_end = op.started_by = op.completed_by = None
        audit.log_required(
            ACTION,
            "work_order_operation",
            resource_id=op.id,
            resource_identifier=op.operation_number,
            description=REASON,
            old_values=before,
            new_values=_snapshot(op, fields),
            extra_data={"source": "incident_repair", "work_order_id": WORK_ORDER_ID},
        )
    work_order_fields = ("status", "quantity_complete", "actual_end", "current_operation_id")
    before = _snapshot(work_order, work_order_fields)
    work_order.status = WorkOrderStatus.IN_PROGRESS
    work_order.quantity_complete = 0
    work_order.actual_end = None
    work_order.current_operation_id = siblings[0].id
    audit.log_required(
        ACTION,
        "work_order",
        resource_id=WORK_ORDER_ID,
        resource_identifier=work_order.work_order_number,
        description=REASON,
        old_values=before,
        new_values=_snapshot(work_order, work_order_fields),
        extra_data={**summary, "inventory_correction_id": correction.id if correction else None},
    )
    db.flush()
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Commit the guarded repair after deploying the fix")
    parser.add_argument("--actor-id", type=int, help="Requesting administrator, required with --apply")
    args = parser.parse_args()
    from app.db.database import SessionLocal

    with SessionLocal() as db:
        try:
            result = repair(db, apply=args.apply, actor_id=args.actor_id)
            if args.apply:
                db.commit()
            else:
                db.rollback()
            print(json.dumps(result, indent=2))
        except Exception:
            db.rollback()
            raise


if __name__ == "__main__":
    main()
