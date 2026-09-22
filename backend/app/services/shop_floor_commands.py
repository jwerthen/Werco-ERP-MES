"""Canonical commit-free production and hold commands shared by ERP and Hank."""

import math
from datetime import date, datetime
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import and_
from sqlalchemy.orm import joinedload

from app.core.time_utils import to_utc_iso
from app.models.quality import NCRSource, NonConformanceReport
from app.models.time_entry import TimeEntry, TimeEntrySource, TimeEntryType
from app.models.user import User
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation
from app.models.work_order_blocker import WorkOrderBlockerCategory
from app.schemas.work_order_blocker import WorkOrderBlockerCreate
from app.services import dispatch_service
from app.services.laser_nest_service import sync_laser_nest_from_operation
from app.services.operational_event_service import OperationalEventService
from app.services.production_receipt_service import find_production_replay, record_production_receipt
from app.services.scrap_reason_service import resolve_scrap_reason_code_or_http
from app.services.work_order_blocker_service import WorkOrderBlockerService
from app.services.work_order_state_service import (
    floor_operation_quantity_at_evidence,
    operation_target_quantity,
    sync_work_order_quantity_complete,
)

_FORBIDDEN_CLIENT_LABOR_SOURCES = frozenset({TimeEntrySource.IMPORT})


def _resolve_labor_source(current_user: User, client_source: Optional[TimeEntrySource]) -> Optional[str]:
    """Resolve the adoption-telemetry channel to record on a labor write.

    Trust model (labor twin of ``_record_source`` for process-sheet records):

    * A kiosk-scoped credential (a badge-minted crew-station operator token,
      ``_token_scope == "kiosk"``) is AUTHORITATIVE -- it always records KIOSK
      regardless of any client hint, so a crew station can never be tricked into
      stamping ``backfill``/``import`` (or anything else) onto its labor.
    * Otherwise the client's declared channel is stored verbatim, EXCEPT the
      loader-reserved channels (``IMPORT``), which a normal request may never
      claim -- rejected with HTTP 422 so the write mutates nothing.
    * Omitted -> ``None`` (NULL): the server never guesses a channel.
    """
    if getattr(current_user, "_token_scope", None) == "kiosk":
        return TimeEntrySource.KIOSK.value
    if client_source in _FORBIDDEN_CLIENT_LABOR_SOURCES:
        raise HTTPException(
            status_code=422,
            detail=(
                f"source '{client_source.value}' cannot be set on an interactive labor entry; "
                "it is reserved for the bulk-import loaders"
            ),
        )
    return client_source.value if client_source else None


def report_production_command(db, current_user, company_id, operation_id, production_data, audit_service):
    """Apply canonical production evidence without commit or realtime I/O."""
    replay = find_production_replay(db, company_id, current_user.id, operation_id, production_data)
    if replay is not None:
        return replay

    operation = (
        db.query(WorkOrderOperation)
        .options(joinedload(WorkOrderOperation.work_order).joinedload(WorkOrder.part))
        .filter(
            WorkOrderOperation.id == operation_id,
            WorkOrderOperation.company_id == company_id,
        )
        .first()
    )

    if not operation:
        raise HTTPException(status_code=404, detail="Operation not found")

    work_order = operation.work_order
    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found for this operation")

    if operation.status != OperationStatus.IN_PROGRESS:
        raise HTTPException(status_code=400, detail="Operation must be in progress to add completed quantity")

    active_entry = (
        db.query(TimeEntry)
        .filter(
            and_(
                TimeEntry.user_id == current_user.id,
                TimeEntry.operation_id == operation_id,
                TimeEntry.clock_out.is_(None),
                TimeEntry.company_id == company_id,
            )
        )
        .first()
    )
    if not active_entry:
        raise HTTPException(status_code=400, detail="You must be clocked in to add completed quantity")

    good_delta = production_data.quantity_complete_delta
    scrap_delta = production_data.quantity_scrapped_delta
    if math.isnan(good_delta) or math.isinf(good_delta) or math.isnan(scrap_delta) or math.isinf(scrap_delta):
        raise HTTPException(status_code=400, detail="Quantity must be a valid number")
    if good_delta < 0 or scrap_delta < 0:
        raise HTTPException(status_code=400, detail="Quantity cannot be negative")
    if good_delta == 0 and scrap_delta == 0:
        raise HTTPException(status_code=400, detail="Enter a completed or scrap quantity")
    # Scrap -> NCR (Kiosk Foundry redesign): an NCR documents scrap, so filing one
    # from a report that carries none is a client bug -- refuse before any mutation.
    if production_data.open_ncr and scrap_delta <= 0:
        raise HTTPException(
            status_code=400,
            detail="open_ncr requires a scrap quantity: report quantity_scrapped_delta greater than 0",
        )

    # A0.1 adoption-telemetry channel (kiosk-token forcing + import guard) resolved
    # before any mutation so a disallowed 'import' 422s without touching the entry.
    recorded_source = _resolve_labor_source(current_user, production_data.source)

    # Lean Phase 1: resolve the structured scrap reason code BEFORE any mutation
    # (404 unknown/cross-tenant, 422 inactive). None passes through untouched.
    scrap_code = resolve_scrap_reason_code_or_http(db, company_id, production_data.scrap_reason_code_id)

    # SFI-1: lock the operation row before the over-completion read-modify-write
    # so concurrent producers serialize on quantity_complete instead of losing
    # updates. Re-read the freshest committed quantity off the locked row rather
    # than reusing the stale in-session value for the guard.
    operation = (
        db.query(WorkOrderOperation)
        .filter(
            WorkOrderOperation.id == operation_id,
            WorkOrderOperation.company_id == company_id,
        )
        .with_for_update()
        .first()
    )
    if not operation:
        raise HTTPException(status_code=404, detail="Operation not found")

    # SFI-1: ALSO re-fetch the parent WO under a row lock (consistent lock order:
    # OPERATION first, then WORK ORDER -- same as complete_operation). The rollup
    # write below sets work_order.quantity_complete; two producers on different
    # operations of the same WO would otherwise race last-writer-wins on that
    # column. Locking the parent serializes the rollup. Tenant-scoped and
    # soft-delete-aware, against the freshest committed row.
    work_order = (
        db.query(WorkOrder)
        .filter(
            WorkOrder.id == operation.work_order_id,
            WorkOrder.company_id == company_id,
            WorkOrder.is_deleted == False,  # noqa: E712
        )
        .with_for_update()
        .first()
    )
    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found for this operation")

    target_qty = operation_target_quantity(operation, work_order)
    next_complete_qty = float(operation.quantity_complete or 0) + good_delta
    if target_qty > 0 and next_complete_qty > target_qty:
        raise HTTPException(
            status_code=400, detail=f"Quantity ({next_complete_qty}) cannot exceed quantity ordered ({target_qty})"
        )

    # /production is an ADDITIVE verb: floor the incremented total at durable
    # TimeEntry evidence and cap at target so additive and absolute writes converge
    # on the same invariant (DUP-3 / SFI-5). The over-completion guard above already
    # rejected a delta that would exceed target.
    operation.quantity_complete = floor_operation_quantity_at_evidence(db, operation, next_complete_qty, target_qty)
    operation.quantity_scrapped = float(operation.quantity_scrapped or 0) + scrap_delta
    # Lean Phase 1: categorize the operation's scrap when THIS report carries both
    # scrap and a code (a code-less report never clears a recorded one).
    if scrap_code is not None and scrap_delta > 0:
        operation.scrap_reason_code_id = scrap_code.id
    # Lean Phase 1 (FPY): produced quantity reported while clocked into a REWORK
    # entry is re-processed work -- track it for first-pass yield.
    if active_entry.entry_type == TimeEntryType.REWORK and good_delta > 0:
        operation.quantity_reworked = float(operation.quantity_reworked or 0) + good_delta
    # Kiosk telemetry (LAST REPORT tile): stamp THIS report as the operation's most
    # recent production evidence -- the deltas of this single report, not totals.
    # Always at least one of the two is > 0 (both-zero was refused above).
    operation.last_reported_at = datetime.utcnow()
    operation.last_reported_good = good_delta
    operation.last_reported_scrapped = scrap_delta
    operation.updated_at = datetime.utcnow()
    sync_laser_nest_from_operation(operation)

    active_entry.quantity_produced = float(active_entry.quantity_produced or 0) + good_delta
    active_entry.quantity_scrapped = float(active_entry.quantity_scrapped or 0) + scrap_delta
    if production_data.notes:
        active_entry.notes = (
            f"{active_entry.notes}\n{production_data.notes}" if active_entry.notes else production_data.notes
        )
    # A0.3: structured scrap reason -- persisted onto the active entry like clock-out's,
    # but only when this report actually carries scrap; an omitted/None reason never
    # clobbers a reason recorded by an earlier in-shift report.
    scrap_reason = production_data.scrap_reason if (production_data.scrap_reason and scrap_delta > 0) else None
    if scrap_reason:
        active_entry.scrap_reason = scrap_reason
    # Lean Phase 1: same semantics for the structured code.
    if scrap_code is not None and scrap_delta > 0:
        active_entry.scrap_reason_code_id = scrap_code.id
    # A0.1 adoption telemetry: record the reporting channel when this write carries one
    # (a kiosk-scoped token always resolves to KIOSK); omitted on a normal session ->
    # keep whatever channel the entry already carries (never guessed).
    if recorded_source:
        active_entry.source = recorded_source
    active_entry.updated_at = datetime.utcnow()

    sync_work_order_quantity_complete(work_order, operation, all_operations_complete=False)
    work_order.updated_at = datetime.utcnow()

    audit_service.log(
        action="REPORT_OPERATION_PRODUCTION",
        resource_type="work_order_operation",
        resource_id=operation_id,
        description=(
            f"Reported production on operation {operation.operation_number} for WO {work_order.work_order_number}. "
            f"Added good: {good_delta}, scrap: {scrap_delta}. "
            f"Qty: {operation.quantity_complete}/{target_qty}"
            + (f". Scrap reason: {scrap_reason}" if scrap_reason else "")
            + (f". Scrap reason code: {scrap_code.code}" if (scrap_code and scrap_delta > 0) else "")
            + (f". Notes: {production_data.notes}" if production_data.notes else "")
        ),
    )

    # Scrap -> NCR (Kiosk Foundry redesign): file the NCR in the SAME transaction
    # as the production write, following the create_quality_hold pattern (number
    # generation, audit log_create, ncr_created event) -- but deliberately with NO
    # blocker and NO hold: the machine keeps running, Quality is notified through
    # the NCR + high-severity operational event.
    ncr_payload: Optional[dict] = None
    if production_data.open_ncr:
        # Service->endpoint edge kept function-local on purpose (create_quality_hold
        # precedent): quality.py owns the canonical company-scoped NCR number
        # generator and importing it beats a third copy drifting.
        from app.services.quality_numbering import generate_ncr_number

        scrap_reason_text = (production_data.scrap_reason or "").strip()
        if not scrap_reason_text and scrap_code is not None:
            scrap_reason_text = f"{scrap_code.code} — {scrap_code.name}"
        ncr_description = (production_data.ncr_description or "").strip() or (
            f"Operator scrap report on WO {work_order.work_order_number} "
            f"op {operation.operation_number}: {scrap_delta} scrapped. Reason: {scrap_reason_text}"
        )
        ncr = NonConformanceReport(
            ncr_number=generate_ncr_number(db, company_id),
            part_id=work_order.part_id,
            work_order_id=work_order.id,
            lot_number=work_order.lot_number,
            quantity_affected=scrap_delta,
            source=NCRSource.IN_PROCESS,
            title=(f"Operator scrap report — WO {work_order.work_order_number} op {operation.operation_number}")[:255],
            description=ncr_description,
            detected_by=current_user.id,
            detected_date=date.today(),
        )
        ncr.company_id = company_id
        db.add(ncr)
        db.flush()
        audit_service.log_create(
            "ncr",
            ncr.id,
            ncr.ncr_number,
            new_values=ncr,
            description=(
                f"NCR {ncr.ncr_number} filed from an operator scrap report on "
                f"WO {work_order.work_order_number} op {operation.operation_number}"
            ),
            extra_data={
                "work_order_id": work_order.id,
                "work_order_operation_id": operation.id,
                "quantity_scrapped_delta": scrap_delta,
                "scrap_reason": scrap_reason,
                "scrap_reason_code": scrap_code.code if scrap_code else None,
                "source": recorded_source,
            },
        )
        OperationalEventService(db).emit_best_effort(
            company_id=company_id,
            event_type="ncr_created",
            source_module="shop_floor",
            entity_type="ncr",
            entity_id=ncr.id,
            work_order_id=work_order.id,
            operation_id=operation.id,
            user_id=current_user.id,
            severity="high",
            event_payload={
                "ncr_number": ncr.ncr_number,
                "title": ncr.title,
                "source": NCRSource.IN_PROCESS.value,
                "quantity_affected": scrap_delta,
                "scrap_reason": scrap_reason,
                "scrap_reason_code": scrap_code.code if scrap_code else None,
            },
        )
        ncr_payload = {"id": ncr.id, "ncr_number": ncr.ncr_number}

    response = {
        "message": "Production quantity added",
        "operation": {
            "id": operation.id,
            "status": operation.status.value,
            "quantity_complete": operation.quantity_complete,
            "quantity_scrapped": operation.quantity_scrapped,
            "quantity_ordered": target_qty,
        },
        "active_time_entry": {
            "id": active_entry.id,
            "quantity_produced": active_entry.quantity_produced,
            "quantity_scrapped": active_entry.quantity_scrapped,
            "clock_out": to_utc_iso(active_entry.clock_out),
        },
        # Scrap -> NCR: the NCR this report filed (open_ncr=true), else null. The
        # kiosk success toast quotes the real ncr_number from here.
        "ncr": ncr_payload,
    }
    record_production_receipt(db, company_id, current_user.id, operation_id, active_entry.id, production_data, response)

    db.flush()
    return response


def hold_operation_command(db, current_user, company_id, operation_id, hold_data, audit):
    """Apply the existing hold, closing the operation crew, without commit or realtime I/O."""
    operation = (
        db.query(WorkOrderOperation)
        .options(joinedload(WorkOrderOperation.work_order))
        .filter(WorkOrderOperation.id == operation_id, WorkOrderOperation.company_id == company_id)
        .first()
    )

    if not operation:
        raise HTTPException(status_code=404, detail="Operation not found")

    if operation.status == OperationStatus.COMPLETE:
        raise HTTPException(status_code=400, detail="Cannot put completed operation on hold")

    # A stale screen can still name an operation whose nest was cancelled.
    # Accepting Hold here promises a reversible pause, but Resume correctly
    # refuses that deleted nest. Apply the same cancellation fence before
    # changing status, stopping labor or recording a successful hold.
    if dispatch_service.operation_has_cancelled_nest(db, company_id, operation.id):
        raise HTTPException(
            status_code=409, detail="This nest was cancelled. Restore the nest before putting it on hold."
        )

    # A0.1 adoption-telemetry channel of THIS hold write (kiosk-token forcing + import
    # guard). Resolved before any mutation so a disallowed 'import' 422s without changing
    # operation state or closing any entry.
    hold_source = _resolve_labor_source(current_user, hold_data.source if hold_data else None)

    operation.status = OperationStatus.ON_HOLD
    operation.updated_at = datetime.utcnow()

    # Close any open time entries for this operation
    open_entries = (
        db.query(TimeEntry)
        .filter(
            and_(
                TimeEntry.operation_id == operation_id,
                TimeEntry.company_id == company_id,
                TimeEntry.clock_out.is_(None),
            )
        )
        .all()
    )
    now = datetime.utcnow()
    for entry in open_entries:
        entry.clock_out = now
        if entry.clock_in:
            entry.duration_hours = (now - entry.clock_in).total_seconds() / 3600.0
        # A0.1 adoption telemetry: a hold auto-closes OTHER operators' open entries
        # too, so only FILL a missing channel -- never overwrite an entry's own
        # recorded clock-in channel with the holder's channel (same as /complete).
        if hold_source and entry.source is None:
            entry.source = hold_source

    # Create audit log (request-scoped service -- carries ip/user_agent)
    audit.log(
        action="HOLD_OPERATION",
        resource_type="work_order_operation",
        resource_id=operation_id,
        description=f"Put operation {operation.operation_number} on hold",
    )
    if work_order := operation.work_order:
        if hold_data and (hold_data.note or hold_data.category != WorkOrderBlockerCategory.OTHER):
            WorkOrderBlockerService(db).create_blocker(
                company_id=company_id,
                user=current_user,
                work_order_id=work_order.id,
                data=WorkOrderBlockerCreate(
                    operation_id=operation.id,
                    category=hold_data.category,
                    severity=hold_data.severity,
                    note=hold_data.note,
                    put_operation_on_hold=False,
                ),
                source=hold_source,
                audit=audit,
            )
        else:
            OperationalEventService(db).emit_best_effort(
                company_id=company_id,
                event_type="operation_hold",
                source_module="shop_floor",
                entity_type="work_order_operation",
                entity_id=operation.id,
                work_order_id=work_order.id,
                operation_id=operation.id,
                user_id=current_user.id,
                severity="medium",
                event_payload={
                    "work_order_number": work_order.work_order_number,
                    "operation_name": operation.name,
                    # A0.1 adoption telemetry: client channel (None = not reported).
                    "source": hold_source,
                },
            )

    db.flush()
    return operation
