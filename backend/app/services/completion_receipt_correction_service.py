"""Audited compensation for a proven false work-order finished-goods receipt.

The original RECEIVE and its unique idempotency key remain immutable. A dedicated
ADJUST trail records its reversal and eventual restoration when the job really
finishes. Ordinary adjustments and shipments cannot enable another receipt.
Every function joins the caller's transaction; callers own commit/rollback.
"""

from math import isfinite

from sqlalchemy.orm import Session

from app.db.ledger_filter import LEDGER_QUANTITY_EPSILON, RECEIPT_CORRECTION_REFERENCE
from app.models.inventory import InventoryItem, InventoryTransaction, TransactionType
from app.models.work_order import WorkOrder, WorkOrderStatus
from app.services.audit_service import AuditService


def _lock_work_order(db: Session, work_order: WorkOrder, company_id: int) -> tuple:
    if work_order.company_id != company_id:
        raise ValueError("Work order does not belong to the correction company")
    # Persist the caller's completion inside this transaction before reading the
    # authoritative values under the lock; never use a stale identity-map quantity.
    db.flush()
    row = (
        db.query(WorkOrder.status, WorkOrder.quantity_complete)
        .filter(WorkOrder.id == work_order.id, WorkOrder.company_id == company_id)
        .with_for_update()
        .first()
    )
    if row is None:
        raise ValueError("Work order not found")
    return row


def _validate_context(user_id: int, company_id: int, audit: AuditService) -> None:
    if user_id is None or audit.company_id != company_id:
        raise ValueError("Receipt correction requires an identified actor and company-scoped audit")


def _receipt(db: Session, work_order: WorkOrder, company_id: int) -> InventoryTransaction:
    receipt = (
        db.query(InventoryTransaction)
        .filter(
            InventoryTransaction.company_id == company_id,
            InventoryTransaction.reference_type == "work_order",
            InventoryTransaction.reference_id == work_order.id,
            InventoryTransaction.transaction_type == TransactionType.RECEIVE,
        )
        .one_or_none()
    )
    if (
        receipt is None
        or receipt.part_id != work_order.part_id
        or not receipt.inventory_item_id
        or not isfinite(float(receipt.quantity))
        or float(receipt.quantity) <= 0
    ):
        raise ValueError("Expected finished-goods receipt not found")
    return receipt


def _corrections(db: Session, work_order: WorkOrder, company_id: int) -> list[InventoryTransaction]:
    return (
        db.query(InventoryTransaction)
        .filter(
            InventoryTransaction.company_id == company_id,
            InventoryTransaction.reference_type == RECEIPT_CORRECTION_REFERENCE,
            InventoryTransaction.reference_id == work_order.id,
        )
        .order_by(InventoryTransaction.id)
        .all()
    )


def _lock_stock(db: Session, receipt: InventoryTransaction, company_id: int) -> InventoryItem:
    stock = (
        db.query(InventoryItem)
        .filter(
            InventoryItem.id == receipt.inventory_item_id,
            InventoryItem.company_id == company_id,
        )
        .populate_existing()
        .with_for_update()
        .first()
    )
    if (
        stock is None
        or stock.part_id != receipt.part_id
        or stock.lot_number != receipt.lot_number
        or stock.location != receipt.to_location
        or not isfinite(float(stock.quantity_on_hand or 0))
        or not isfinite(float(stock.quantity_allocated or 0))
    ):
        raise ValueError("The original finished-goods stock row has changed")
    return stock


def _post_adjustment(db, work_order, receipt, stock, quantity, reason, user_id, company_id, audit):
    # Reconcile callers may catch inventory failures. Keep stock, correction, and
    # both mandatory audit rows indivisible even if a caller catches our exception.
    with db.begin_nested():
        return _post_audited_adjustment(db, work_order, receipt, stock, quantity, reason, user_id, company_id, audit)


def _post_audited_adjustment(db, work_order, receipt, stock, quantity, reason, user_id, company_id, audit):
    old_qty = float(stock.quantity_on_hand or 0)
    txn = InventoryTransaction(
        company_id=company_id,
        inventory_item_id=stock.id,
        part_id=receipt.part_id,
        transaction_type=TransactionType.ADJUST,
        quantity=quantity,
        from_location=stock.location,
        to_location=stock.location,
        lot_number=receipt.lot_number,
        reference_type=RECEIPT_CORRECTION_REFERENCE,
        reference_id=work_order.id,
        reference_number=work_order.work_order_number,
        reason_code="COMPLETION_CORRECTION",
        notes=f"Receipt {receipt.id}: {reason}",
        unit_cost=receipt.unit_cost,
        total_cost=abs(quantity) * float(receipt.unit_cost or 0),
        created_by=user_id,
    )
    db.add(txn)
    stock.quantity_on_hand = old_qty + quantity
    stock.quantity_available = stock.quantity_on_hand - float(stock.quantity_allocated or 0)
    db.flush()
    extra = {"source": RECEIPT_CORRECTION_REFERENCE, "original_receipt_id": receipt.id, "reason": reason}
    audit.log_required(
        "CREATE",
        "inventory",
        resource_id=txn.id,
        resource_identifier=str(txn.id),
        new_values={
            "quantity": quantity,
            "inventory_item_id": stock.id,
            "part_id": receipt.part_id,
            "transaction_type": TransactionType.ADJUST.value,
            "reference_type": RECEIPT_CORRECTION_REFERENCE,
            "reference_id": work_order.id,
            "lot_number": receipt.lot_number,
            "created_by": user_id,
        },
        description=f"Finished-goods receipt correction for {work_order.work_order_number}: {reason}",
        extra_data=extra,
    )
    audit.log_required(
        "UPDATE",
        "inventory",
        resource_id=stock.id,
        resource_identifier=f"{receipt.part_id} @ {stock.location}",
        old_values={"quantity_on_hand": old_qty},
        new_values={"quantity_on_hand": stock.quantity_on_hand},
        description=f"Receipt {receipt.id} correction: {reason}",
        extra_data=extra,
    )
    return txn


def reverse_erroneous_finished_goods_receipt(
    db: Session,
    work_order: WorkOrder,
    *,
    expected_receipt_id: int,
    expected_quantity: float,
    reason: str,
    user_id: int,
    company_id: int,
    audit: AuditService,
) -> InventoryTransaction:
    """Reverse one verified untouched receipt; repeated identical calls are no-ops.

    This is an incident-repair primitive, not a general reopen endpoint. Refuses
    reserved stock or any other stock movement, so it cannot undo material already
    used/shipped. The caller must prove and repair the erroneous completion in the
    SAME transaction before committing this compensating movement.
    """
    if not reason.strip() or not isfinite(expected_quantity) or expected_quantity <= 0:
        raise ValueError("A reason and positive finite expected receipt quantity are required")
    _validate_context(user_id, company_id, audit)
    _lock_work_order(db, work_order, company_id)
    receipt = _receipt(db, work_order, company_id)
    if receipt.id != expected_receipt_id or abs(float(receipt.quantity) - expected_quantity) > LEDGER_QUANTITY_EPSILON:
        raise ValueError("The finished-goods receipt no longer matches the verified incident")
    corrections = _corrections(db, work_order, company_id)
    if corrections:
        if (
            len(corrections) == 1
            and corrections[0].transaction_type == TransactionType.ADJUST
            and corrections[0].inventory_item_id == receipt.inventory_item_id
            and corrections[0].part_id == receipt.part_id
            and corrections[0].lot_number == receipt.lot_number
            and abs(float(corrections[0].quantity) + expected_quantity) <= LEDGER_QUANTITY_EPSILON
        ):
            return corrections[0]
        raise ValueError("This receipt already has a different correction or has been restored")
    stock = _lock_stock(db, receipt, company_id)
    other_movement = (
        db.query(InventoryTransaction.id)
        .filter(
            InventoryTransaction.company_id == company_id,
            InventoryTransaction.inventory_item_id == stock.id,
            InventoryTransaction.id != receipt.id,
        )
        .first()
    )
    if (
        other_movement
        or not isfinite(float(stock.quantity_on_hand or 0))
        or not isfinite(float(stock.quantity_allocated or 0))
        or abs(float(stock.quantity_on_hand or 0) - expected_quantity) > LEDGER_QUANTITY_EPSILON
        or abs(float(stock.quantity_allocated or 0)) > LEDGER_QUANTITY_EPSILON
    ):
        raise ValueError("Finished goods have been moved, changed, or allocated since receipt")
    return _post_adjustment(
        db,
        work_order,
        receipt,
        stock,
        -expected_quantity,
        reason.strip(),
        user_id,
        company_id,
        audit,
    )


def restore_corrected_finished_goods_receipt(
    db: Session,
    work_order: WorkOrder,
    *,
    user_id: int,
    company_id: int,
    audit: AuditService,
) -> InventoryTransaction | None:
    """On true completion restore only the quantity missing from an explicit reversal.

    Serialize on the WO before reading the correction ledger, and on its stock row
    before adding the delta. SHIP/ordinary ADJUST rows are deliberately excluded:
    moving stock never creates permission to receive a completed job twice.
    """
    if not _corrections(db, work_order, company_id):
        return None
    _validate_context(user_id, company_id, audit)
    status, quantity_complete = _lock_work_order(db, work_order, company_id)
    if status != WorkOrderStatus.COMPLETE:
        return None
    receipt = _receipt(db, work_order, company_id)
    corrections = _corrections(db, work_order, company_id)
    if any(
        row.transaction_type != TransactionType.ADJUST
        or row.inventory_item_id != receipt.inventory_item_id
        or row.part_id != receipt.part_id
        or row.lot_number != receipt.lot_number
        or not isfinite(float(row.quantity))
        for row in corrections
    ):
        raise ValueError("Finished-goods correction ledger does not match its original receipt")
    if abs(float(corrections[0].quantity) + float(receipt.quantity)) > LEDGER_QUANTITY_EPSILON or any(
        float(row.quantity) < 0 for row in corrections[1:]
    ):
        raise ValueError("Finished-goods correction ledger is not one complete reversal followed by restorations")
    net_received = float(receipt.quantity) + sum(float(row.quantity) for row in corrections)
    target = float(quantity_complete or 0)
    if not isfinite(target) or target < 0 or not isfinite(net_received) or net_received < -LEDGER_QUANTITY_EPSILON:
        raise ValueError("Invalid corrected finished-goods receipt quantity")
    delta = target - net_received
    if delta <= LEDGER_QUANTITY_EPSILON:
        return None
    stock = _lock_stock(db, receipt, company_id)
    return _post_adjustment(
        db,
        work_order,
        receipt,
        stock,
        delta,
        "Restored receipt after corrected work order reached completion",
        user_id,
        company_id,
        audit,
    )
