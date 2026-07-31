from datetime import date, datetime
from typing import Dict, List, Optional, Sequence

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session, joinedload

from app.api.deps import get_audit_service, get_current_company_id, get_current_user, require_role
from app.db.database import atomic_transaction, get_db
from app.db.ledger_filter import LEDGER_QUANTITY_EPSILON, WORK_ORDER_REFERENCE_TYPE, work_order_ledger_filter
from app.models.inventory import (
    CycleCount,
    CycleCountItem,
    CycleCountStatus,
    InventoryItem,
    InventoryLocation,
    InventoryTransaction,
    TransactionType,
)
from app.models.part import Part
from app.models.user import User, UserRole
from app.models.work_order import WorkOrder
from app.schemas.inventory import InventoryTransactionResponse
from app.services.audit_service import AuditService
from app.services.operational_event_service import OperationalEventService

router = APIRouter()

# A cycle count in either of these states is closed for good. COMPLETED has already
# posted its variance to the ledger; CANCELLED was deliberately abandoned. Re-opening
# or re-completing one would append a SECOND COUNT transaction for the same physical
# variance, permanently diverging the ledger from on-hand.
TERMINAL_COUNT_STATUSES = (CycleCountStatus.COMPLETED, CycleCountStatus.CANCELLED)

# Every verb that moves stock or posts a row to the inventory ledger. Matches the
# PO-receipt path ``POST /receiving/receive``, which writes the same
# ``inventory_items`` / ``inventory_transactions`` tables. Used by /receive, /issue,
# /transfer, /adjust, and both privileged cycle-count steps (create + complete).
STOCK_MUTATOR_ROLES = [UserRole.ADMIN, UserRole.MANAGER, UserRole.SUPERVISOR]

# Who may write a counted quantity (``start`` + ``record_count``). Deliberately
# defined by EXCLUSION: counting is a shop-floor task, so every working role keeps
# it -- only VIEWER, which is read-only by definition (``inventory:view`` and
# nothing else in ``frontend/src/utils/permissions.ts``), is refused. A counted
# quantity is the quality record the manager's ledger-posting adjustment is derived
# from, so it is not a read.
COUNT_WRITE_ROLES = [
    UserRole.ADMIN,
    UserRole.MANAGER,
    UserRole.SUPERVISOR,
    UserRole.OPERATOR,
    UserRole.QUALITY,
    UserRole.SHIPPING,
]

# Bound on the ``IN (...)`` list used to bulk-load stock rows for a completion, so a
# warehouse-wide count cannot blow past the driver's bind-parameter ceiling.
_IN_CHUNK = 500


def _status_value(status: Optional[CycleCountStatus]) -> str:
    """The status column's value as a plain string, for messages and audit payloads.

    ``CycleCount.status`` is a ``SQLEnum`` column, so SQLAlchemy always hands back the
    enum member itself -- never its name or value as a bare string. That is why the
    lifecycle guards compare ``count.status`` to members directly, and why this helper
    does not carry a "maybe it's already a string" branch: the only value it can see
    that is not a member is ``None`` (the column is nullable, so a row written outside
    the ORM -- which supplies the SCHEDULED default -- can carry NULL).
    """
    return status.value if status is not None else "unknown"


def _audit_stock_movement(
    audit: AuditService,
    txn: InventoryTransaction,
    inv: InventoryItem,
    old_quantity: float,
    new_quantity: float,
    *,
    movement_description: str,
    stock_label: str,
) -> None:
    """Write the dual-row tamper-evident trail for one stock movement.

    Every stock mutator in this module records two audit rows: an ``inventory``
    CREATE for the ``InventoryTransaction`` (the movement) and an ``inventory``
    UPDATE for the ``quantity_on_hand`` change it produced. Callers invoke this
    inside their ``atomic_transaction`` block so the audit rows commit with the
    inventory write.

    Used by ``/inventory/issue``, ``/inventory/adjust`` and
    ``/inventory/cycle-counts/{id}/complete``. ``/receive`` and ``/transfer`` write
    their own blocks: receive's stock update is conditional (a brand-new row has no
    "old" quantity) and identifies the row by part number, and transfer produces a
    THIRD row for the destination increment.
    """
    audit.log_create("inventory", txn.id, str(txn.id), new_values=txn, description=movement_description)
    audit.log_update(
        "inventory",
        inv.id,
        f"inventory_item {inv.id} @ {inv.location}",
        old_values={"quantity_on_hand": old_quantity},
        new_values={"quantity_on_hand": new_quantity},
        description=f"{stock_label}: stock for inventory item {inv.id} at {inv.location}",
    )


def _load_inventory_items(
    db: Session, company_id: int, item_ids: Sequence[int], *, for_update: bool = False
) -> Dict[int, InventoryItem]:
    """Tenant-scoped bulk load of stock rows, keyed by id.

    Replaces a per-item ``SELECT`` inside the cycle-count completion loop:
    ``create_cycle_count`` enrolls every stock row in a warehouse, so that loop is
    inherently bulk. Rows belonging to another company are simply absent from the
    map, which is what makes the caller's "skip anything not ours" guard work.

    ``for_update=True`` takes ``FOR UPDATE`` row locks, acquired in ascending-id order
    (the same lock-ordering rule as the consumption engine, so a completion drawing the
    same lots concurrently cannot deadlock against a cycle-count posting). No-op on the
    SQLite test backend.
    """
    loaded: Dict[int, InventoryItem] = {}
    ids = sorted(i for i in item_ids if i is not None)
    for start in range(0, len(ids), _IN_CHUNK):
        chunk = ids[start : start + _IN_CHUNK]
        query = db.query(InventoryItem).filter(InventoryItem.company_id == company_id, InventoryItem.id.in_(chunk))
        if for_update:
            query = query.order_by(InventoryItem.id.asc()).with_for_update()
        loaded.update({row.id: row for row in query.all()})
    return loaded


def _find_stock_row(
    db: Session,
    *,
    company_id: int,
    part_id: int,
    location_code: str,
    lot_number: Optional[str],
    for_update: bool = False,
) -> Optional[InventoryItem]:
    """The existing stock row for (part, location, lot), tenant-scoped. Shared by
    ``/receive`` and ``/transfer``.

    ``lot_number`` branches to ``IS NULL`` when the incoming lot is ``None``: the naive
    ``lot_number == None`` comparison compiles to ``lot_number = NULL``, which never
    matches in SQL, so every lot-less receive minted a brand-new fragment row instead of
    incrementing the one that was already there.

    ``for_update=True`` locks the row before the read-modify-write of
    ``quantity_on_hand`` (no-op on SQLite; the postgresql dialect-compile test in
    ``test_inventory_row_locking.py`` pins the ``FOR UPDATE``). Ordered by id so a
    legacy fragmented set resolves deterministically to its oldest row.
    """
    return _stock_row_query(
        db,
        company_id=company_id,
        part_id=part_id,
        location_code=location_code,
        lot_number=lot_number,
        for_update=for_update,
    ).first()


def _stock_row_query(
    db: Session,
    *,
    company_id: int,
    part_id: int,
    location_code: str,
    lot_number: Optional[str],
    for_update: bool = False,
):
    """The query behind ``_find_stock_row``, exposed for the dialect-compile test."""
    lot_clause = InventoryItem.lot_number.is_(None) if lot_number is None else InventoryItem.lot_number == lot_number
    query = db.query(InventoryItem).filter(
        InventoryItem.company_id == company_id,
        InventoryItem.part_id == part_id,
        InventoryItem.location == location_code,
        lot_clause,
    )
    query = query.order_by(InventoryItem.id.asc())
    if for_update:
        query = query.with_for_update()
    return query


@router.get("/low-stock")
def get_low_stock_alerts(
    limit: int = Query(500, ge=1, le=2000),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get parts with inventory below reorder point.

    Filter is applied in SQL so we don't load the full parts catalog into
    memory. A hard limit bounds the response size (``ge=1`` so a negative value
    cannot reach ``.limit()`` -- PostgreSQL rejects a negative LIMIT and SQLite
    silently treats it as "unbounded"); the frontend paginates display in any case.
    """
    # Subquery: sum of on-hand quantity per active inventory row, by part.
    # Tenant-scoped so the aggregate can never sum another company's stock into
    # this company's on-hand figure.
    qty_subq = (
        db.query(
            InventoryItem.part_id.label("pid"),
            func.coalesce(func.sum(InventoryItem.quantity_on_hand), 0).label("total_qty"),
        )
        .filter(InventoryItem.company_id == company_id, InventoryItem.is_active == True)
        .group_by(InventoryItem.part_id)
        .subquery()
    )

    total_qty_col = func.coalesce(qty_subq.c.total_qty, 0)

    rows = (
        db.query(Part, total_qty_col.label("total_qty"))
        .outerjoin(qty_subq, Part.id == qty_subq.c.pid)
        .filter(
            Part.company_id == company_id,
            # Part is the one SoftDeleteMixin model this module touches. Deleting a
            # part also clears is_active, so the predicate below is belt-and-braces
            # today -- but is_active is an operational flag anyone can toggle back,
            # while is_deleted is the deletion record. Filter on the real one too, so
            # a restored-to-active-but-still-deleted part can't reappear as a
            # purchasing signal.
            Part.is_deleted == False,
            Part.is_active == True,
            Part.reorder_point > 0,
            total_qty_col <= Part.reorder_point,
        )
        .limit(limit)
        .all()
    )

    alerts = []
    for part, total_qty in rows:
        total_qty = float(total_qty or 0)
        alerts.append(
            {
                "part_id": part.id,
                "part_number": part.part_number,
                "part_name": part.name,
                "quantity_on_hand": total_qty,
                "reorder_point": part.reorder_point,
                "reorder_quantity": part.reorder_quantity,
                "safety_stock": part.safety_stock,
                "shortage": part.reorder_point - total_qty,
                "is_critical": total_qty <= (part.safety_stock or 0),
            }
        )

    # Sort by critical first, then by shortage
    alerts.sort(key=lambda x: (not x["is_critical"], -x["shortage"]))
    return alerts


# Pydantic schemas
class LocationCreate(BaseModel):
    code: str
    name: Optional[str] = None
    warehouse: str
    zone: Optional[str] = None
    aisle: Optional[str] = None
    rack: Optional[str] = None
    shelf: Optional[str] = None
    bin: Optional[str] = None
    location_type: str = "bin"
    is_pickable: bool = True
    is_receivable: bool = True


# Movement quantities are strictly positive (Field(gt=0)): a NEGATIVE issue would MINT
# stock while writing a positive-quantity ISSUE ledger row with a negative total_cost, a
# negative receive would remove stock, and a negative transfer would move dest->source
# against locations/lots the response never named. Direction is the verb, never the sign.
class ReceiveItemRequest(BaseModel):
    part_id: int
    quantity: float = Field(gt=0)
    location_code: str
    lot_number: Optional[str] = None
    serial_number: Optional[str] = None
    po_number: Optional[str] = None
    unit_cost: float = 0.0
    cert_number: Optional[str] = None
    heat_lot: Optional[str] = None
    notes: Optional[str] = None


class IssueItemRequest(BaseModel):
    inventory_item_id: int
    quantity: float = Field(gt=0)
    work_order_number: Optional[str] = None
    notes: Optional[str] = None


class TransferRequest(BaseModel):
    inventory_item_id: int
    quantity: float = Field(gt=0)
    to_location_code: str
    notes: Optional[str] = None


class AdjustmentRequest(BaseModel):
    inventory_item_id: int
    # Absolute target on-hand: zero is a legitimate write-off, negative is not a state a
    # manual adjustment may DICTATE (only the shortage engine drives a lot negative, and
    # the manual remedy is adjusting it back UP).
    new_quantity: float = Field(ge=0)
    reason_code: str
    notes: Optional[str] = None


class CycleCountCreate(BaseModel):
    warehouse: Optional[str] = None
    location_code: Optional[str] = None
    part_id: Optional[int] = None
    scheduled_date: date
    notes: Optional[str] = None


class CountItemRequest(BaseModel):
    # A physical count observation can be zero (nothing on the shelf) but never negative.
    counted_quantity: float = Field(ge=0)
    notes: Optional[str] = None


# Location endpoints
@router.get("/locations")
def list_locations(
    warehouse: Optional[str] = None,
    active_only: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    query = db.query(InventoryLocation).filter(InventoryLocation.company_id == company_id)
    if warehouse:
        query = query.filter(InventoryLocation.warehouse == warehouse)
    if active_only:
        query = query.filter(InventoryLocation.is_active == True)
    return query.order_by(InventoryLocation.code).all()


@router.post("/locations")
def create_location(
    loc_in: LocationCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role([UserRole.ADMIN, UserRole.MANAGER])),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    existing = (
        db.query(InventoryLocation)
        .filter(InventoryLocation.code == loc_in.code, InventoryLocation.company_id == company_id)
        .first()
    )
    if existing:
        raise HTTPException(status_code=400, detail="Location code already exists")

    location = InventoryLocation(**loc_in.model_dump())
    location.company_id = company_id
    db.add(location)
    db.flush()

    # Tamper-evident audit trail (invariant #2): a location is the scoping anchor for
    # receives, transfers and cycle counts, so its creation is a state change the hash
    # chain must record. Written before the commit so it lands with the row.
    audit.log_create(
        "inventory_location",
        location.id,
        location.code,
        new_values=location,
        description=f"Created inventory location {location.code} (warehouse {location.warehouse})",
    )

    db.commit()
    db.refresh(location)
    return location


# Inventory endpoints
@router.get("/")
def list_inventory(
    part_id: Optional[int] = None,
    warehouse: Optional[str] = None,
    location_code: Optional[str] = None,
    has_quantity: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    query = (
        db.query(InventoryItem).filter(InventoryItem.company_id == company_id).options(joinedload(InventoryItem.part))
    )

    if part_id:
        query = query.filter(InventoryItem.part_id == part_id)
    if warehouse:
        query = query.filter(InventoryItem.warehouse == warehouse)
    if location_code:
        query = query.filter(InventoryItem.location == location_code)
    if has_quantity:
        # != 0, not > 0: the shortage posture deliberately drives a lot NEGATIVE rather
        # than fail a completion, and a driven-negative lot is a discrepancy someone has
        # to see and fix — filtering it out made it invisible to the one list view.
        query = query.filter(InventoryItem.quantity_on_hand != 0)

    return query.order_by(InventoryItem.part_id, InventoryItem.location).all()


@router.get("/summary")
def get_inventory_summary(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Get inventory summary by part with locations"""
    # Get all inventory items with quantity
    items = (
        db.query(InventoryItem)
        .options(joinedload(InventoryItem.part))
        .filter(
            InventoryItem.company_id == company_id, InventoryItem.is_active == True, InventoryItem.quantity_on_hand > 0
        )
        .all()
    )

    # Group by part
    by_part = {}
    for item in items:
        pid = item.part_id
        if pid not in by_part:
            by_part[pid] = {
                "part_id": pid,
                "part_number": item.part.part_number if item.part else "",
                "part_name": item.part.name if item.part else "",
                "total_on_hand": 0,
                "total_allocated": 0,
                "locations": [],
            }
        by_part[pid]["total_on_hand"] += item.quantity_on_hand
        by_part[pid]["total_allocated"] += item.quantity_allocated
        by_part[pid]["locations"].append(
            {"location": item.location, "quantity": item.quantity_on_hand, "lot_number": item.lot_number}
        )

    result = []
    for data in by_part.values():
        data["available"] = data["total_on_hand"] - data["total_allocated"]
        result.append(data)

    return result


@router.post("/receive")
def receive_inventory(
    receive_in: ReceiveItemRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(STOCK_MUTATOR_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Receive inventory into stock.

    Role gate matches ``POST /receiving/receive`` — the PO-receipt path that writes
    the same ``inventory_items`` / ``inventory_transactions`` tables — and the sibling
    stock mutators ``/inventory/issue`` and ``/inventory/adjust``. Previously this
    endpoint took only ``get_current_user``, so any authenticated user (VIEWER
    included) could create stock and a ledger row.

    A soft-deleted part is refused with **400**, matching the repo-wide deleted-part
    policy (``po_upload.py`` / ``bom.py``): "restore it or use a different part
    number". Without the ``is_deleted`` predicate a Manager could create brand-new
    stock and a ledger row against a part the business has deleted.
    """
    # Verify part exists and is not deleted (Part is a SoftDeleteMixin model).
    part = (
        db.query(Part)
        .filter(Part.id == receive_in.part_id, Part.company_id == company_id, Part.is_deleted == False)
        .first()
    )
    if not part:
        deleted_part = (
            db.query(Part)
            .filter(Part.id == receive_in.part_id, Part.company_id == company_id, Part.is_deleted == True)
            .first()
        )
        if deleted_part:
            raise HTTPException(
                status_code=400,
                detail=f"Part '{deleted_part.part_number}' is deleted - restore it or use a different part number",
            )
        raise HTTPException(status_code=404, detail="Part not found")

    # Verify location exists (tenant-scoped: another company's location code is "not found")
    location = (
        db.query(InventoryLocation)
        .filter(InventoryLocation.code == receive_in.location_code, InventoryLocation.company_id == company_id)
        .first()
    )
    if not location:
        raise HTTPException(status_code=404, detail="Location not found")

    # Check for existing inventory at this location with same lot (tenant-scoped, so a
    # matching lot in another company can never be the row we increment). Shared helper:
    # NULL-safe on a lot-less receive, and row-locked for the increment below.
    existing = _find_stock_row(
        db,
        company_id=company_id,
        part_id=receive_in.part_id,
        location_code=receive_in.location_code,
        lot_number=receive_in.lot_number,
        for_update=True,
    )

    old_quantity_on_hand = existing.quantity_on_hand if existing else None

    with atomic_transaction(db):
        if existing:
            existing.quantity_on_hand += receive_in.quantity
            existing.quantity_available = existing.quantity_on_hand - existing.quantity_allocated
            inv_item = existing
        else:
            inv_item = InventoryItem(
                part_id=receive_in.part_id,
                location=receive_in.location_code,
                warehouse=location.warehouse,
                quantity_on_hand=receive_in.quantity,
                quantity_available=receive_in.quantity,
                lot_number=receive_in.lot_number,
                serial_number=receive_in.serial_number,
                po_number=receive_in.po_number,
                unit_cost=receive_in.unit_cost,
                cert_number=receive_in.cert_number,
                heat_lot=receive_in.heat_lot,
                received_date=datetime.utcnow(),
            )
            inv_item.company_id = company_id
            db.add(inv_item)

        db.flush()

        # Create transaction
        txn = InventoryTransaction(
            company_id=company_id,
            inventory_item_id=inv_item.id,
            part_id=receive_in.part_id,
            transaction_type=TransactionType.RECEIVE,
            quantity=receive_in.quantity,
            to_location=receive_in.location_code,
            lot_number=receive_in.lot_number,
            serial_number=receive_in.serial_number,
            reference_type="purchase_order" if receive_in.po_number else None,
            reference_number=receive_in.po_number,
            unit_cost=receive_in.unit_cost,
            total_cost=receive_in.quantity * receive_in.unit_cost,
            notes=receive_in.notes,
            created_by=current_user.id,
        )
        db.add(txn)
        db.flush()
        OperationalEventService(db).emit_best_effort(
            company_id=company_id,
            event_type="inventory_received",
            source_module="inventory",
            entity_type="inventory_transaction",
            entity_id=txn.id,
            user_id=current_user.id,
            severity="info",
            event_payload={
                "part_id": receive_in.part_id,
                "part_number": part.part_number,
                "quantity": receive_in.quantity,
                "location": receive_in.location_code,
                "lot_number": receive_in.lot_number,
                "po_number": receive_in.po_number,
            },
        )

        # Tamper-evident audit trail (hash chain) for the stock movement. Flushed
        # inside the atomic block so the audit row commits with the inventory write.
        audit.log_create(
            "inventory",
            txn.id,
            str(txn.id),
            new_values=txn,
            description=(
                f"Received {receive_in.quantity} of part {part.part_number} "
                f"into {receive_in.location_code}" + (f" lot {receive_in.lot_number}" if receive_in.lot_number else "")
            ),
        )
        if old_quantity_on_hand is not None:
            audit.log_update(
                "inventory",
                inv_item.id,
                f"{part.part_number} @ {receive_in.location_code}",
                old_values={"quantity_on_hand": old_quantity_on_hand},
                new_values={"quantity_on_hand": inv_item.quantity_on_hand},
                description=f"Receive: stock for part {part.part_number} at {receive_in.location_code}",
            )

    return {"message": "Inventory received", "inventory_item_id": inv_item.id, "quantity": receive_in.quantity}


@router.post("/issue", deprecated=True, summary="Issue inventory manually (deprecated)")
def issue_inventory(
    issue_in: IssueItemRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(STOCK_MUTATOR_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Issue inventory manually (deprecated).

    DEPRECATION NOTICE: this free-form issue verb is slated for removal. Work-order
    material consumption goes through material ties (``work_order_material_allocations``
    -- consumption posts automatically as operations/work orders complete), and manual
    stock corrections go through ``POST /inventory/adjust``. Role gate matches the
    sibling ``/inventory/adjust`` stock-mutating endpoint.

    Work-order attribution is REFUSED here (400): this endpoint could only record it as
    ``reference_type='work_order'`` + ``reference_number`` with NO ``reference_id``,
    a shape invisible to ``work_order_ledger_filter`` -- i.e. to job costing, lot
    genealogy, analytics and the backflush nets. A movement the work-order record
    cannot see is worse than no attribution at all.
    """
    if issue_in.work_order_number:
        raise HTTPException(
            status_code=400,
            detail=(
                "Work-order attribution is not supported on this deprecated endpoint: the row it would "
                "write is invisible to work-order material history and job costing. Tie the material to "
                "the work order (material allocations) so consumption posts through the completion flows, "
                "or use POST /inventory/adjust for a manual correction."
            ),
        )

    # FOR UPDATE (no-op on SQLite): the decrement below is a read-modify-write, and the
    # consumption engine may be drawing the same lot concurrently.
    inv_item = (
        db.query(InventoryItem)
        .filter(InventoryItem.id == issue_in.inventory_item_id, InventoryItem.company_id == company_id)
        .with_for_update()
        .first()
    )
    if not inv_item:
        raise HTTPException(status_code=404, detail="Inventory item not found")

    if inv_item.quantity_available < issue_in.quantity:
        raise HTTPException(status_code=400, detail=f"Insufficient quantity. Available: {inv_item.quantity_available}")

    old_quantity_on_hand = inv_item.quantity_on_hand

    with atomic_transaction(db):
        inv_item.quantity_on_hand -= issue_in.quantity
        inv_item.quantity_available = inv_item.quantity_on_hand - inv_item.quantity_allocated

        txn = InventoryTransaction(
            company_id=company_id,
            inventory_item_id=inv_item.id,
            part_id=inv_item.part_id,
            transaction_type=TransactionType.ISSUE,
            quantity=-issue_in.quantity,
            from_location=inv_item.location,
            lot_number=inv_item.lot_number,
            serial_number=inv_item.serial_number,
            # Always None: work-order attribution is refused above (see docstring).
            reference_type=None,
            reference_number=None,
            unit_cost=inv_item.unit_cost,
            total_cost=issue_in.quantity * inv_item.unit_cost,
            notes=issue_in.notes,
            created_by=current_user.id,
        )
        db.add(txn)
        db.flush()
        OperationalEventService(db).emit_best_effort(
            company_id=company_id,
            event_type="inventory_issued",
            source_module="inventory",
            entity_type="inventory_transaction",
            entity_id=txn.id,
            user_id=current_user.id,
            severity="info",
            event_payload={
                "part_id": inv_item.part_id,
                "quantity": issue_in.quantity,
                "location": inv_item.location,
                "lot_number": inv_item.lot_number,
                "work_order_number": issue_in.work_order_number,
            },
        )

        # Tamper-evident audit trail (hash chain) for the stock movement.
        _audit_stock_movement(
            audit,
            txn,
            inv_item,
            old_quantity_on_hand,
            inv_item.quantity_on_hand,
            movement_description=(
                f"Issued {issue_in.quantity} from {inv_item.location}"
                + (f" for work order {issue_in.work_order_number}" if issue_in.work_order_number else "")
            ),
            stock_label="Issue",
        )

    return {"message": "Inventory issued", "quantity": issue_in.quantity}


@router.post("/transfer")
def transfer_inventory(
    transfer_in: TransferRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(STOCK_MUTATOR_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Transfer inventory between locations.

    Role gate matches the **Transfer** row already documented in
    ``docs/RBAC_PERMISSIONS.md`` (Admin / Manager / Supervisor) and the sibling stock
    mutators. Previously this endpoint took only ``get_current_user``, so the server
    under-enforced its own documented policy.
    """
    # FOR UPDATE (no-op on SQLite): both the source decrement and the destination
    # increment below are read-modify-writes racing the consumption engine.
    # Lock ordering caveat: this handler locks source-by-id first, then destination
    # by (part, location, lot) -- the pair is NOT id-ordered relative to each other,
    # so two opposing concurrent transfers of the same part (or a transfer racing the
    # engine's ascending-id lock set) can deadlock. Postgres detects it and aborts one
    # victim with an error (never a hang), and nothing partial commits. A two-phase
    # ascending-id acquisition would close the window if this ever shows up in practice.
    inv_item = (
        db.query(InventoryItem)
        .filter(InventoryItem.id == transfer_in.inventory_item_id, InventoryItem.company_id == company_id)
        .with_for_update()
        .first()
    )
    if not inv_item:
        raise HTTPException(status_code=404, detail="Inventory item not found")

    # Tenant-scoped: another company's location code must behave as "not found",
    # never as a valid transfer destination.
    to_location = (
        db.query(InventoryLocation)
        .filter(InventoryLocation.code == transfer_in.to_location_code, InventoryLocation.company_id == company_id)
        .first()
    )
    if not to_location:
        raise HTTPException(status_code=404, detail="Destination location not found")

    if inv_item.quantity_available < transfer_in.quantity:
        raise HTTPException(status_code=400, detail="Insufficient quantity")

    from_location = inv_item.location
    source_old_quantity = inv_item.quantity_on_hand

    with atomic_transaction(db):
        # Reduce from source
        inv_item.quantity_on_hand -= transfer_in.quantity
        inv_item.quantity_available = inv_item.quantity_on_hand - inv_item.quantity_allocated

        # Add to destination (or create new). Tenant-scoped so the destination row we
        # increment is always this company's stock. Shared helper: NULL-safe when the
        # source row is lot-less (the naive ``lot_number == None`` never matched, so a
        # lot-less transfer always minted a new destination fragment), and row-locked
        # for the increment.
        dest_inv = _find_stock_row(
            db,
            company_id=company_id,
            part_id=inv_item.part_id,
            location_code=transfer_in.to_location_code,
            lot_number=inv_item.lot_number,
            for_update=True,
        )

        dest_old_quantity = dest_inv.quantity_on_hand if dest_inv else None
        if dest_inv:
            dest_inv.quantity_on_hand += transfer_in.quantity
            dest_inv.quantity_available = dest_inv.quantity_on_hand - dest_inv.quantity_allocated
        else:
            dest_inv = InventoryItem(
                part_id=inv_item.part_id,
                location=transfer_in.to_location_code,
                warehouse=to_location.warehouse,
                quantity_on_hand=transfer_in.quantity,
                quantity_available=transfer_in.quantity,
                lot_number=inv_item.lot_number,
                serial_number=inv_item.serial_number,
                unit_cost=inv_item.unit_cost,
                received_date=inv_item.received_date,
            )
            dest_inv.company_id = company_id
            db.add(dest_inv)

        # Transaction record
        txn = InventoryTransaction(
            company_id=company_id,
            inventory_item_id=inv_item.id,
            part_id=inv_item.part_id,
            transaction_type=TransactionType.TRANSFER,
            quantity=transfer_in.quantity,
            from_location=from_location,
            to_location=transfer_in.to_location_code,
            lot_number=inv_item.lot_number,
            notes=transfer_in.notes,
            created_by=current_user.id,
        )
        db.add(txn)
        db.flush()
        OperationalEventService(db).emit_best_effort(
            company_id=company_id,
            event_type="inventory_transferred",
            source_module="inventory",
            entity_type="inventory_transaction",
            entity_id=txn.id,
            user_id=current_user.id,
            severity="info",
            event_payload={
                "part_id": inv_item.part_id,
                "quantity": transfer_in.quantity,
                "from_location": from_location,
                "to_location": transfer_in.to_location_code,
                "lot_number": inv_item.lot_number,
            },
        )

        # Tamper-evident audit trail (hash chain): the movement plus both stock-level
        # changes (source decrement, destination increment). Deliberately NOT routed
        # through ``_audit_stock_movement``: a transfer produces a third row (the
        # destination increment below), and the source row is identified by its
        # ORIGINATING location, not its current one.
        audit.log_create(
            "inventory",
            txn.id,
            str(txn.id),
            new_values=txn,
            description=(f"Transferred {transfer_in.quantity} from {from_location} to {transfer_in.to_location_code}"),
        )
        audit.log_update(
            "inventory",
            inv_item.id,
            f"inventory_item {inv_item.id} @ {from_location}",
            old_values={"quantity_on_hand": source_old_quantity},
            new_values={"quantity_on_hand": inv_item.quantity_on_hand},
            description=f"Transfer out: stock for inventory item {inv_item.id} at {from_location}",
        )
        if dest_old_quantity is not None:
            audit.log_update(
                "inventory",
                dest_inv.id,
                f"inventory_item {dest_inv.id} @ {transfer_in.to_location_code}",
                old_values={"quantity_on_hand": dest_old_quantity},
                new_values={"quantity_on_hand": dest_inv.quantity_on_hand},
                description=(f"Transfer in: stock for inventory item {dest_inv.id} at {transfer_in.to_location_code}"),
            )

    return {"message": "Transfer complete"}


@router.post("/adjust")
def adjust_inventory(
    adjust_in: AdjustmentRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(STOCK_MUTATOR_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Adjust inventory quantity"""
    # FOR UPDATE (no-op on SQLite): the absolute SET below is derived from the read
    # (``variance``), so a concurrent movement between read and write would be lost.
    inv_item = (
        db.query(InventoryItem)
        .filter(InventoryItem.id == adjust_in.inventory_item_id, InventoryItem.company_id == company_id)
        .with_for_update()
        .first()
    )
    if not inv_item:
        raise HTTPException(status_code=404, detail="Inventory item not found")

    old_qty = inv_item.quantity_on_hand
    variance = adjust_in.new_quantity - old_qty

    with atomic_transaction(db):
        inv_item.quantity_on_hand = adjust_in.new_quantity
        inv_item.quantity_available = inv_item.quantity_on_hand - inv_item.quantity_allocated

        txn = InventoryTransaction(
            company_id=company_id,
            inventory_item_id=inv_item.id,
            part_id=inv_item.part_id,
            transaction_type=TransactionType.ADJUST,
            quantity=variance,
            from_location=inv_item.location,
            to_location=inv_item.location,
            lot_number=inv_item.lot_number,
            reason_code=adjust_in.reason_code,
            notes=f"Adjusted from {old_qty} to {adjust_in.new_quantity}. {adjust_in.notes or ''}",
            unit_cost=inv_item.unit_cost,
            total_cost=abs(variance) * inv_item.unit_cost,
            created_by=current_user.id,
        )
        db.add(txn)
        db.flush()
        OperationalEventService(db).emit_best_effort(
            company_id=company_id,
            event_type="inventory_adjusted",
            source_module="inventory",
            entity_type="inventory_transaction",
            entity_id=txn.id,
            user_id=current_user.id,
            severity="medium",
            event_payload={
                "part_id": inv_item.part_id,
                "old_quantity": old_qty,
                "new_quantity": adjust_in.new_quantity,
                "variance": variance,
                "location": inv_item.location,
                "reason_code": adjust_in.reason_code,
            },
        )

        # Tamper-evident audit trail (hash chain): the adjustment movement plus the
        # stock-level change it produced.
        _audit_stock_movement(
            audit,
            txn,
            inv_item,
            old_qty,
            adjust_in.new_quantity,
            movement_description=(
                f"Adjusted inventory item {inv_item.id} at {inv_item.location} "
                f"from {old_qty} to {adjust_in.new_quantity} (reason: {adjust_in.reason_code})"
            ),
            stock_label="Adjust",
        )

    return {"message": "Adjustment complete", "old_quantity": old_qty, "new_quantity": adjust_in.new_quantity}


# Cycle Count endpoints
@router.get("/cycle-counts")
def list_cycle_counts(
    status: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    query = db.query(CycleCount).filter(CycleCount.company_id == company_id).options(joinedload(CycleCount.items))
    if status:
        query = query.filter(CycleCount.status == status)
    return query.order_by(CycleCount.scheduled_date.desc()).all()


@router.post("/cycle-counts")
def create_cycle_count(
    count_in: CycleCountCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(STOCK_MUTATOR_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Create a new cycle count.

    Audited: this is the step that DEFINES the count's scope and enrolls the stock
    rows ``complete`` later adjusts, so "who scoped this count, and which rows did it
    pull in" has to be answerable from the hash chain. ``CycleCount`` and
    ``CycleCountItem`` are both TenantMixin tables, and this is their only writer.
    """
    # Generate count number
    today = datetime.now().strftime("%Y%m%d")
    prefix = f"CC-{today}-"
    last = (
        db.query(CycleCount)
        .filter(CycleCount.count_number.like(f"{prefix}%"), CycleCount.company_id == company_id)
        .order_by(CycleCount.count_number.desc())
        .first()
    )
    num = int(last.count_number.split("-")[-1]) + 1 if last else 1

    count = CycleCount(
        count_number=f"{prefix}{num:03d}",
        warehouse=count_in.warehouse,
        part_id=count_in.part_id,
        scheduled_date=count_in.scheduled_date,
        notes=count_in.notes,
        created_by=current_user.id,
    )
    count.company_id = company_id

    # Get location if specified (tenant-scoped). An unknown code — or one that
    # belongs to another company — is a 404, matching the posture of every other
    # location lookup in this file. Silently ignoring it produced a count whose
    # declared scope did not match the rows it actually enrolled.
    if count_in.location_code:
        loc = (
            db.query(InventoryLocation)
            .filter(InventoryLocation.code == count_in.location_code, InventoryLocation.company_id == company_id)
            .first()
        )
        if not loc:
            raise HTTPException(status_code=404, detail="Location not found")
        count.location_id = loc.id
        count.warehouse = loc.warehouse

    db.add(count)
    db.flush()

    # Add items to count. Tenant-scoped: warehouse / location codes are not unique
    # across companies, so an unscoped scan would enroll another tenant's stock rows
    # into this count (and complete_cycle_count would then adjust them).
    # != 0, not > 0: a lot the shortage posture drove NEGATIVE is exactly the row a
    # cycle count exists to reconcile — enrolling only positive rows made it
    # permanently uncountable.
    query = db.query(InventoryItem).filter(
        InventoryItem.company_id == company_id,
        InventoryItem.is_active == True,
        InventoryItem.quantity_on_hand != 0,
    )

    if count.warehouse:
        query = query.filter(InventoryItem.warehouse == count.warehouse)
    if count_in.location_code:
        query = query.filter(InventoryItem.location == count_in.location_code)
    if count.part_id:
        query = query.filter(InventoryItem.part_id == count.part_id)

    items = query.all()
    for inv in items:
        count_item = CycleCountItem(
            cycle_count_id=count.id,
            inventory_item_id=inv.id,
            system_quantity=inv.quantity_on_hand,
            unit_cost=inv.unit_cost,
        )
        # CycleCountItem is a TenantMixin table (company_id NOT NULL — migration
        # 026). Without this stamp the insert raises IntegrityError and the whole
        # create rolls back, which is exactly what used to happen: enrolling any
        # item always 500'd. No untagged row was ever persisted.
        count_item.company_id = company_id
        db.add(count_item)

    count.total_items = len(items)

    # Tamper-evident audit trail (hash chain) for the enrollment, written before the
    # commit so the audit row lands with the count and its items.
    audit.log_create(
        "cycle_count",
        count.id,
        count.count_number,
        new_values=count,
        description=(
            f"Created cycle count {count.count_number} "
            f"(scope: warehouse={count.warehouse or 'any'}, location={count_in.location_code or 'any'}, "
            f"part_id={count.part_id or 'any'}) enrolling {len(items)} stock row(s)"
        ),
        extra_data={
            "warehouse": count.warehouse,
            "location_code": count_in.location_code,
            "part_id": count.part_id,
            "total_items": len(items),
        },
    )

    db.commit()
    db.refresh(count)

    return count


@router.post("/cycle-counts/{count_id}/start")
def start_cycle_count(
    count_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(COUNT_WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Open a cycle count for counting.

    Gated by EXCLUSION (``COUNT_WRITE_ROLES``): every working role keeps this, and
    only VIEWER — read-only by definition — is refused. Counting is an operator task
    by documented policy (``docs/RBAC_PERMISSIONS.md`` → Inventory): the privileged
    steps are *creating* the count and *completing* it (posting the variance to the
    ledger), both of which stay ``require_role(STOCK_MUTATOR_ROLES)``. Narrowing this
    to the stock-mutator set would hard-block an operator from ever working a
    SCHEDULED count, since ``record_count`` 409s unless the parent is IN_PROGRESS —
    do not do that without owner sign-off. Previously this endpoint took bare
    ``get_current_user``, so VIEWER could open a count and (via ``record_count``)
    write the quantities a manager's ledger-posting adjustment is derived from.

    Refuses a terminal count with 409: re-opening a COMPLETED count would allow a
    second ``complete`` to double-post the same physical variance to the ledger, and
    a CANCELLED count was deliberately abandoned.
    """
    count = db.query(CycleCount).filter(CycleCount.id == count_id, CycleCount.company_id == company_id).first()
    if not count:
        raise HTTPException(status_code=404, detail="Cycle count not found")

    if count.status in TERMINAL_COUNT_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"Cycle count {count.count_number} is {_status_value(count.status)} and cannot be started",
        )

    old_status = _status_value(count.status)
    old_assigned_to = count.assigned_to
    already_started = count.status == CycleCountStatus.IN_PROGRESS

    count.status = CycleCountStatus.IN_PROGRESS
    if not already_started:
        # Only stamp on the real transition — ``started_at`` is the traceability
        # record of when counting began and must survive a re-assignment.
        count.started_at = datetime.utcnow()
    count.assigned_to = current_user.id

    if already_started:
        audit.log_update(
            "cycle_count",
            count.id,
            count.count_number,
            old_values={"assigned_to": old_assigned_to},
            new_values={"assigned_to": count.assigned_to},
            description=f"Reassigned cycle count {count.count_number}",
        )
    else:
        audit.log_status_change(
            "cycle_count",
            count.id,
            count.count_number,
            old_status,
            _status_value(CycleCountStatus.IN_PROGRESS),
            description=f"Started cycle count {count.count_number}",
        )

    db.commit()

    return {"message": "Cycle count started"}


@router.post("/cycle-counts/{count_id}/items/{item_id}/count")
def record_count(
    count_id: int,
    item_id: int,
    count_in: CountItemRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(COUNT_WRITE_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Record a count for an item.

    Gated by EXCLUSION (``COUNT_WRITE_ROLES``), same set as ``start``: the whole
    shop-floor counting path is preserved and only the read-only VIEWER role loses
    write access. The counted quantity is the quality record the manager's
    ledger-posting adjustment is derived from, so writing it is not a read.

    Audited as an UPDATE of the ``cycle_count_item`` row on EVERY write, not only on
    a re-count. The row already exists (``create_cycle_count`` enrolled it), so UPDATE
    is the accurate verb, and a uniform trail means "who counted this, when, and what
    did it replace" is answerable for every count. It matters most on a re-POST while
    the parent is still IN_PROGRESS: that silently overwrites ``counted_quantity`` /
    ``variance`` / ``counted_by``, destroying an evidence value whose only other
    record is the row being overwritten.
    """
    # Tenant-scoped on both the parent count and the item: without it, any
    # authenticated user could write counted quantities onto another company's rows.
    count = db.query(CycleCount).filter(CycleCount.id == count_id, CycleCount.company_id == company_id).first()
    if not count:
        raise HTTPException(status_code=404, detail="Cycle count not found")

    # The counted quantity is the quality record the variance adjustment is derived
    # from. Once the count is closed (COMPLETED / CANCELLED) that record is evidence
    # and must not be overwritten; before it is opened there is nothing to count
    # against. Only an IN_PROGRESS count accepts writes.
    if count.status != CycleCountStatus.IN_PROGRESS:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cycle count {count.count_number} is {_status_value(count.status)}; "
                "counts can only be recorded while it is in progress"
            ),
        )

    item = (
        db.query(CycleCountItem)
        .filter(
            CycleCountItem.id == item_id,
            CycleCountItem.cycle_count_id == count_id,
            CycleCountItem.company_id == company_id,
        )
        .first()
    )

    if not item:
        raise HTTPException(status_code=404, detail="Count item not found")

    was_counted = bool(item.is_counted)
    old_values = {
        "counted_quantity": item.counted_quantity,
        "variance": item.variance,
        "variance_value": item.variance_value,
        "counted_by": item.counted_by,
        "is_counted": was_counted,
        "notes": item.notes,
    }

    item.counted_quantity = count_in.counted_quantity
    item.variance = count_in.counted_quantity - item.system_quantity
    item.variance_value = item.variance * item.unit_cost
    item.is_counted = True
    item.counted_at = datetime.utcnow()
    item.counted_by = current_user.id
    item.notes = count_in.notes

    # Update count progress (``count`` was already resolved tenant-scoped above)
    count.items_counted = (
        db.query(CycleCountItem)
        .filter(
            CycleCountItem.cycle_count_id == count_id,
            CycleCountItem.company_id == company_id,
            CycleCountItem.is_counted == True,
        )
        .count()
    )

    # Tamper-evident audit trail (hash chain) for the counted quantity, written
    # before the commit so it lands with the count item.
    audit.log_update(
        "cycle_count_item",
        item.id,
        f"{count.count_number} item {item.id}",
        old_values=old_values,
        new_values={
            "counted_quantity": item.counted_quantity,
            "variance": item.variance,
            "variance_value": item.variance_value,
            "counted_by": item.counted_by,
            "is_counted": True,
            "notes": item.notes,
        },
        description=(
            f"{'Re-counted' if was_counted else 'Counted'} inventory item {item.inventory_item_id} "
            f"on cycle count {count.count_number}: system {item.system_quantity}, "
            f"counted {item.counted_quantity} (variance {item.variance})"
        ),
        extra_data={"cycle_count_id": count.id, "inventory_item_id": item.inventory_item_id},
    )

    db.commit()

    return {"message": "Count recorded", "variance": item.variance}


@router.post("/cycle-counts/{count_id}/complete")
def complete_cycle_count(
    count_id: int,
    apply_adjustments: bool = True,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_role(STOCK_MUTATOR_ROLES)),
    company_id: int = Depends(get_current_company_id),
    audit: AuditService = Depends(get_audit_service),
):
    """Complete cycle count and optionally apply adjustments.

    Refuses a terminal count with 409. This endpoint posts a COUNT
    ``InventoryTransaction`` per adjusted row; without the guard a double-click
    appends a SECOND ledger row for the same physical variance while writing the
    same on-hand figure, permanently diverging the ledger from stock. The guard is
    check-then-act, so the row is LOCKED first (``_lock_cycle_count``) — otherwise
    two concurrent requests both read IN_PROGRESS and both post.

    ``total_variance_value`` records the variance that was actually **posted** —
    see the note where it is assigned below.
    """
    # Serialize the terminal-state guard against a concurrent double-complete. The
    # guard below is check-then-act: under PostgreSQL READ COMMITTED two overlapping
    # requests would both read IN_PROGRESS, both pass it, and both post a COUNT
    # transaction for the same physical variance — and FastAPI runs these ``def``
    # handlers in a threadpool, so the overlap is real. CycleCount carries no
    # optimistic-lock column, so the row is locked instead.
    #
    # Deliberately a SEPARATE id-only pre-lock rather than chaining onto the load
    # below: that one uses joinedload(CycleCount.items), and PostgreSQL refuses FOR
    # UPDATE across the LEFT OUTER JOIN it emits. The lock is taken before
    # ``atomic_transaction`` (whose commit releases it), so the status a second
    # request reads is this request's committed COMPLETED. As everywhere else in this
    # codebase, with_for_update is a no-op on the SQLite test backend.
    locked = (
        db.query(CycleCount.id)
        .filter(CycleCount.id == count_id, CycleCount.company_id == company_id)
        .with_for_update()
        .first()
    )
    if not locked:
        raise HTTPException(status_code=404, detail="Cycle count not found")

    count = (
        db.query(CycleCount)
        .options(joinedload(CycleCount.items))
        .filter(CycleCount.id == count_id, CycleCount.company_id == company_id)
        .first()
    )
    if not count:
        raise HTTPException(status_code=404, detail="Cycle count not found")

    if count.status in TERMINAL_COUNT_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"Cycle count {count.count_number} is already {_status_value(count.status)}",
        )

    old_status = _status_value(count.status)

    # Two distinct figures, deliberately tracked separately:
    #   measured_variance — what the counters found (every counted item with a
    #                       non-zero variance) priced at the ENROLLMENT-TIME unit cost
    #                       snapshotted on the count item, regardless of posting.
    #   posted_variance   — what actually hit the ledger: only the items that produced
    #                       a COUNT InventoryTransaction, priced on the SAME basis as
    #                       that transaction (the CURRENT InventoryItem.unit_cost and
    #                       the CURRENT-basis quantity delta).
    # They differ whenever apply_adjustments is false, a count item points at a stock
    # row that is gone or belongs to another tenant, the part's unit cost moved
    # between enrollment and completion, OR stock moved between enrollment and
    # completion (routine now that operation completion consumes tied material).
    # Mixing the bases is what used to make ``CycleCount.total_variance_value`` fail
    # to reconcile with the very rows this completion wrote.
    measured_variance = 0.0
    posted_variance = 0.0
    items_adjusted = 0

    with atomic_transaction(db):
        # One tenant-scoped bulk load instead of a SELECT per count item: a
        # warehouse-scoped count enrolls every stock row in the warehouse. Narrowed to
        # the rows this completion could actually adjust (same predicate as the loop),
        # and skipped entirely when nothing will post. Rows belonging to another
        # company are absent from the map, which is what makes the per-item guard
        # below refuse to write through them.
        adjustable_ids = (
            [i.inventory_item_id for i in count.items if i.is_counted and i.variance] if apply_adjustments else []
        )
        # FOR UPDATE (no-op on SQLite): on-hand is read below to compute the
        # current-basis delta, then written absolutely — a concurrent movement (the
        # consumption engine runs on every operation completion) between that read and
        # the write would otherwise be silently lost.
        inventory_by_id = _load_inventory_items(db, company_id, adjustable_ids, for_update=apply_adjustments)

        for item in count.items:
            # A null variance means the row was never really counted; writing
            # ``counted_quantity`` (also null) through to on-hand would corrupt stock.
            if not item.is_counted or not item.variance:
                continue

            measured_variance += item.variance_value or 0.0

            if not apply_adjustments:
                continue

            # Tenant-scoped: a count item must never be able to adjust an inventory
            # row belonging to another company (or one that has since been removed).
            inv = inventory_by_id.get(item.inventory_item_id)
            if not inv:
                continue

            # TWO bases, deliberately kept apart (both are stated on the ledger row):
            #   enrollment basis — counted - system_quantity (the snapshot taken when the
            #                      count was created). That is the QUALITY figure: what
            #                      the counters found vs. what the system said then. It
            #                      stays on ``item.variance`` untouched.
            #   current basis    — counted - on-hand AS OF THIS COMPLETION (read under
            #                      the row lock above). That is what the ledger row must
            #                      carry: the ledger records actual stock movement, and
            #                      any movement between enrollment and completion (now
            #                      routine — operation-completion consumption) makes the
            #                      two differ. Posting the enrollment variance while
            #                      writing on-hand absolutely made SUM(ledger) diverge
            #                      from on-hand permanently and silently resurrected
            #                      consumed stock.
            old_qty = inv.quantity_on_hand
            current_delta = float(item.counted_quantity or 0.0) - float(old_qty or 0.0)
            inv.quantity_on_hand = item.counted_quantity
            inv.quantity_available = inv.quantity_on_hand - inv.quantity_allocated

            if abs(current_delta) <= LEDGER_QUANTITY_EPSILON:
                # On-hand already equals the counted figure (up to the shared ledger
                # epsilon: fractional consumption leaves ~1e-15 float residues that
                # must not post as a COUNT row), so there is no stock movement to
                # record. The count outcome is already on the count item; a
                # zero-quantity ledger row is never posted (existing convention).
                # On-hand was still snapped to the counted figure above.
                continue

            # Create adjustment transaction. company_id is NOT NULL on
            # inventory_transactions (TenantMixin — migration 026), so omitting it
            # raised IntegrityError and rolled the completion back: this path always
            # 500'd whenever it had an adjustment to post. No untagged ledger row
            # was ever persisted.
            txn = InventoryTransaction(
                company_id=company_id,
                inventory_item_id=inv.id,
                part_id=inv.part_id,
                transaction_type=TransactionType.COUNT,
                quantity=current_delta,
                from_location=inv.location,
                to_location=inv.location,
                lot_number=inv.lot_number,
                reason_code="cycle_count",
                notes=(
                    f"Cycle count {count.count_number}. Counted: {item.counted_quantity}; "
                    f"on-hand at completion: {old_qty} (current-basis delta {current_delta:+g}); "
                    f"system at enrollment: {item.system_quantity} "
                    f"(enrollment variance {(item.variance or 0):+g})"
                ),
                unit_cost=inv.unit_cost,
                total_cost=abs(current_delta) * inv.unit_cost,
                created_by=current_user.id,
            )
            db.add(txn)
            db.flush()

            # Priced on the ledger row's OWN basis (current unit cost, current-basis
            # delta), not the enrollment-time snapshot — see the note above.
            posted_variance += current_delta * (inv.unit_cost or 0.0)
            items_adjusted += 1

            # Tamper-evident audit trail (hash chain), same dual-row convention as
            # /inventory/adjust: the movement, plus the stock-level change it made.
            _audit_stock_movement(
                audit,
                txn,
                inv,
                old_qty,
                inv.quantity_on_hand,
                movement_description=(
                    f"Cycle count {count.count_number}: adjusted inventory item {inv.id} at "
                    f"{inv.location} from {old_qty} to {item.counted_quantity} (reason: cycle_count)"
                ),
                stock_label="Cycle count",
            )

        count.status = CycleCountStatus.COMPLETED
        count.completed_at = datetime.utcnow()
        count.completed_by = current_user.id
        count.items_adjusted = items_adjusted
        # Persist the POSTED figure: this column has to reconcile with the COUNT
        # ledger rows this completion wrote. The measured total is not lost — each
        # item keeps its own ``variance_value`` — and it is returned below.
        count.total_variance_value = posted_variance

        audit.log_status_change(
            "cycle_count",
            count.id,
            count.count_number,
            old_status,
            _status_value(CycleCountStatus.COMPLETED),
            description=(
                f"Completed cycle count {count.count_number}: {items_adjusted} item(s) adjusted, "
                f"posted variance value {posted_variance}"
            ),
            extra_data={
                "apply_adjustments": apply_adjustments,
                "items_adjusted": items_adjusted,
                "measured_variance_value": measured_variance,
                "posted_variance_value": posted_variance,
            },
        )

    return {
        "message": "Cycle count completed",
        "items_adjusted": items_adjusted,
        "total_variance_value": posted_variance,
        "measured_variance_value": measured_variance,
    }


# Transaction history
@router.get("/transactions", response_model=List[InventoryTransactionResponse])
def list_transactions(
    part_id: Optional[int] = None,
    transaction_type: Optional[str] = None,
    reference_type: Optional[str] = None,
    reference_id: Optional[int] = None,
    work_order_id: Optional[int] = None,
    lot_number: Optional[str] = None,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    company_id: int = Depends(get_current_company_id),
):
    """Inventory transaction (ledger) history for the active company.

    Server-paged via ``limit``/``offset`` and ordered newest-first, matching the
    offset-paged convention used by ``GET /audit/`` (the frontend ``DataTable``
    ``serverPagination`` contract: request ``pageSize + 1`` rows and infer
    ``hasNext`` from the overflow row — no total-count query against the ledger).

    Typed by ``InventoryTransactionResponse`` (a ``UTCModel``), so ``created_at``
    serializes as UTC ISO-8601 with a trailing ``Z``. This used to return raw ORM
    rows, which dumped the whole joined ``Part`` row and emitted a zone-less
    timestamp. The nested ``part`` object is preserved, narrowed to its identifying
    fields (number / name / description / revision / UoM).

    Note for callers summing ``quantity``: ``transfer`` rows carry a **positive**
    quantity with both ``from_location`` and ``to_location`` and represent zero net
    change in on-hand, so a naive ``SUM(quantity)`` over a filtered set over-counts.
    ``receive`` is positive, ``issue`` is negative, and ``adjust``/``count`` carry the
    signed delta.
    """
    query = (
        db.query(InventoryTransaction)
        .filter(InventoryTransaction.company_id == company_id)
        .options(joinedload(InventoryTransaction.part))
    )

    if part_id:
        query = query.filter(InventoryTransaction.part_id == part_id)
    if transaction_type:
        query = query.filter(InventoryTransaction.transaction_type == transaction_type)
    if reference_type:
        query = query.filter(InventoryTransaction.reference_type == reference_type)
    if reference_id is not None:
        query = query.filter(InventoryTransaction.reference_id == reference_id)
    if lot_number:
        query = query.filter(InventoryTransaction.lot_number == lot_number)
    if start_date:
        query = query.filter(InventoryTransaction.created_at >= start_date)
    if end_date:
        query = query.filter(InventoryTransaction.created_at <= end_date)

    if work_order_id is not None:
        # Convenience filter: "everything this work order consumed/produced".
        #
        # Four shapes exist in the ledger:
        #   1. reference_type='work_order' + reference_id=<wo.id>
        #      (FG receipt; plus LEGACY pre-PR-4.4 one-shot component ISSUE rows)
        #   1b. reference_type='work_order_backflush' + reference_id=<wo.id>
        #      (the reconciling component leg: BOM/routing demand and work-order-scoped
        #       ties, spilling across as many lots as the demand needs)
        #   2. reference_type='work_order_operation' + reference_id=<operation.id>
        #      (per-run consumption of operation-tied material)
        #   3. reference_type='work_order' + reference_number=<wo.work_order_number>,
        #      reference_id NULL                                   (POST /inventory/issue)
        #
        # Shapes 1, 1b and 2 are the SHARED ``work_order_ledger_filter`` — the same predicate
        # job costing, analytics and lot genealogy use, so this list can never disagree
        # with the cost of the job it is listing. Shape 3 has no id at all and stays a
        # local reference_number clause. The work-order number is resolved tenant-scoped;
        # an unknown/other-tenant id simply yields no reference_number clause (and the
        # ledger query is already company-scoped, so nothing can leak).
        #
        # DELIBERATE: no ``is_deleted == False`` filter. This is a traceability/history
        # read, and soft delete is not erasure — the movements a since-voided work order
        # posted are still real ledger facts. Filtering here would silently drop the
        # reference_number-shaped rows from that work order's history.
        wo_number = (
            db.query(WorkOrder.work_order_number)
            .filter(WorkOrder.id == work_order_id, WorkOrder.company_id == company_id)
            .scalar()
        )
        wo_clauses = [work_order_ledger_filter(work_order_id, company_id)]
        if wo_number:
            wo_clauses.append(
                and_(
                    InventoryTransaction.reference_type == WORK_ORDER_REFERENCE_TYPE,
                    InventoryTransaction.reference_number == wo_number,
                )
            )
        query = query.filter(or_(*wo_clauses))

    return (
        query.order_by(InventoryTransaction.created_at.desc(), InventoryTransaction.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
