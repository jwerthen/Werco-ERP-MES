from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

from app.api.deps import get_current_company_id, get_current_user
from app.db.database import get_db

# The two ledger reference shapes a work order's movement lands under, and the shared
# predicate that spans them. IMPORTED, not re-declared with string literals: genealogy
# that read one shape and cost/analytics that read the other is exactly the drift these
# constants exist to prevent, and an as-built record missing the material a job actually
# burned is an AS9100D hole. Historical rows are NOT migrated; they truthfully carry
# ``work_order`` only. On the ``work_order_operation`` rows ``reference_id`` is the
# OPERATION id, resolved back to its work order below.
from app.db.ledger_filter import (
    LEDGER_QUANTITY_EPSILON,
    OPERATION_REFERENCE_TYPE,
    WORK_ORDER_REFERENCE_TYPES,
    work_order_ledger_filter,
)
from app.models.inventory import InventoryItem, InventoryTransaction, TransactionType
from app.models.part import Part
from app.models.purchasing import POReceipt, PurchaseOrder, PurchaseOrderLine
from app.models.quality import NonConformanceReport
from app.models.user import User
from app.models.work_order import WorkOrder, WorkOrderOperation

# The shared ledger-quantity threshold, imported from ``app.db.ledger_filter`` alongside
# the predicate above rather than re-declared, so "this component lot is fully returned"
# means here exactly what "nothing is consumed" means to the consumption engine and to
# the tie endpoint's untie guard. It comes from ``db`` and not from the engine on
# purpose: this is a read-only genealogy endpoint and has no business importing
# ``material_consumption_service`` (and transitively ``completion_inventory_service`` and
# ``operational_event_service``) to learn one float.

router = APIRouter()


class LotHistoryItem(BaseModel):
    timestamp: datetime
    event_type: str
    description: str
    quantity: Optional[float] = None
    location: Optional[str] = None
    reference: Optional[str] = None
    user: Optional[str] = None
    details: Optional[dict] = None


class ConsumedComponent(BaseModel):
    """One component lot consumed to BUILD a work-order-produced (finished-good) lot.

    The as-built genealogy second hop: tracing a finished-good lot enumerates the
    components that went into it (component part + the consumed source lot + quantity),
    grouped per producing work order, reconstructed from that WO's component ISSUE txns.
    """

    work_order_id: int
    work_order_number: Optional[str] = None
    component_part_id: Optional[int] = None
    component_part_number: Optional[str] = None
    component_part_name: Optional[str] = None
    lot_number: Optional[str] = None
    quantity: float = 0


class LotTraceResponse(BaseModel):
    lot_number: str
    part_id: Optional[int] = None
    part_number: Optional[str] = None
    part_name: Optional[str] = None

    # Source info
    supplier_name: Optional[str] = None
    po_number: Optional[str] = None
    received_date: Optional[datetime] = None
    cert_number: Optional[str] = None
    heat_lot: Optional[str] = None

    # Current status
    current_quantity: float = 0
    current_location: Optional[str] = None
    status: str = "unknown"

    # Usage
    work_orders_used: List[str] = []
    shipments: List[str] = []
    ncrs: List[str] = []

    # As-built genealogy: components consumed to produce THIS lot (when it is a
    # work-order-produced finished-good lot). Empty for purchased/raw lots.
    consumed_components: List[ConsumedComponent] = []

    # Full history
    history: List[LotHistoryItem] = []


@router.get("/lot/{lot_number}", response_model=LotTraceResponse)
def trace_lot(
    lot_number: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """
    Get full traceability for a lot number.

    Shows source, usage, and current status for AS9100D compliance. For a
    finished-good lot produced by a work order, the response also reconstructs the
    as-built genealogy in ``consumed_components`` (the component part / lot / quantity
    consumed to build this lot, from the producing work order's backflush ISSUE
    transactions). All data is scoped to the active company.
    """
    history: List[LotHistoryItem] = []
    work_orders_used = set()
    shipments_list = set()
    ncrs_list = set()
    # Producing work-order ids for the as-built second hop: a RECEIVE txn whose
    # reference is a work order means THIS lot is that WO's finished-good lot, so the
    # WO's component ISSUE txns carry the consumed-component genealogy.
    producing_work_order_ids: set[int] = set()

    # Find inventory items with this lot
    inv_items = (
        db.query(InventoryItem)
        .options(joinedload(InventoryItem.part))
        .filter(InventoryItem.lot_number == lot_number, InventoryItem.company_id == company_id)
        .all()
    )

    part = None
    current_qty = 0
    current_loc = None
    status = "unknown"
    cert_number = None
    heat_lot = None
    received_date = None
    po_number = None
    supplier_name = None

    if inv_items:
        item = inv_items[0]
        part = item.part
        current_qty = sum(i.quantity_on_hand for i in inv_items)
        current_loc = inv_items[0].location
        status = inv_items[0].status
        cert_number = item.cert_number
        heat_lot = item.heat_lot
        received_date = item.received_date
        po_number = item.po_number

    # Find transactions with this lot
    transactions = (
        db.query(InventoryTransaction)
        .options(joinedload(InventoryTransaction.user), joinedload(InventoryTransaction.part))
        .filter(InventoryTransaction.lot_number == lot_number, InventoryTransaction.company_id == company_id)
        .order_by(InventoryTransaction.created_at)
        .all()
    )

    for txn in transactions:
        if not part and txn.part:
            part = txn.part

        event_type = txn.transaction_type.value
        desc = f"{event_type.upper()}: {abs(txn.quantity)} units"

        if txn.to_location:
            desc += f" to {txn.to_location}"
        if txn.from_location:
            desc += f" from {txn.from_location}"

        # ``reference_number`` is the WO number on BOTH reference types (the
        # operation-scoped consumption rows carry it too), so one membership test
        # covers material consumed per-run as well as WO-level movement.
        if txn.reference_type in WORK_ORDER_REFERENCE_TYPES and txn.reference_number:
            work_orders_used.add(txn.reference_number)
        # A RECEIVE referencing a work order: this lot was PRODUCED by that WO -> its
        # component ISSUE txns hold the as-built genealogy (the second hop below).
        if (
            txn.reference_type == "work_order"
            and txn.reference_id is not None
            and txn.transaction_type == TransactionType.RECEIVE
        ):
            producing_work_order_ids.add(txn.reference_id)

        history.append(
            LotHistoryItem(
                timestamp=txn.created_at,
                event_type=event_type,
                description=desc,
                quantity=txn.quantity,
                location=txn.to_location or txn.from_location,
                reference=txn.reference_number,
                user=txn.user.full_name if txn.user else None,
                details={"reference_type": txn.reference_type, "reason": txn.reason_code, "notes": txn.notes},
            )
        )

    # Check PO receipts (exclude voided -- a voided receipt must not show in the active trace)
    receipts = (
        db.query(POReceipt)
        .options(
            joinedload(POReceipt.po_line).joinedload(PurchaseOrderLine.purchase_order).joinedload(PurchaseOrder.vendor)
        )
        .filter(
            POReceipt.lot_number == lot_number,
            POReceipt.company_id == company_id,
            POReceipt.is_deleted == False,  # noqa: E712
        )
        .all()
    )

    for r in receipts:
        if r.po_line and r.po_line.purchase_order:
            po = r.po_line.purchase_order
            po_number = po.po_number
            if po.vendor:
                supplier_name = po.vendor.name

        history.append(
            LotHistoryItem(
                timestamp=r.received_at,
                event_type="received",
                description=f"Received {r.quantity_received} units - Receipt #{r.receipt_number}",
                quantity=r.quantity_received,
                location=None,
                reference=r.receipt_number,
                details={
                    "cert_number": r.cert_number,
                    "packing_slip": r.packing_slip_number,
                    "inspection_status": r.status.value if r.status else None,
                },
            )
        )

    # Check NCRs with this lot (exclude voided -- a voided NCR must not show in the active trace)
    ncrs = (
        db.query(NonConformanceReport)
        .filter(
            NonConformanceReport.lot_number == lot_number,
            NonConformanceReport.company_id == company_id,
            NonConformanceReport.is_deleted == False,  # noqa: E712
        )
        .all()
    )

    for ncr in ncrs:
        ncrs_list.add(ncr.ncr_number)
        history.append(
            LotHistoryItem(
                timestamp=ncr.created_at,
                event_type="ncr",
                description=f"NCR #{ncr.ncr_number} - {ncr.disposition or 'Open'}",
                reference=ncr.ncr_number,
                details={"description": ncr.description, "disposition": ncr.disposition},
            )
        )

    # Check shipments (if lot tracking added to shipments)
    # This would require joining through work orders

    # As-built genealogy second hop (INV-3 / TRACE-2 / TRACE-5): the lot-keyed scan
    # above surfaces only the PRODUCING work order for a finished-good lot -- the
    # consumed component lots ride the WO's *component* ISSUE txns, not the FG lot. So,
    # for each producing WO, enumerate its component ISSUE txns (the backflush
    # consumption) and report the consumed component part / lot / quantity. Every query
    # is tenant-scoped (company_id) per invariant #1.
    consumed_components = _reconstruct_consumed_components(db, producing_work_order_ids, company_id)

    # Sort history by timestamp
    history.sort(key=lambda x: x.timestamp)

    return LotTraceResponse(
        lot_number=lot_number,
        part_id=part.id if part else None,
        part_number=part.part_number if part else None,
        part_name=part.name if part else None,
        supplier_name=supplier_name,
        po_number=po_number,
        received_date=received_date,
        cert_number=cert_number,
        heat_lot=heat_lot,
        current_quantity=current_qty,
        current_location=current_loc,
        status=status,
        work_orders_used=list(work_orders_used),
        shipments=list(shipments_list),
        ncrs=list(ncrs_list),
        consumed_components=consumed_components,
        history=history,
    )


def _reconstruct_consumed_components(
    db: Session,
    producing_work_order_ids: set[int],
    company_id: int,
) -> List[ConsumedComponent]:
    """As-built second hop: consumed component lots for each producing work order.

    Given the work orders that PRODUCED the traced finished-good lot (collected from
    its RECEIVE txns), enumerate each WO's component ISSUE transactions and return one
    ``ConsumedComponent`` per (WO, component part, lot) with the total quantity consumed
    (reported positive). Tenant-scoped: every query filters ``company_id`` so a
    cross-tenant trace can never surface another company's genealogy (invariant #1 /
    TRACE-1).

    TWO sources of consumption are read, and both must be:
      * ``reference_type='work_order'``           — the one-shot BOM/routing backflush,
                                                    ``reference_id`` = the work order;
      * ``reference_type='work_order_operation'`` — per-run consumption of material tied
                                                    to an operation, ``reference_id`` =
                                                    the OPERATION, mapped back to its work
                                                    order here so both collapse into the
                                                    same per-WO genealogy lines.

    TWO transaction types are read, and the aggregation is SIGNED. ``RETURN`` rows are the
    reasoned compensating credit for consumption (PR 3) and mirror the reference shape of
    the ISSUE rows they compensate, so they arrive through the same predicate. Reading
    ISSUE alone would keep claiming a sheet that physically went back to the rack; adding
    RETURN to the type filter *without* the sign flip would be worse still, since the
    ``abs()`` this aggregation used to take turns a credit into MORE consumption. **This is
    the as-built record** — a returned lot must stop being claimed as built into the part
    (AS9100D 8.5.2), so a (WO, part, lot) line that nets to nothing is dropped entirely
    rather than reported as a zero-quantity component.

    The sign is keyed on ``transaction_type``, not on the stored sign of ``quantity``:
    ISSUE contributes ``+abs(quantity)`` exactly as before and RETURN contributes
    ``-abs(quantity)``, so a work order with no returns produces byte-identical lines to
    the previous ISSUE-only ``abs()`` sum, whatever sign its historical rows carry.
    """
    if not producing_work_order_ids:
        return []

    # Operations belonging to the producing WOs, so operation-scoped consumption rows
    # resolve BACK to a work order for the aggregation key below. Tenant-scoped; empty
    # when no WO has operations. (The WHERE clause itself is the shared predicate — this
    # map exists for the id resolution the predicate cannot do.)
    operation_to_work_order = {
        op_id: wo_id
        for op_id, wo_id in db.query(WorkOrderOperation.id, WorkOrderOperation.work_order_id)
        .filter(
            WorkOrderOperation.company_id == company_id,
            WorkOrderOperation.work_order_id.in_(producing_work_order_ids),
        )
        .all()
    }

    movement_txns = (
        db.query(InventoryTransaction)
        .filter(
            InventoryTransaction.company_id == company_id,
            work_order_ledger_filter(producing_work_order_ids, company_id),
            InventoryTransaction.transaction_type.in_((TransactionType.ISSUE, TransactionType.RETURN)),
        )
        .order_by(InventoryTransaction.created_at)
        .all()
    )
    if not movement_txns:
        return []

    # Resolve WO numbers + component part identity in two batched, tenant-scoped reads.
    wo_numbers = {
        wo_id: wo_number
        for wo_id, wo_number in db.query(WorkOrder.id, WorkOrder.work_order_number)
        .filter(WorkOrder.id.in_(producing_work_order_ids), WorkOrder.company_id == company_id)
        .all()
    }
    component_part_ids = {txn.part_id for txn in movement_txns if txn.part_id is not None}
    parts_by_id = {
        p.id: p for p in db.query(Part).filter(Part.id.in_(component_part_ids), Part.company_id == company_id).all()
    }

    # Aggregate per (work_order, component part, consumed lot) so multiple ISSUE rows
    # for the same component lot collapse into a single consumed-quantity line, NETTING
    # any RETURN rows that credited that same lot back off the job.
    aggregated: dict[tuple, float] = {}
    # Keys that saw at least one RETURN. The net-to-zero drop below is applied ONLY to
    # these, so a job with no returns keeps every line it emitted before -- including the
    # degenerate zero-quantity line a zero-quantity ISSUE would produce.
    returned_keys: set[tuple] = set()
    for txn in movement_txns:
        # Operation-scoped rows key on the operation; resolve to its work order so a
        # WO's per-run consumption and its backflush collapse into the same lines.
        if txn.reference_type == OPERATION_REFERENCE_TYPE:
            wo_id = operation_to_work_order.get(txn.reference_id)
            if wo_id is None:  # pragma: no cover - filtered out by the query above
                continue
        else:
            wo_id = txn.reference_id
        key = (wo_id, txn.part_id, txn.lot_number)
        magnitude = abs(float(txn.quantity or 0))
        if txn.transaction_type == TransactionType.RETURN:
            # A credit: material came back off the job. Subtract, never add.
            aggregated[key] = aggregated.get(key, 0.0) - magnitude
            returned_keys.add(key)
        else:
            aggregated[key] = aggregated.get(key, 0.0) + magnitude

    consumed: List[ConsumedComponent] = []
    for (wo_id, comp_part_id, lot), qty in aggregated.items():
        # A fully-returned lot was never built into the part; it must not appear in the
        # as-built record at all. (A partial return simply reports the net quantity.)
        if (wo_id, comp_part_id, lot) in returned_keys and qty <= LEDGER_QUANTITY_EPSILON:
            continue
        comp_part = parts_by_id.get(comp_part_id)
        consumed.append(
            ConsumedComponent(
                work_order_id=wo_id,
                work_order_number=wo_numbers.get(wo_id),
                component_part_id=comp_part_id,
                component_part_number=comp_part.part_number if comp_part else None,
                component_part_name=comp_part.name if comp_part else None,
                lot_number=lot,
                quantity=qty,
            )
        )
    return consumed


@router.get("/serial/{serial_number}")
def trace_serial(
    serial_number: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get traceability for a serial number"""
    # Similar to lot tracing but for individual serial numbers
    inv_item = (
        db.query(InventoryItem)
        .options(joinedload(InventoryItem.part))
        .filter(InventoryItem.serial_number == serial_number, InventoryItem.company_id == company_id)
        .first()
    )

    if not inv_item:
        raise HTTPException(status_code=404, detail="Serial number not found")

    transactions = (
        db.query(InventoryTransaction)
        .options(joinedload(InventoryTransaction.user))
        .filter(InventoryTransaction.serial_number == serial_number, InventoryTransaction.company_id == company_id)
        .order_by(InventoryTransaction.created_at)
        .all()
    )

    # TRACE-4: mirror trace_lot's work-order/NCR collection so a serial trace is as
    # complete as a lot trace. Collect the WOs this serial was built by / used in from
    # the transaction reference linkage (the FG-receipt + backflush txns from a WO
    # completion carry reference_type=='work_order'), and query NCRs raised against
    # this serial. Both are tenant-scoped (company_id) per invariant #1 (TRACE-1).
    work_orders_used: set[str] = set()
    history = []
    for txn in transactions:
        if txn.reference_type in WORK_ORDER_REFERENCE_TYPES and txn.reference_number:
            work_orders_used.add(txn.reference_number)
        history.append(
            {
                "timestamp": txn.created_at,
                "event_type": txn.transaction_type.value,
                "quantity": txn.quantity,
                "location": txn.to_location or txn.from_location,
                "reference": txn.reference_number,
                "reference_type": txn.reference_type,
                "user": txn.user.full_name if txn.user else None,
            }
        )

    ncrs_list: set[str] = set()
    ncrs = (
        db.query(NonConformanceReport)
        .filter(
            NonConformanceReport.serial_number == serial_number,
            NonConformanceReport.company_id == company_id,
            NonConformanceReport.is_deleted == False,  # noqa: E712
        )
        .all()
    )
    for ncr in ncrs:
        ncrs_list.add(ncr.ncr_number)
        history.append(
            {
                "timestamp": ncr.created_at,
                "event_type": "ncr",
                "reference": ncr.ncr_number,
                "reference_type": "ncr",
                "description": f"NCR #{ncr.ncr_number} - {ncr.disposition or 'Open'}",
            }
        )

    history.sort(key=lambda x: x["timestamp"])

    return {
        "serial_number": serial_number,
        "part_number": inv_item.part.part_number if inv_item.part else None,
        "part_name": inv_item.part.name if inv_item.part else None,
        "lot_number": inv_item.lot_number,
        "current_location": inv_item.location,
        "status": inv_item.status,
        "received_date": inv_item.received_date,
        "cert_number": inv_item.cert_number,
        "work_orders_used": list(work_orders_used),
        "ncrs": list(ncrs_list),
        "history": history,
    }


@router.get("/search")
def search_lots(
    q: str = Query(..., min_length=1),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Search for lot/serial numbers"""
    results = []

    # Search inventory items
    items = (
        db.query(InventoryItem)
        .options(joinedload(InventoryItem.part))
        .filter(
            InventoryItem.company_id == company_id,
            or_(
                InventoryItem.lot_number.ilike(f"%{q}%"),
                InventoryItem.serial_number.ilike(f"%{q}%"),
                InventoryItem.cert_number.ilike(f"%{q}%"),
                InventoryItem.heat_lot.ilike(f"%{q}%"),
            ),
        )
        .limit(50)
        .all()
    )

    seen = set()
    for item in items:
        if item.lot_number and item.lot_number not in seen:
            seen.add(item.lot_number)
            results.append(
                {
                    "type": "lot",
                    "number": item.lot_number,
                    "part_number": item.part.part_number if item.part else None,
                    "part_name": item.part.name if item.part else None,
                    "quantity": item.quantity_on_hand,
                    "location": item.location,
                }
            )
        if item.serial_number and item.serial_number not in seen:
            seen.add(item.serial_number)
            results.append(
                {
                    "type": "serial",
                    "number": item.serial_number,
                    "part_number": item.part.part_number if item.part else None,
                    "part_name": item.part.name if item.part else None,
                    "location": item.location,
                }
            )

    return results
