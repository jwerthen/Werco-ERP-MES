"""Open work-order / open purchase-order migration imports (A0.2).

These are the go-live "in-flight paperwork" loaders: the shop is migrating off
Excel, so day-1 floor queues and the open-PO book must be seeded from
spreadsheets. Both importers:

* consume a :class:`~app.services.import_service.ParsedTable` (CSV/XLSX already
  normalized by ``parse_import_file``),
* run every row inside a SAVEPOINT — ``dry_run`` rolls the savepoint back
  (guaranteed zero writes, including audit rows), commit mode commits row by
  row so one bad row never poisons the rest (same partial-success contract as
  the existing master-data CSV imports),
* create records through the SAME paths hand-entered records use (work-order
  routing-operation generation, number generators, audit chain, operational
  events), so an imported record is indistinguishable from a hand-entered one
  in tenancy and audit completeness.

Provenance decision for ``completed_through_seq`` (operations already finished
on paper before migration): the operation is marked ``COMPLETE`` with its
target quantity, but we deliberately do NOT fabricate ``actual_start`` /
``actual_end`` timestamps, ``started_by``/``completed_by`` operators, or
TimeEntry labor rows — none of that evidence exists in this system and
inventing it would corrupt cycle-time/labor analytics and the AS9100D story.
Instead each paper-completed operation emits an ``operation_completed``
OperationalEvent with ``source="import"`` (the A0.1 adoption-telemetry
channel) and the work order's audit rows record exactly which sequences were
seeded as paper-complete.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime
from typing import Dict, List, Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.db.tenant_filter import tenant_query
from app.models.customer import Customer
from app.models.part import Part
from app.models.purchasing import POStatus, PurchaseOrder, PurchaseOrderLine, Vendor
from app.models.user import User
from app.models.work_order import (
    OperationStatus,
    WorkOrder,
    WorkOrderOperation,
    WorkOrderStatus,
)
from app.schemas.import_kit import (
    PurchaseOrderImportError,
    PurchaseOrderImportResponse,
    PurchaseOrderImportRowResult,
    WorkOrderImportError,
    WorkOrderImportResponse,
    WorkOrderImportRowResult,
)
from app.services.audit_service import AuditService
from app.services.completion_signal_service import emit_operation_completed_event
from app.services.import_service import ParsedTable, parse_date_field
from app.services.operational_event_service import OperationalEventService
from app.services.work_order_state_service import (
    operation_target_quantity,
    sync_work_order_quantity_complete,
)

logger = logging.getLogger(__name__)

IMPORT_SOURCE = "import"


def _required(row: Dict[str, str], key: str) -> str:
    value = (row.get(key) or "").strip()
    if not value:
        raise ValueError(f"{key} is required")
    return value


def _parse_positive_float(value: str, field_name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a number") from exc
    if parsed <= 0:
        raise ValueError(f"{field_name} must be greater than 0")
    return parsed


def _parse_non_negative_float(value: str, field_name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a number") from exc
    if parsed < 0:
        raise ValueError(f"{field_name} must be zero or greater")
    return parsed


def _parse_optional_int(value: str, field_name: str) -> Optional[int]:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return int(float(text))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a whole number") from exc


def _expunge_rolled_back(db: Session, instances: List[object]) -> None:
    """Drop savepoint-rolled-back instances from the session identity map.

    On SQLite (tests) rowids are reused after a rollback, so a stale identity
    entry for a rolled-back row collides with the next row's flush (SAWarning
    "identity map already had an identity"). Postgres sequences never reuse
    ids, but expunging is correct on both. No-op for already-detached objects.
    """
    for instance in instances:
        if instance in db:
            db.expunge(instance)


def _find_part(db: Session, company_id: int, part_number: str) -> Optional[Part]:
    """Tenant-scoped, soft-delete-respecting part lookup by part number."""
    return (
        tenant_query(db, Part, company_id)
        .filter(
            func.upper(Part.part_number) == part_number.upper(),
            Part.is_deleted == False,  # noqa: E712
        )
        .first()
    )


# ---------------------------------------------------------------------------
# Open work orders
# ---------------------------------------------------------------------------


@dataclass
class _WorkOrderRow:
    row_number: int
    wo_number: Optional[str]
    part_number: str
    quantity: float
    due_date: Optional[date]
    customer: Optional[str]
    customer_po: Optional[str]
    priority: int
    completed_through_seq: Optional[int]


def _parse_work_order_row(row_number: int, row: Dict[str, str]) -> _WorkOrderRow:
    priority = _parse_optional_int(row.get("priority", ""), "priority")
    if priority is None:
        priority = 5
    if not 1 <= priority <= 10:
        raise ValueError("priority must be between 1 and 10")

    completed_through_seq = _parse_optional_int(row.get("completed_through_seq", ""), "completed_through_seq")
    if completed_through_seq is not None and completed_through_seq <= 0:
        completed_through_seq = None

    # NOTE: unlike the interactive WorkOrderCreate schema, PAST due dates are
    # accepted here on purpose — migrating shops have genuinely overdue open
    # work orders and rejecting them would block go-live.
    return _WorkOrderRow(
        row_number=row_number,
        wo_number=(row.get("wo_number") or "").strip() or None,
        part_number=_required(row, "part_number"),
        quantity=_parse_positive_float(_required(row, "quantity"), "quantity"),
        due_date=parse_date_field(row.get("due_date", ""), "due_date"),
        customer=(row.get("customer") or row.get("customer_name") or row.get("customer_code") or "").strip() or None,
        customer_po=(row.get("customer_po") or "").strip() or None,
        priority=priority,
        completed_through_seq=completed_through_seq,
    )


def _resolve_customer_name(db: Session, company_id: int, customer_value: str) -> str:
    """Match a customer by code or name (tenant-scoped, excluding soft-deleted)."""
    needle = customer_value.strip().lower()
    customer = (
        tenant_query(db, Customer, company_id)
        .filter(Customer.is_deleted == False)  # noqa: E712
        .filter(or_(func.lower(Customer.code) == needle, func.lower(Customer.name) == needle))
        .first()
    )
    if not customer:
        raise ValueError(f"customer '{customer_value}' not found (import customers first, or leave the column blank)")
    return customer.name


def import_open_work_orders(
    db: Session,
    *,
    table: ParsedTable,
    current_user: User,
    company_id: int,
    audit: AuditService,
    dry_run: bool,
) -> WorkOrderImportResponse:
    """Create open work orders (with routed operations) from a parsed upload."""
    # Lazy import: the routing-operation generator lives with the work-orders
    # router (which itself imports services), so a module-level import here
    # would be circular. Same precedent as materials.py reusing parts.py helpers.
    from app.api.endpoints.work_orders import (
        create_routing_operations_for_work_order,
        generate_work_order_number,
    )

    errors: List[WorkOrderImportError] = []
    results: List[WorkOrderImportRowResult] = []
    created_ids: List[int] = []
    total_rows = 0
    seen_numbers: set[str] = set()

    for row_number, row in table.iter_rows():
        total_rows += 1
        try:
            parsed = _parse_work_order_row(row_number, row)
        except ValueError as exc:
            errors.append(
                WorkOrderImportError(
                    row=row_number,
                    wo_number=(row.get("wo_number") or "").strip() or None,
                    part_number=(row.get("part_number") or "").strip() or None,
                    reason=str(exc),
                )
            )
            continue

        nested = db.begin_nested()
        row_instances: List[object] = []
        try:
            part = _find_part(db, company_id, parsed.part_number)
            if not part:
                raise ValueError(f"part '{parsed.part_number}' not found")

            customer_name = _resolve_customer_name(db, company_id, parsed.customer) if parsed.customer else None

            if parsed.wo_number:
                number_key = parsed.wo_number.upper()
                if number_key in seen_numbers:
                    raise ValueError(f"wo_number '{parsed.wo_number}' appears more than once in this file")
                exists = (
                    tenant_query(db, WorkOrder, company_id)
                    .filter(WorkOrder.work_order_number == parsed.wo_number)
                    .first()
                )
                if exists:
                    raise ValueError(f"wo_number '{parsed.wo_number}' already exists")
                wo_number = parsed.wo_number
            elif dry_run:
                # Numbers are only reserved at commit; a rolled-back preview must
                # not promise one. Placeholder keeps the NOT NULL/unique columns
                # happy inside the savepoint and is reported as None below.
                wo_number = f"WO-PREVIEW-{row_number}"
            else:
                wo_number = generate_work_order_number(db, company_id)

            work_order = WorkOrder(
                work_order_number=wo_number,
                part_id=part.id,
                quantity_ordered=parsed.quantity,
                priority=parsed.priority,
                due_date=parsed.due_date,
                customer_name=customer_name,
                customer_po=parsed.customer_po,
                status=WorkOrderStatus.DRAFT,
                created_by=current_user.id,
            )
            work_order.company_id = company_id
            db.add(work_order)
            db.flush()
            row_instances.append(work_order)

            # SAME operation generation as POST /work-orders (released routing,
            # assembly-aware) — never raw operation inserts.
            create_routing_operations_for_work_order(db, work_order, part, parsed.quantity, company_id)
            db.flush()
            operations = (
                tenant_query(db, WorkOrderOperation, company_id)
                .filter(WorkOrderOperation.work_order_id == work_order.id)
                .order_by(WorkOrderOperation.sequence)
                .all()
            )
            row_instances.extend(operations)
            if not operations:
                raise ValueError(
                    f"part '{part.part_number}' has no released routing — import/release the routing first"
                )

            completed_ops: List[WorkOrderOperation] = []
            if parsed.completed_through_seq is not None:
                completed_ops = [op for op in operations if op.sequence <= parsed.completed_through_seq]
                if len(completed_ops) == len(operations):
                    raise ValueError(
                        "completed_through_seq covers every routing operation; " "only OPEN work orders can be imported"
                    )

            for op in completed_ops:
                # Paper-completed before migration: COMPLETE + target quantity,
                # but NO fabricated timestamps/operators/TimeEntries (see module
                # docstring for the provenance decision).
                op.status = OperationStatus.COMPLETE
                op.quantity_complete = operation_target_quantity(op, work_order)
                sync_work_order_quantity_complete(work_order, op, all_operations_complete=False)

            # Release so the WO shows up in floor queues: same state the
            # /release endpoint produces (released_by/released_at + first
            # pending op promoted to READY).
            work_order.status = WorkOrderStatus.IN_PROGRESS if completed_ops else WorkOrderStatus.RELEASED
            work_order.released_by = current_user.id
            work_order.released_at = datetime.utcnow()
            next_op = next((op for op in operations if op.status == OperationStatus.PENDING), None)
            if next_op:
                next_op.status = OperationStatus.READY
            work_order.current_operation_id = next(
                (op.id for op in operations if op.status == OperationStatus.READY),
                next((op.id for op in operations if op.status != OperationStatus.COMPLETE), None),
            )
            db.flush()

            status_value = work_order.status.value
            result = WorkOrderImportRowResult(
                row=row_number,
                wo_number=None if (dry_run and not parsed.wo_number) else wo_number,
                part_number=part.part_number,
                quantity=parsed.quantity,
                due_date=parsed.due_date,
                customer_name=customer_name,
                status=status_value,
                operation_count=len(operations),
                completed_operation_count=len(completed_ops),
                next_operation_sequence=next_op.sequence if next_op else None,
            )

            if dry_run:
                nested.rollback()
                _expunge_rolled_back(db, row_instances)
            else:
                audit.log_create(
                    resource_type="work_order",
                    resource_id=work_order.id,
                    resource_identifier=work_order.work_order_number,
                    new_values=work_order,
                    description=(
                        f"Imported open work order {work_order.work_order_number} from Excel migration upload"
                    ),
                    extra_data={
                        "source": IMPORT_SOURCE,
                        "part_number": part.part_number,
                        "quantity": parsed.quantity,
                        "operation_count": len(operations),
                        "completed_through_seq": parsed.completed_through_seq,
                        "paper_completed_sequences": [op.sequence for op in completed_ops],
                    },
                )
                audit.log_status_change(
                    resource_type="work_order",
                    resource_id=work_order.id,
                    resource_identifier=work_order.work_order_number,
                    old_status=WorkOrderStatus.DRAFT.value,
                    new_status=status_value,
                    description=(
                        f"Released on import: {len(completed_ops)} operation(s) were already complete on paper "
                        "before migration (no labor evidence fabricated)"
                    ),
                    extra_data={"source": IMPORT_SOURCE},
                )
                for op in completed_ops:
                    emit_operation_completed_event(
                        db,
                        company_id=company_id,
                        work_order=work_order,
                        operation=op,
                        user_id=current_user.id,
                        source_module="import",
                        source=IMPORT_SOURCE,
                    )
                nested.commit()
                db.commit()
                created_ids.append(work_order.id)

            if parsed.wo_number:
                seen_numbers.add(parsed.wo_number.upper())
            results.append(result)
        except ValueError as exc:
            nested.rollback()
            _expunge_rolled_back(db, row_instances)
            errors.append(
                WorkOrderImportError(
                    row=row_number,
                    wo_number=parsed.wo_number,
                    part_number=parsed.part_number,
                    reason=str(exc),
                )
            )
        except Exception:
            nested.rollback()
            _expunge_rolled_back(db, row_instances)
            logger.exception("open work order import failed on row %s", row_number)
            errors.append(
                WorkOrderImportError(
                    row=row_number,
                    wo_number=parsed.wo_number,
                    part_number=parsed.part_number,
                    reason="Failed to create work order due to a database error",
                )
            )

    if dry_run:
        # Belt and braces: nothing from a preview may ever reach the database.
        db.rollback()

    return WorkOrderImportResponse(
        dry_run=dry_run,
        total_rows=total_rows,
        created_count=len(results),
        skipped_count=total_rows - len(results),
        created_ids=created_ids,
        results=results,
        errors=errors,
    )


# ---------------------------------------------------------------------------
# Open purchase orders
# ---------------------------------------------------------------------------


@dataclass
class _PurchaseOrderLineRow:
    row_number: int
    po_number: Optional[str]
    vendor_code: str
    part_number: str
    quantity: float
    unit_price: float
    promised_date: Optional[date]


def _parse_purchase_order_row(row_number: int, row: Dict[str, str]) -> _PurchaseOrderLineRow:
    return _PurchaseOrderLineRow(
        row_number=row_number,
        po_number=(row.get("po_number") or "").strip() or None,
        vendor_code=_required(row, "vendor_code"),
        part_number=_required(row, "part_number"),
        quantity=_parse_positive_float(_required(row, "quantity"), "quantity"),
        unit_price=_parse_non_negative_float(_required(row, "unit_price"), "unit_price"),
        promised_date=parse_date_field(row.get("promised_date", ""), "promised_date"),
    )


def import_open_purchase_orders(
    db: Session,
    *,
    table: ParsedTable,
    current_user: User,
    company_id: int,
    audit: AuditService,
    dry_run: bool,
) -> PurchaseOrderImportResponse:
    """Create open (issued) purchase orders from a parsed upload.

    Rows sharing a ``po_number`` become lines of one PO; a blank ``po_number``
    makes a single-line PO with a generated number. POs land in ``SENT``
    (issued) status so receiving can act on them. ``order_date`` is left NULL
    on purpose — the real order date predates this system and is unknown, and
    we do not fabricate provenance (mirrors the work-order timestamp decision).
    """
    from app.api.endpoints.purchasing import generate_po_number  # lazy: avoids router<->service cycle

    errors: List[PurchaseOrderImportError] = []
    results: List[PurchaseOrderImportRowResult] = []
    created_ids: List[int] = []
    total_rows = 0
    created_line_count = 0

    # Parse every row first, then group rows into POs by po_number.
    groups: Dict[str, List[_PurchaseOrderLineRow]] = {}
    group_order: List[str] = []
    failed_groups: Dict[str, int] = {}  # group key -> row that broke it

    for row_number, row in table.iter_rows():
        total_rows += 1
        po_number = (row.get("po_number") or "").strip()
        key = f"po:{po_number.upper()}" if po_number else f"row:{row_number}"
        if key not in groups:
            groups[key] = []
            group_order.append(key)
        try:
            groups[key].append(_parse_purchase_order_row(row_number, row))
        except ValueError as exc:
            failed_groups.setdefault(key, row_number)
            errors.append(
                PurchaseOrderImportError(
                    row=row_number,
                    po_number=po_number or None,
                    part_number=(row.get("part_number") or "").strip() or None,
                    reason=str(exc),
                )
            )

    seen_po_numbers: set[str] = set()

    for key in group_order:
        lines = groups[key]
        po_number = next((line.po_number for line in lines if line.po_number), None)

        if key in failed_groups:
            # A PO must import whole: skip sibling lines of a failed group.
            for line in lines:
                errors.append(
                    PurchaseOrderImportError(
                        row=line.row_number,
                        po_number=po_number,
                        part_number=line.part_number,
                        reason=f"skipped: row {failed_groups[key]} in the same purchase order failed validation",
                    )
                )
            continue

        nested = db.begin_nested()
        row_instances: List[object] = []
        try:
            vendor_codes = {line.vendor_code.upper() for line in lines}
            if len(vendor_codes) > 1:
                raise ValueError(f"purchase order '{po_number}' has conflicting vendor codes: {sorted(vendor_codes)}")
            vendor_code = lines[0].vendor_code
            vendor = tenant_query(db, Vendor, company_id).filter(func.upper(Vendor.code) == vendor_code.upper()).first()
            if not vendor:
                raise ValueError(f"vendor '{vendor_code}' not found (import vendors first)")

            if po_number:
                if po_number.upper() in seen_po_numbers:
                    raise ValueError(f"po_number '{po_number}' was already imported earlier in this file")
                exists = (
                    tenant_query(db, PurchaseOrder, company_id).filter(PurchaseOrder.po_number == po_number).first()
                )
                if exists:
                    raise ValueError(f"po_number '{po_number}' already exists")
                final_number = po_number
            elif dry_run:
                final_number = f"PO-PREVIEW-{lines[0].row_number}"
            else:
                final_number = generate_po_number(db, company_id)

            promised_dates = [line.promised_date for line in lines if line.promised_date]
            po = PurchaseOrder(
                po_number=final_number,
                vendor_id=vendor.id,
                status=POStatus.SENT,  # issued/open: receivable on day 1
                expected_date=max(promised_dates) if promised_dates else None,
                created_by=current_user.id,
            )
            po.company_id = company_id
            db.add(po)
            db.flush()
            row_instances.append(po)

            subtotal = 0.0
            for idx, line in enumerate(lines, 1):
                part = _find_part(db, company_id, line.part_number)
                if not part:
                    raise ValueError(f"part '{line.part_number}' not found (row {line.row_number})")
                line_total = line.quantity * line.unit_price
                po_line = PurchaseOrderLine(
                    purchase_order_id=po.id,
                    line_number=idx,
                    part_id=part.id,
                    quantity_ordered=line.quantity,
                    unit_price=line.unit_price,
                    line_total=line_total,
                    required_date=line.promised_date,
                )
                po_line.company_id = company_id
                db.add(po_line)
                row_instances.append(po_line)
                subtotal += line_total
            po.subtotal = subtotal
            po.total = subtotal + float(po.tax or 0) + float(po.shipping or 0)
            db.flush()

            result = PurchaseOrderImportRowResult(
                rows=[line.row_number for line in lines],
                po_number=None if (dry_run and not po_number) else final_number,
                vendor_code=vendor.code,
                line_count=len(lines),
                total=float(po.total or 0),
                status=POStatus.SENT.value,
            )

            if dry_run:
                nested.rollback()
                _expunge_rolled_back(db, row_instances)
            else:
                audit.log_create(
                    resource_type="purchase_order",
                    resource_id=po.id,
                    resource_identifier=po.po_number,
                    new_values=po,
                    description=f"Imported open purchase order {po.po_number} from Excel migration upload",
                    extra_data={
                        "source": IMPORT_SOURCE,
                        "vendor_code": vendor.code,
                        "line_count": len(lines),
                        "status": POStatus.SENT.value,
                    },
                )
                # Same operational event the interactive create emits, tagged
                # with the import source channel.
                OperationalEventService(db).emit(
                    company_id=company_id,
                    event_type="purchase_order_created",
                    source_module="import",
                    entity_type="purchase_order",
                    entity_id=po.id,
                    user_id=current_user.id,
                    severity="info",
                    event_payload={
                        "po_number": po.po_number,
                        "vendor_id": vendor.id,
                        "vendor_name": vendor.name,
                        "line_count": len(lines),
                        "required_date": None,
                        "total": float(po.total or 0),
                        "source": IMPORT_SOURCE,
                    },
                )
                nested.commit()
                db.commit()
                created_ids.append(po.id)

            if po_number:
                seen_po_numbers.add(po_number.upper())
            created_line_count += len(lines)
            results.append(result)
        except ValueError as exc:
            nested.rollback()
            _expunge_rolled_back(db, row_instances)
            for line in lines:
                errors.append(
                    PurchaseOrderImportError(
                        row=line.row_number,
                        po_number=po_number,
                        part_number=line.part_number,
                        reason=str(exc),
                    )
                )
        except Exception:
            nested.rollback()
            _expunge_rolled_back(db, row_instances)
            logger.exception("open purchase order import failed for group %s", key)
            for line in lines:
                errors.append(
                    PurchaseOrderImportError(
                        row=line.row_number,
                        po_number=po_number,
                        part_number=line.part_number,
                        reason="Failed to create purchase order due to a database error",
                    )
                )

    if dry_run:
        db.rollback()

    line_rows_imported = created_line_count
    return PurchaseOrderImportResponse(
        dry_run=dry_run,
        total_rows=total_rows,
        created_count=len(results),
        created_line_count=created_line_count,
        skipped_count=total_rows - line_rows_imported,
        created_ids=created_ids,
        results=results,
        errors=errors,
    )
