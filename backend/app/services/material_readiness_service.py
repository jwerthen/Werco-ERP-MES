"""Read-only, non-reserving material coverage for a reviewed set of jobs.

Ties are demand, not additional stock. The consumption resolver owns BOM/routing
precedence and diagnostics; this service asks it about the planned full run using
a detached value object. A shared virtual pool prevents selected jobs from each
claiming the same lot or outstanding PO line. No estimated supplier date becomes
a promise, and no held/expired/allocated/negative stock becomes usable supply.
"""

from collections import defaultdict
from dataclasses import asdict
from datetime import date, datetime
from types import SimpleNamespace

from app.db.ledger_filter import work_order_ledger_filter
from app.models.bom import BOM, BOMItem
from app.models.inventory import InventoryItem, InventoryTransaction, TransactionType
from app.models.part import Part, uom_disagrees
from app.models.purchasing import POStatus, PurchaseOrder, PurchaseOrderLine
from app.models.work_order import WorkOrderOperation
from app.models.work_order_material import AllocationStatus, WorkOrderMaterialAllocation
from app.services.completion_inventory_service import _resolve_backflush_demand
from app.services.material_consumption_service import CONSUMABLE_ITEM_CLAUSES


def _day(value):
    return value.date() if isinstance(value, datetime) else value


def _stamp(value):
    return value.isoformat() if value is not None else None


def _number(value):
    return float(value or 0)


def material_readiness(db, company_id: int, orders: list, today: date) -> dict:
    """Coverage in caller-supplied job order. Snapshot is private token input."""
    ids = [wo.id for wo in orders]
    allocations = (
        db.query(WorkOrderMaterialAllocation)
        .filter(
            WorkOrderMaterialAllocation.company_id == company_id,
            WorkOrderMaterialAllocation.work_order_id.in_(ids),
            WorkOrderMaterialAllocation.status == AllocationStatus.OPEN,
        )
        .order_by(WorkOrderMaterialAllocation.id)
        .populate_existing()
        .all()
    )
    transactions = (
        db.query(InventoryTransaction)
        .filter(InventoryTransaction.company_id == company_id, work_order_ledger_filter(ids, company_id))
        .order_by(InventoryTransaction.id)
        .populate_existing()
        .all()
    )
    operations = (
        db.query(WorkOrderOperation)
        .filter(WorkOrderOperation.company_id == company_id, WorkOrderOperation.work_order_id.in_(ids))
        .populate_existing()
        .all()
    )
    consumed = defaultdict(float)
    tied_consumed = defaultdict(float)
    # Receipt/transfer quantities are not material consumption. Signed ISSUE and
    # RETURN rows net together, including compensating returns and legacy shapes.
    for row in transactions:
        if row.transaction_type in (TransactionType.ISSUE, TransactionType.RETURN):
            if row.allocation_id is not None:
                tied_consumed[row.allocation_id] -= _number(row.quantity)
    parts = {
        part.id: part
        for part in db.query(Part)
        .filter(Part.company_id == company_id, Part.id.in_([wo.part_id for wo in orders if wo.part_id]))
        .populate_existing()
        .all()
    }
    requirements = {}
    diagnostics = {}
    for wo in orders:
        # Never copy an ORM instance and assign its instrumented attributes: its
        # shared InstanceState could make a preview dirty.
        planned = SimpleNamespace(**{col.name: getattr(wo, col.name) for col in wo.__table__.columns})
        planned.part = parts.get(wo.part_id)
        planned.operations = [op for op in operations if op.work_order_id == wo.id]
        planned.quantity_complete = max(_number(wo.quantity_ordered), _number(wo.quantity_complete))
        planned.quantity_scrapped = _number(wo.quantity_scrapped)
        ties = [row for row in allocations if row.work_order_id == wo.id]
        resolution = _resolve_backflush_demand(db, planned, company_id, ties)
        diagnostics[wo.id] = [asdict(row) for row in resolution.diagnostics if row.severity == "blocking"]
        demand = dict(resolution.demand)
        demand.update({key: value[0] for key, value in resolution.ledger_blocked.items()})
        op_ids = {op.id for op in planned.operations}
        for row in transactions:
            belongs = (row.reference_type == "work_order_operation" and row.reference_id in op_ids) or (
                row.reference_type != "work_order_operation" and row.reference_id == wo.id
            )
            if (
                belongs
                and row.allocation_id is None
                and row.transaction_type in (TransactionType.ISSUE, TransactionType.RETURN)
            ):
                consumed[(wo.id, row.part_id)] -= _number(row.quantity)
        tied_parts = {row.part_id for row in ties}
        rows = []
        for part_id, quantity in sorted(demand.items()):
            if part_id not in tied_parts:
                rows.append(
                    {
                        "part_id": part_id,
                        "required_quantity": max(0, quantity - consumed[(wo.id, part_id)]),
                        "allocation_id": None,
                        "pinned_inventory_item_id": None,
                        "unit_of_measure": getattr(parts.get(part_id), "unit_of_measure", None),
                    }
                )
        for tie in ties:
            if tie.work_order_operation_id is not None and tie.work_order_operation_id not in op_ids:
                diagnostics[wo.id].append({"detail": "A material tie references an unavailable operation."})
            rows.append(
                {
                    "part_id": tie.part_id,
                    "required_quantity": max(0, _number(tie.qty_planned) - max(0, tied_consumed[tie.id])),
                    "allocation_id": tie.id,
                    "pinned_inventory_item_id": tie.pinned_inventory_item_id,
                    "unit_of_measure": tie.unit_of_measure,
                }
            )
        requirements[wo.id] = rows

    part_ids = {row["part_id"] for rows in requirements.values() for row in rows}
    parts.update(
        {
            part.id: part
            for part in db.query(Part)
            .filter(Part.company_id == company_id, Part.id.in_(part_ids))
            .populate_existing()
            .all()
        }
    )
    # Discover the bounded BOM dependency graph for apply locks, including phantom
    # parents which disappear from the final purchased-material demand.
    dependency_parts = set(part_ids) | {wo.part_id for wo in orders if wo.part_id}
    frontier = {wo.part_id for wo in orders if wo.part_id}
    bom_ids = set()
    visited_parts = set()
    for _ in range(21):
        visited_parts.update(frontier)
        headers = db.query(BOM).filter(BOM.company_id == company_id, BOM.part_id.in_(frontier)).all()
        new_ids = {row.id for row in headers} - bom_ids
        if not new_ids:
            break
        bom_ids.update(new_ids)
        children = {
            row.component_part_id
            for row in db.query(BOMItem).filter(BOMItem.company_id == company_id, BOMItem.bom_id.in_(new_ids)).all()
        }
        frontier = children - visited_parts
        dependency_parts.update(children)
        if not frontier:
            break
    stock = (
        db.query(InventoryItem)
        .filter(InventoryItem.company_id == company_id, InventoryItem.part_id.in_(part_ids))
        .order_by(InventoryItem.id)
        .populate_existing()
        .all()
    )
    usable_ids = {
        row.id
        for row in db.query(InventoryItem.id)
        .filter(InventoryItem.company_id == company_id, InventoryItem.part_id.in_(part_ids), *CONSUMABLE_ITEM_CLAUSES)
        .all()
    }
    stock_left = {
        row.id: (
            max(0, _number(row.quantity_on_hand) - max(0, _number(row.quantity_allocated)))
            if row.id in usable_ids and (not row.expiration_date or _day(row.expiration_date) >= today)
            else 0
        )
        for row in stock
    }
    supply = (
        db.query(PurchaseOrderLine, PurchaseOrder)
        .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderLine.purchase_order_id)
        .filter(
            PurchaseOrder.company_id == company_id,
            PurchaseOrderLine.company_id == company_id,
            PurchaseOrder.is_deleted.is_(False),
            PurchaseOrderLine.part_id.in_(part_ids),
            PurchaseOrder.status.in_([POStatus.SENT, POStatus.PARTIAL]),
            PurchaseOrderLine.is_closed.is_(False),
        )
        .order_by(PurchaseOrderLine.id)
        .populate_existing()
        .all()
    )
    po_left = {line.id: max(0, _number(line.quantity_ordered) - _number(line.quantity_received)) for line, _ in supply}
    supply.sort(key=lambda pair: (getattr(pair[1], "supplier_confirmed_date", None) or date.max, pair[0].id))
    jobs = {}
    for wo in orders:
        ready = today
        lines = []
        unknown = bool(diagnostics[wo.id])
        for req in requirements[wo.id]:
            part = parts.get(req["part_id"])
            needed = req["required_quantity"]
            remaining = needed
            sources = []
            if req["allocation_id"] is None and part is not None:
                req["unit_of_measure"] = part.unit_of_measure
            incompatible = part is None or uom_disagrees(req["unit_of_measure"], part.unit_of_measure)
            pin = req["pinned_inventory_item_id"]
            if not incompatible:
                for lot in stock:
                    if lot.part_id != req["part_id"] or (pin is not None and pin != lot.id):
                        continue
                    take = min(remaining, stock_left[lot.id])
                    if take > 0:
                        sources.append(
                            {
                                "kind": "stock",
                                "id": lot.id,
                                "label": lot.lot_number or f"Stock #{lot.id}",
                                "quantity": take,
                                "available_date": today.isoformat(),
                                "expires_on": _stamp(_day(lot.expiration_date)),
                            }
                        )
                        stock_left[lot.id] -= take
                        remaining -= take
                # Future unassigned supply cannot satisfy a tie pinned to one existing lot.
                if pin is None:
                    for line, po in supply:
                        confirmed = getattr(po, "supplier_confirmed_date", None)
                        if (
                            line.part_id != req["part_id"]
                            or not getattr(po, "supplier_acknowledged_at", None)
                            or not confirmed
                            or confirmed < today
                        ):
                            continue
                        take = min(remaining, po_left[line.id])
                        if take > 0:
                            sources.append(
                                {
                                    "kind": "purchase_order",
                                    "id": po.id,
                                    "line_id": line.id,
                                    "label": po.po_number,
                                    "quantity": take,
                                    "available_date": confirmed.isoformat(),
                                }
                            )
                            ready = max(ready, confirmed)
                            po_left[line.id] -= take
                            remaining -= take
            missing = remaining > 1e-9 or incompatible
            unknown = unknown or missing
            reason = None
            if incompatible:
                reason = "Material identity or unit of measure needs review."
            elif missing:
                reason = (
                    "Pinned lot is insufficient or unavailable."
                    if pin
                    else "Insufficient usable stock and confirmed future arrivals. Overdue or unconfirmed supply needs follow-up."
                )
            lines.append(
                {
                    "part_id": part.id if part else None,
                    "part_number": part.part_number if part else "Unavailable material",
                    "unit_of_measure": (
                        str(getattr(part.unit_of_measure, "value", part.unit_of_measure)) if part else None
                    ),
                    "required_quantity": needed,
                    "covered_quantity": max(0, needed - remaining),
                    "shortage_quantity": max(0, remaining),
                    "reason": reason,
                    "sources": sources,
                }
            )
        for line in lines:
            expiring = [
                source
                for source in line["sources"]
                if source.get("expires_on") and source["expires_on"] < ready.isoformat()
            ]
            if expiring:
                unknown = True
                lost = sum(source["quantity"] for source in expiring)
                line["covered_quantity"] = max(0, line["covered_quantity"] - lost)
                line["shortage_quantity"] += lost
                line["sources"] = [source for source in line["sources"] if source not in expiring]
                line["reason"] = "Stock expires before the other required material arrives."
        jobs[wo.id] = {
            "status": "unknown" if unknown else "ready" if lines else "not_defined",
            "ready_date": ready.isoformat() if not unknown and lines else None,
            "lines": lines,
            "warnings": [row["detail"] for row in diagnostics[wo.id]],
            "basis": "Shared planning estimate; stock and incoming supply are not reserved by this preview.",
        }
        if not lines:
            jobs[wo.id]["warnings"].append("No material requirements are recorded; material readiness is not verified.")
    return {
        "jobs": jobs,
        "snapshot": {
            "requirements": jobs,
            "stock": [
                [
                    row.id,
                    row.part_id,
                    row.quantity_on_hand,
                    row.quantity_allocated,
                    row.status,
                    row.is_active,
                    _stamp(row.expiration_date),
                    _stamp(row.updated_at),
                ]
                for row in stock
            ],
            "supply": [
                [
                    line.id,
                    po.id,
                    line.quantity_ordered,
                    line.quantity_received,
                    _stamp(getattr(po, "supplier_confirmed_date", None)),
                    _stamp(getattr(po, "supplier_acknowledged_at", None)),
                    _stamp(po.updated_at),
                ]
                for line, po in supply
            ],
        },
        "part_ids": sorted(dependency_parts),
        "bom_ids": sorted(bom_ids),
    }


def lock_material_dependencies(db, company_id, orders, readiness):
    """Apply-only fences. Preview never locks or writes.

    Parent FK locks fence new supply/stock/ties; row locks fence edits to existing
    evidence. BOM headers/items are tenant-scoped because phantom dependencies can
    be introduced by an edit while the plan is being validated.
    """
    part_ids = readiness["part_ids"]
    po_ids = [
        row.purchase_order_id
        for row in db.query(PurchaseOrderLine.purchase_order_id)
        .filter(PurchaseOrderLine.company_id == company_id, PurchaseOrderLine.part_id.in_(part_ids))
        .all()
    ]
    # Receive/inspection paths own PO headers/lines before touching stock.
    db.query(PurchaseOrder).filter(PurchaseOrder.company_id == company_id, PurchaseOrder.id.in_(po_ids)).order_by(
        PurchaseOrder.id
    ).with_for_update().all()
    db.query(PurchaseOrderLine).filter(
        PurchaseOrderLine.company_id == company_id, PurchaseOrderLine.part_id.in_(part_ids)
    ).order_by(PurchaseOrderLine.id).with_for_update().all()
    db.query(BOM).filter(BOM.company_id == company_id, BOM.id.in_(readiness["bom_ids"])).order_by(
        BOM.id
    ).with_for_update().all()
    db.query(BOMItem).filter(BOMItem.company_id == company_id, BOMItem.bom_id.in_(readiness["bom_ids"])).order_by(
        BOMItem.id
    ).with_for_update().all()
    db.query(Part).filter(Part.company_id == company_id, Part.id.in_(part_ids)).order_by(
        Part.id
    ).with_for_update().all()
    db.query(WorkOrderMaterialAllocation).filter(
        WorkOrderMaterialAllocation.company_id == company_id,
        WorkOrderMaterialAllocation.work_order_id.in_([wo.id for wo in orders]),
    ).order_by(WorkOrderMaterialAllocation.id).with_for_update().all()
    db.query(InventoryItem).filter(
        InventoryItem.company_id == company_id, InventoryItem.part_id.in_(part_ids)
    ).order_by(InventoryItem.id).with_for_update().all()


def load_locked_material_readiness(db, company_id, orders, today):
    """Legacy/direct writers validate live coverage under the same evidence locks."""
    ids = [wo.id for wo in orders]
    from app.models.work_order import WorkOrder

    db.query(WorkOrder).filter(WorkOrder.company_id == company_id, WorkOrder.id.in_(ids)).order_by(
        WorkOrder.id
    ).with_for_update().all()
    db.query(WorkOrderOperation).filter(
        WorkOrderOperation.company_id == company_id, WorkOrderOperation.work_order_id.in_(ids)
    ).order_by(WorkOrderOperation.id).with_for_update().all()
    readiness = material_readiness(db, company_id, orders, today)
    lock_material_dependencies(db, company_id, orders, readiness)
    current = material_readiness(db, company_id, orders, today)
    if current['part_ids'] != readiness['part_ids'] or current['bom_ids'] != readiness['bom_ids']:
        from fastapi import HTTPException

        raise HTTPException(409, 'Material requirements changed during scheduling. Refresh and retry.')
    return current['jobs']


def material_start_date(materials):
    from fastapi import HTTPException

    if materials['status'] == 'unknown':
        raise HTTPException(
            409,
            'Material-ready date is unknown. Resolve the shortages or unconfirmed supplier arrivals before scheduling.',
        )
    return date.fromisoformat(materials['ready_date']) if materials['ready_date'] else None


def validate_material_start(materials, start):
    from fastapi import HTTPException

    ready = material_start_date(materials)
    start = _day(start)
    if ready and (start is None or start < ready):
        raise HTTPException(
            409, f'Material is not ready until {ready.isoformat()}. Review earliest available scheduling.'
        )
    if start and any(
        source.get('expires_on') and source['expires_on'] < start.isoformat()
        for line in materials['lines']
        for source in line['sources']
    ):
        raise HTTPException(409, 'Covered stock expires before the proposed start. Review replacement supply.')
