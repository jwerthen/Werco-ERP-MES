"""Process Sheets business logic (PR 1 + PR 3 of docs/PROCESS_SHEETS_SCOPE.md).

Owns the sheet lifecycle (draft -> released -> obsolete + new revisions), sheet-number
generation, the per-type step-definition validation, and — since PR 3 — the WO-creation
snapshot, the shop-floor capture validation ladder (typed step records, tolerance
enforcement, supersede corrections) and the operation-completion gate. Each mutating
function owns its unit of work (commits at the end) and writes the tamper-evident audit
row BEFORE the terminal commit so the state change and its audit trail commit atomically
(AuditService only flushes; a request session never commits on teardown).

Invariants enforced here:
- Only DRAFT sheets are mutable (sheet fields, step CRUD, delete). Anything else -> 409.
- INSTRUCTION steps are never required; ``requires_gauge`` is MEASUREMENT-only.
- MEASUREMENT config needs numeric lsl/nominal/usl with lsl <= nominal <= usl, lsl < usl.
- LIST config needs a non-empty ``options`` array.
- ``spc_characteristic_id`` is MEASUREMENT-only and must resolve in the active company.
- All queries tenant-scoped via tenant_query(); soft delete only, never physical.
- Snapshot resolves the attached sheet's FAMILY (``sheet_number``) to its currently
  RELEASED revision; a family with no released revision blocks WO creation (409).
- Step records are append-only: an out-of-tolerance measurement is REFUSED (409, no
  row); corrections are new records that stamp ``superseded_by_id`` exactly once.
"""

import json
import math
import os
import uuid
from copy import deepcopy
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload, selectinload

from app.core.time_utils import to_utc_iso
from app.db.locks import acquire_generator_lock
from app.db.tenant_filter import tenant_query
from app.models.calibration import CalibrationStatus, Equipment
from app.models.document import Document, DocumentType
from app.models.process_sheet import (
    OperationStepRecord,
    ProcessSheet,
    ProcessSheetStatus,
    ProcessSheetStep,
    StepType,
    WOOperationStep,
)
from app.models.quality import (
    FAICharacteristic,
    FirstArticleInspection,
    NCRSource,
    NonConformanceReport,
)
from app.models.spc import SPCCharacteristic, SPCMeasurement
from app.models.time_entry import TimeEntry
from app.models.user import User
from app.models.work_order import OperationStatus, WorkOrder, WorkOrderOperation
from app.models.work_order_blocker import WorkOrderBlockerCategory, WorkOrderBlockerSeverity
from app.schemas.process_sheet import (
    OperationStepRecordCreate,
    OperationStepRecordSupersede,
    ProcessSheetCreate,
    ProcessSheetStepCreate,
    ProcessSheetStepUpdate,
    ProcessSheetUpdate,
    QualityHoldRequest,
)
from app.schemas.work_order_blocker import WorkOrderBlockerCreate
from app.services.audit_service import AuditService
from app.services.document_numbering import generate_document_number
from app.services.operational_event_service import OperationalEventService
from app.services.operator_qualification_service import evaluate_operator_qualification
from app.services.storage_service import get_storage, resolve_upload_dir, sanitize_ext
from app.services.work_order_blocker_service import WorkOrderBlockerService

SHEET_NUMBER_PREFIX = "PS-"

# Step-evidence attachment limits (PHOTO/FILE steps). MIME allowlists follow the
# existing documents-upload posture (client content type checked against an
# allowlist); FILE steps additionally accept PDFs.
MAX_STEP_ATTACHMENT_BYTES = 10 * 1024 * 1024  # 10 MB
ALLOWED_PHOTO_MIME_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif", "image/heic", "image/heif"}
ALLOWED_FILE_MIME_TYPES = ALLOWED_PHOTO_MIME_TYPES | {"application/pdf"}


# ---------- helpers ----------


def generate_sheet_number(db: Session, company_id: int) -> str:
    """Generate the next per-company sheet number (``PS-000123``).

    Holds a Postgres advisory lock for the duration of the transaction so two
    concurrent creates can't read the same "last number" (same pattern as
    work_order_number / ncr_number generation). Soft-deleted sheets are counted
    on purpose — a number is never reused.
    """
    acquire_generator_lock(db, "process_sheet_number", company_id)

    last = (
        tenant_query(db, ProcessSheet, company_id)
        .filter(ProcessSheet.sheet_number.like(f"{SHEET_NUMBER_PREFIX}%"))
        .order_by(ProcessSheet.sheet_number.desc())
        .first()
    )
    next_seq = 1
    if last:
        try:
            next_seq = int(last.sheet_number.split("-")[-1]) + 1
        except ValueError:
            next_seq = 1
    return f"{SHEET_NUMBER_PREFIX}{next_seq:06d}"


def _next_revision(current: str) -> str:
    """Excel-style letter increment: A -> B, ..., Z -> AA, AZ -> BA."""
    letters = list(current.upper())
    i = len(letters) - 1
    while i >= 0:
        if letters[i] != "Z":
            letters[i] = chr(ord(letters[i]) + 1)
            return "".join(letters)
        letters[i] = "A"
        i -= 1
    return "A" + "".join(letters)


def _sheet_identifier(sheet: ProcessSheet) -> str:
    return f"{sheet.sheet_number} Rev {sheet.revision}"


def _get_sheet_or_404(db: Session, company_id: int, sheet_id: int, with_steps: bool = False) -> ProcessSheet:
    query = tenant_query(db, ProcessSheet, company_id).filter(
        ProcessSheet.id == sheet_id, ProcessSheet.is_deleted == False  # noqa: E712
    )
    if with_steps:
        query = query.options(selectinload(ProcessSheet.steps))
    sheet = query.first()
    if not sheet:
        raise HTTPException(status_code=404, detail="Process sheet not found")
    return sheet


def _require_draft(sheet: ProcessSheet, action: str) -> None:
    if sheet.status != ProcessSheetStatus.DRAFT.value:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Cannot {action} a {sheet.status} process sheet — only drafts are editable. "
                "Create a new revision to change released content."
            ),
        )


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_step_definition(
    db: Session,
    company_id: int,
    *,
    step_type: str,
    config: Optional[Dict[str, Any]],
    is_required: bool,
    requires_gauge: bool,
    spc_characteristic_id: Optional[int],
) -> bool:
    """Validate the EFFECTIVE step definition; returns the normalized is_required.

    Called with the merged (existing + payload) values on update so partial payloads
    can't sneak an invalid combination past per-field checks.
    """
    if step_type == StepType.MEASUREMENT.value:
        if not isinstance(config, dict):
            raise HTTPException(
                status_code=400, detail="MEASUREMENT steps require a config with numeric lsl, nominal and usl"
            )
        for key in ("lsl", "nominal", "usl"):
            if not _is_number(config.get(key)):
                raise HTTPException(status_code=400, detail=f"MEASUREMENT config requires a numeric '{key}'")
        lsl, nominal, usl = config["lsl"], config["nominal"], config["usl"]
        if not (lsl <= nominal <= usl):
            raise HTTPException(status_code=400, detail="MEASUREMENT config must satisfy lsl <= nominal <= usl")
        if not lsl < usl:
            raise HTTPException(status_code=400, detail="MEASUREMENT config must satisfy lsl < usl")
        # PR 4 authoring guard (PR 3 audit note): capture rounds the measured value to
        # ``decimals`` BEFORE the tolerance check, so a rounding step coarser than the
        # tolerance band could pass an out-of-band measurement and store only the
        # rounded value. Require the rounding resolution to resolve the band.
        decimals = config.get("decimals")
        if decimals is not None:
            if isinstance(decimals, bool) or not isinstance(decimals, int) or decimals < 0:
                raise HTTPException(
                    status_code=400, detail="MEASUREMENT config 'decimals' must be a non-negative integer"
                )
            # Tiny epsilon so a band that EQUALS the resolution (spec: 10^-d <= usl-lsl)
            # is not rejected on float-subtraction noise.
            if 10**-decimals > (usl - lsl) + 1e-12:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"MEASUREMENT config 'decimals' ({decimals}) is too coarse to resolve the tolerance "
                        f"band (usl - lsl = {usl - lsl:g}); rounding at 10^-{decimals} could hide an "
                        "out-of-tolerance value"
                    ),
                )
    elif step_type == StepType.LIST.value:
        options = (config or {}).get("options")
        if not isinstance(options, list) or not options:
            raise HTTPException(status_code=400, detail="LIST steps require a config with a non-empty 'options' array")

    if requires_gauge and step_type != StepType.MEASUREMENT.value:
        raise HTTPException(status_code=400, detail="requires_gauge is only valid on MEASUREMENT steps")

    if spc_characteristic_id is not None:
        if step_type != StepType.MEASUREMENT.value:
            raise HTTPException(status_code=400, detail="spc_characteristic_id is only valid on MEASUREMENT steps")
        characteristic = (
            tenant_query(db, SPCCharacteristic, company_id)
            .filter(SPCCharacteristic.id == spc_characteristic_id)
            .first()
        )
        if not characteristic:
            raise HTTPException(status_code=404, detail="SPC characteristic not found")

    # INSTRUCTION steps are display-only: never required, regardless of what the client sent.
    if step_type == StepType.INSTRUCTION.value:
        return False
    return is_required


# ---------- sheet queries ----------


def list_sheets(
    db: Session,
    company_id: int,
    *,
    status: Optional[str] = None,
    search: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
) -> List[ProcessSheet]:
    query = (
        tenant_query(db, ProcessSheet, company_id)
        .filter(ProcessSheet.is_deleted == False)  # noqa: E712
        .options(selectinload(ProcessSheet.steps))
    )
    if status:
        query = query.filter(ProcessSheet.status == status)
    if search:
        pattern = f"%{search}%"
        query = query.filter((ProcessSheet.sheet_number.ilike(pattern)) | (ProcessSheet.title.ilike(pattern)))
    return (
        query.order_by(ProcessSheet.sheet_number.desc(), ProcessSheet.revision.desc()).offset(skip).limit(limit).all()
    )


def get_sheet(db: Session, company_id: int, sheet_id: int) -> ProcessSheet:
    return _get_sheet_or_404(db, company_id, sheet_id, with_steps=True)


# ---------- sheet lifecycle ----------


def create_sheet(
    db: Session, company_id: int, data: ProcessSheetCreate, user: User, audit: AuditService
) -> ProcessSheet:
    sheet = ProcessSheet(
        sheet_number=generate_sheet_number(db, company_id),
        title=data.title,
        description=data.description,
        revision="A",
        status=ProcessSheetStatus.DRAFT.value,
        created_by=user.id,
        company_id=company_id,
    )
    db.add(sheet)
    db.flush()  # assign the PK so the audit row carries a real resource_id
    audit.log_create("process_sheet", sheet.id, _sheet_identifier(sheet), new_values=sheet)
    db.commit()
    db.refresh(sheet)
    return sheet


def update_sheet(
    db: Session, company_id: int, sheet_id: int, data: ProcessSheetUpdate, user: User, audit: AuditService
) -> ProcessSheet:
    sheet = _get_sheet_or_404(db, company_id, sheet_id, with_steps=True)
    _require_draft(sheet, "update")

    update_data = data.model_dump(exclude_unset=True)
    changed = {f: v for f, v in update_data.items() if getattr(sheet, f) != v}
    if changed:
        old_values = {f: getattr(sheet, f) for f in changed}
        for field, value in changed.items():
            setattr(sheet, field, value)
        sheet.updated_by = user.id
        audit.log_update("process_sheet", sheet.id, _sheet_identifier(sheet), old_values=old_values, new_values=changed)
        db.commit()
        db.refresh(sheet)
    return sheet


def soft_delete_sheet(db: Session, company_id: int, sheet_id: int, user: User, audit: AuditService) -> None:
    sheet = _get_sheet_or_404(db, company_id, sheet_id)
    _require_draft(sheet, "delete")

    audit.log_delete("process_sheet", sheet.id, _sheet_identifier(sheet), old_values=sheet, soft_delete=True)
    sheet.soft_delete(user.id)
    db.commit()


def release_sheet(db: Session, company_id: int, sheet_id: int, user: User, audit: AuditService) -> ProcessSheet:
    sheet = _get_sheet_or_404(db, company_id, sheet_id, with_steps=True)
    if sheet.status != ProcessSheetStatus.DRAFT.value:
        raise HTTPException(status_code=409, detail=f"Only a draft sheet can be released (this one is {sheet.status})")
    if not sheet.steps:
        raise HTTPException(status_code=400, detail="Cannot release a process sheet with no steps")

    sheet.status = ProcessSheetStatus.RELEASED.value
    sheet.effective_date = datetime.utcnow()
    sheet.updated_by = user.id

    audit.log_status_change(
        "process_sheet",
        sheet.id,
        _sheet_identifier(sheet),
        old_status=ProcessSheetStatus.DRAFT.value,
        new_status=ProcessSheetStatus.RELEASED.value,
    )
    db.commit()
    db.refresh(sheet)
    return sheet


def obsolete_sheet(db: Session, company_id: int, sheet_id: int, user: User, audit: AuditService) -> ProcessSheet:
    sheet = _get_sheet_or_404(db, company_id, sheet_id, with_steps=True)
    if sheet.status != ProcessSheetStatus.RELEASED.value:
        raise HTTPException(
            status_code=409, detail=f"Only a released sheet can be obsoleted (this one is {sheet.status})"
        )

    sheet.status = ProcessSheetStatus.OBSOLETE.value
    sheet.obsolete_date = datetime.utcnow()
    sheet.is_active = False
    sheet.updated_by = user.id

    audit.log_status_change(
        "process_sheet",
        sheet.id,
        _sheet_identifier(sheet),
        old_status=ProcessSheetStatus.RELEASED.value,
        new_status=ProcessSheetStatus.OBSOLETE.value,
    )
    db.commit()
    db.refresh(sheet)
    return sheet


def new_revision(db: Session, company_id: int, sheet_id: int, user: User, audit: AuditService) -> ProcessSheet:
    """Copy a released/obsolete sheet (and its steps) to a new DRAFT row with the next revision letter."""
    source = _get_sheet_or_404(db, company_id, sheet_id, with_steps=True)
    if source.status == ProcessSheetStatus.DRAFT.value:
        raise HTTPException(status_code=409, detail="Sheet is still a draft — edit it directly instead of revising")

    # Serialize revision computation for this sheet family against concurrent revisers.
    acquire_generator_lock(db, f"process_sheet_revision:{source.sheet_number}", company_id)

    siblings = tenant_query(db, ProcessSheet, company_id).filter(ProcessSheet.sheet_number == source.sheet_number).all()
    existing_revisions = {s.revision.upper() for s in siblings}
    alpha_revisions = [r for r in existing_revisions if r.isalpha()]
    # Revision letters sort by (length, value): B < Z < AA < AB.
    candidate = _next_revision(max(alpha_revisions, key=lambda r: (len(r), r)) if alpha_revisions else "A")
    while candidate in existing_revisions:
        candidate = _next_revision(candidate)

    draft_in_family = next(
        (s for s in siblings if not s.is_deleted and s.status == ProcessSheetStatus.DRAFT.value), None
    )
    if draft_in_family:
        raise HTTPException(
            status_code=409,
            detail=f"A draft revision ({draft_in_family.revision}) of {source.sheet_number} already exists — edit it.",
        )

    revision = ProcessSheet(
        sheet_number=source.sheet_number,
        title=source.title,
        description=source.description,
        revision=candidate,
        status=ProcessSheetStatus.DRAFT.value,
        created_by=user.id,
        company_id=company_id,
    )
    db.add(revision)
    db.flush()

    for step in source.steps:
        db.add(
            ProcessSheetStep(
                process_sheet_id=revision.id,
                company_id=company_id,
                sequence=step.sequence,
                label=step.label,
                instruction_text=step.instruction_text,
                step_type=step.step_type,
                is_required=step.is_required,
                config=step.config,
                requires_gauge=step.requires_gauge,
                spc_characteristic_id=step.spc_characteristic_id,
            )
        )
    db.flush()

    audit.log_create(
        "process_sheet",
        revision.id,
        _sheet_identifier(revision),
        new_values=revision,
        description=f"Created process sheet revision {_sheet_identifier(revision)} from Rev {source.revision}",
        extra_data={"source_sheet_id": source.id, "source_revision": source.revision},
    )
    db.commit()
    db.refresh(revision)
    return revision


# ---------- step CRUD (draft sheets only) ----------


def _step_identifier(sheet: ProcessSheet, step: ProcessSheetStep) -> str:
    return f"{_sheet_identifier(sheet)} step {step.sequence}"


def _get_step_or_404(db: Session, company_id: int, sheet_id: int, step_id: int) -> ProcessSheetStep:
    step = (
        tenant_query(db, ProcessSheetStep, company_id)
        .filter(ProcessSheetStep.id == step_id, ProcessSheetStep.process_sheet_id == sheet_id)
        .first()
    )
    if not step:
        raise HTTPException(status_code=404, detail="Process sheet step not found")
    return step


def add_step(
    db: Session, company_id: int, sheet_id: int, data: ProcessSheetStepCreate, user: User, audit: AuditService
) -> ProcessSheetStep:
    sheet = _get_sheet_or_404(db, company_id, sheet_id)
    _require_draft(sheet, "add a step to")

    is_required = _validate_step_definition(
        db,
        company_id,
        step_type=data.step_type.value,
        config=data.config,
        is_required=data.is_required,
        requires_gauge=data.requires_gauge,
        spc_characteristic_id=data.spc_characteristic_id,
    )

    step = ProcessSheetStep(
        process_sheet_id=sheet.id,
        company_id=company_id,
        sequence=data.sequence,
        label=data.label,
        instruction_text=data.instruction_text,
        step_type=data.step_type.value,
        is_required=is_required,
        config=data.config,
        requires_gauge=data.requires_gauge,
        spc_characteristic_id=data.spc_characteristic_id,
    )
    db.add(step)
    sheet.updated_by = user.id
    db.flush()
    audit.log_create("process_sheet_step", step.id, _step_identifier(sheet, step), new_values=step)
    db.commit()
    db.refresh(step)
    return step


def update_step(
    db: Session,
    company_id: int,
    sheet_id: int,
    step_id: int,
    data: ProcessSheetStepUpdate,
    user: User,
    audit: AuditService,
) -> ProcessSheetStep:
    sheet = _get_sheet_or_404(db, company_id, sheet_id)
    _require_draft(sheet, "edit a step of")
    step = _get_step_or_404(db, company_id, sheet_id, step_id)

    update_data = data.model_dump(exclude_unset=True)
    if "step_type" in update_data and update_data["step_type"] is not None:
        update_data["step_type"] = update_data["step_type"].value

    # Validate the EFFECTIVE (merged) definition, not just the delta.
    effective = {
        "step_type": step.step_type,
        "config": step.config,
        "is_required": step.is_required,
        "requires_gauge": step.requires_gauge,
        "spc_characteristic_id": step.spc_characteristic_id,
    }
    effective.update({k: v for k, v in update_data.items() if k in effective})
    update_data["is_required"] = _validate_step_definition(
        db,
        company_id,
        step_type=effective["step_type"],
        config=effective["config"],
        is_required=effective["is_required"],
        requires_gauge=effective["requires_gauge"],
        spc_characteristic_id=effective["spc_characteristic_id"],
    )

    changed = {f: v for f, v in update_data.items() if getattr(step, f) != v}
    if changed:
        old_values = {f: getattr(step, f) for f in changed}
        for field, value in changed.items():
            setattr(step, field, value)
        sheet.updated_by = user.id
        audit.log_update(
            "process_sheet_step", step.id, _step_identifier(sheet, step), old_values=old_values, new_values=changed
        )
        db.commit()
        db.refresh(step)
    return step


def delete_step(db: Session, company_id: int, sheet_id: int, step_id: int, user: User, audit: AuditService) -> None:
    sheet = _get_sheet_or_404(db, company_id, sheet_id)
    _require_draft(sheet, "delete a step from")
    step = _get_step_or_404(db, company_id, sheet_id, step_id)

    # Hard delete is correct here: steps carry no SoftDeleteMixin and only exist on DRAFT
    # sheets (released content is immutable) — same precedent as RoutingOperation deletes.
    # log_delete serializes the model immediately, so the live instance captures full old state.
    audit.log_delete("process_sheet_step", step.id, _step_identifier(sheet, step), old_values=step, soft_delete=False)
    db.delete(step)
    sheet.updated_by = user.id
    db.commit()


# ---------- PR 3: snapshot at WO creation ----------


class ProcessSheetUnavailableError(HTTPException):
    """WO creation blocker: an attached sheet's family has no released revision.

    Raised inside ``snapshot_steps_for_work_order`` BEFORE any commit, so the whole
    work-order creation rolls back atomically. Carries the structured 409 payload
    (``code: PROCESS_SHEET_UNAVAILABLE``) both callers surface: POST /work-orders
    returns it verbatim; the Excel-migration import converts it to a row error.
    """

    def __init__(self, *, operation: str, sheet_number: str):
        super().__init__(
            status_code=409,
            detail={
                "code": "PROCESS_SHEET_UNAVAILABLE",
                "detail": (
                    f"Cannot create work order: operation {operation} references process sheet {sheet_number} "
                    "which has no released revision. Release a revision or detach the sheet from the routing."
                ),
                "operation": operation,
                "sheet_number": sheet_number,
            },
        )


def resolve_released_revision(db: Session, company_id: int, sheet_number: str) -> Optional[ProcessSheet]:
    """Resolve a sheet FAMILY (``sheet_number``) to its currently-RELEASED revision.

    Settled snapshot semantics (scope doc, 2026-07-06): the routing attach points at a
    sheet row, but the snapshot follows the family — releasing Rev B flows to future WOs
    without re-attaching routings. Soft-deleted and obsolete revisions never resolve.
    When a deliberate transition period leaves TWO revisions released, the highest
    revision letter wins (length-then-value ordering: B < Z < AA), deterministically.
    """
    released = (
        tenant_query(db, ProcessSheet, company_id)
        .filter(
            ProcessSheet.sheet_number == sheet_number,
            ProcessSheet.status == ProcessSheetStatus.RELEASED.value,
            ProcessSheet.is_deleted == False,  # noqa: E712
        )
        .options(selectinload(ProcessSheet.steps))
        .all()
    )
    if not released:
        return None
    return max(released, key=lambda sheet: (len(sheet.revision.upper()), sheet.revision.upper()))


def snapshot_steps_for_work_order(
    db: Session,
    company_id: int,
    operation_sheet_pairs: Iterable[Tuple[WorkOrderOperation, Optional[int]]],
) -> List[Dict[str, Any]]:
    """Copy released process-sheet steps onto freshly created WO operations (the traveler).

    ``operation_sheet_pairs`` are (wo_operation, attached process_sheet_id) straight from
    the routing copy — pairs without a sheet are skipped. For each attached sheet the
    family is resolved to its currently-RELEASED revision and every step is copied into
    ``wo_operation_steps`` (immutable snapshot; ``source_sheet_id``/``source_sheet_revision``
    record exactly what was snapshotted). A family with NO released revision raises
    ``ProcessSheetUnavailableError`` (409) — never snapshot obsolete content, never
    silently skip. Flushes but never commits: the caller's unit of work stays atomic.

    Returns the snapshot summary (one entry per operation) for the WO-creation audit row.
    """
    pairs = [(op, sheet_id) for op, sheet_id in operation_sheet_pairs if sheet_id]
    if not pairs:
        return []
    db.flush()  # assign operation PKs so snapshot rows can reference them

    attached_ids = {sheet_id for _, sheet_id in pairs}
    attached_by_id = {
        sheet.id: sheet
        for sheet in tenant_query(db, ProcessSheet, company_id).filter(ProcessSheet.id.in_(attached_ids)).all()
    }

    resolved_by_number: Dict[str, Optional[ProcessSheet]] = {}
    summary: List[Dict[str, Any]] = []
    for op, sheet_id in sorted(pairs, key=lambda pair: (pair[0].sequence or 0, pair[0].id or 0)):
        identifier = op.operation_number or f"Op {op.sequence}"
        attached = attached_by_id.get(sheet_id)
        if attached is None:
            # FK integrity makes this a cross-tenant attach or a vanished row —
            # either way there is nothing releasable to snapshot.
            raise ProcessSheetUnavailableError(operation=identifier, sheet_number=f"id {sheet_id}")

        if attached.sheet_number not in resolved_by_number:
            resolved_by_number[attached.sheet_number] = resolve_released_revision(db, company_id, attached.sheet_number)
        resolved = resolved_by_number[attached.sheet_number]
        if resolved is None:
            raise ProcessSheetUnavailableError(operation=identifier, sheet_number=attached.sheet_number)

        for step in resolved.steps:  # relationship is ordered by sequence
            db.add(
                WOOperationStep(
                    company_id=company_id,
                    work_order_operation_id=op.id,
                    source_sheet_id=resolved.id,
                    source_sheet_revision=resolved.revision,
                    sequence=step.sequence,
                    label=step.label,
                    instruction_text=step.instruction_text,
                    step_type=step.step_type,
                    is_required=step.is_required,
                    config=deepcopy(step.config),  # never share a mutable JSON payload with the library row
                    requires_gauge=step.requires_gauge,
                    spc_characteristic_id=step.spc_characteristic_id,
                )
            )
        summary.append(
            {
                "operation": identifier,
                "operation_sequence": op.sequence,
                "attached_sheet_id": sheet_id,
                "sheet_number": attached.sheet_number,
                "resolved_sheet_id": resolved.id,
                "resolved_revision": resolved.revision,
                "step_count": len(resolved.steps),
            }
        )
    db.flush()
    return summary


# ---------- PR 3: shop-floor capture ----------


def parse_serial_numbers(raw: Any) -> List[str]:
    """Parse a serial-numbers JSON Text snapshot into a list, guarding non-JSON values.

    THE shared serial parser (PR 4 ledger): ``parse_work_order_serials`` below and
    ``coc_service._parse_serial_numbers`` both delegate here so the "what counts as a
    serialized snapshot" rule can never drift between capture and CoC generation.
    """
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(s) for s in raw]
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if isinstance(parsed, list):
        return [str(s) for s in parsed]
    return []


def parse_work_order_serials(work_order: WorkOrder) -> List[str]:
    """Parse ``WorkOrder.serial_numbers`` — a WO is "serialized" exactly when non-empty."""
    return parse_serial_numbers(work_order.serial_numbers)


def get_wo_step_or_404(db: Session, company_id: int, operation_id: int, step_id: int) -> WOOperationStep:
    """Fetch a snapshot step, tenant-scoped AND pinned to the operation in the path."""
    step = (
        tenant_query(db, WOOperationStep, company_id)
        .filter(WOOperationStep.id == step_id, WOOperationStep.work_order_operation_id == operation_id)
        .first()
    )
    if not step:
        raise HTTPException(status_code=404, detail="Process step not found for this operation")
    return step


def _display_name(user: Optional[User]) -> Optional[str]:
    if user is None:
        return None
    name = f"{user.first_name or ''} {user.last_name or ''}".strip()
    return name or user.email


def _round_measurement(value: float, config: Optional[Dict[str, Any]]) -> float:
    decimals = (config or {}).get("decimals")
    if isinstance(decimals, int) and not isinstance(decimals, bool) and decimals >= 0:
        return round(value, decimals)
    return value


def _resolve_equipment(
    db: Session, company_id: int, *, equipment_id: Optional[int], equipment_code: Optional[str]
) -> Optional[Equipment]:
    """Resolve the gauge reference on a capture payload (PR 4 addendum).

    Kiosk operator tokens are path-fenced away from ``/equipment``, so the kiosk cannot
    list gauges — operators scan/type the gauge's MARKED identifier instead. Exactly one
    of ``equipment_id`` (int PK) or ``equipment_code`` (``Equipment.equipment_id``, the
    unique human-readable/barcode identifier) may be supplied (both -> 400). Both forms
    resolve tenant-scoped; an unknown code 404s naming the identifier.
    """
    if equipment_id is not None and equipment_code is not None:
        raise HTTPException(
            status_code=400,
            detail="Provide either equipment_id or equipment_code for the gauge, not both",
        )
    if equipment_code is not None:
        code = equipment_code.strip()
        if not code:
            raise HTTPException(status_code=400, detail="equipment_code must not be blank")
        # Case-insensitive on purpose (code review): the kiosk field is scan-OR-TYPE
        # and a typed code shouldn't fail on case. The column is globally unique, so
        # a lowercase match cannot be ambiguous; the echo carries the CANONICAL
        # stored code.
        equipment = (
            tenant_query(db, Equipment, company_id).filter(func.lower(Equipment.equipment_id) == code.lower()).first()
        )
        if not equipment:
            raise HTTPException(status_code=404, detail=f"No gauge with identifier '{code}'")
        return equipment
    if equipment_id is not None:
        equipment = tenant_query(db, Equipment, company_id).filter(Equipment.id == equipment_id).first()
        if not equipment:
            raise HTTPException(status_code=404, detail="Equipment not found")
        return equipment
    return None


def equipment_ref(equipment: Optional[Equipment]) -> Optional[Dict[str, Any]]:
    """The resolved-gauge echo for capture responses: {equipment_id, equipment_code, name}."""
    if equipment is None:
        return None
    return {"equipment_id": equipment.id, "equipment_code": equipment.equipment_id, "name": equipment.name}


def _validate_gauge(step: WOOperationStep, equipment: Optional[Equipment]) -> None:
    """Gauge calibration validation for a step record (PR 4 enforcement).

    Takes the already-RESOLVED gauge (``_resolve_equipment`` owns id/code resolution and
    tenant scoping). ``requires_gauge`` MEASUREMENT steps: a gauge is MANDATORY (400)
    and must be calibration-current — ``Equipment.status == ACTIVE`` AND a
    ``next_calibration_date`` on or after today (the caller-implemented currency rule
    from ``models/calibration.py``; a gauge with NO due date is not demonstrably
    current, so it fails closed) — else **409 ``GAUGE_OUT_OF_CAL``** and NO record row.
    Every other step keeps the PR 3 posture: an optional tenant-validated passthrough
    (no calibration check).

    Runs BEFORE the tolerance evaluation on purpose: a measurement taken with an
    out-of-cal gauge is untrustworthy in both directions, so it must be refused before
    it can either pass the gate or trigger the OOT/NCR path.
    """
    if step.requires_gauge and step.step_type == StepType.MEASUREMENT.value and equipment is None:
        raise HTTPException(
            status_code=400,
            detail="This measurement step requires a calibrated gauge — equipment_id or equipment_code is required",
        )
    if equipment is None:
        return
    if step.requires_gauge and step.step_type == StepType.MEASUREMENT.value:
        is_current = (
            equipment.status == CalibrationStatus.ACTIVE
            and equipment.next_calibration_date is not None
            and equipment.next_calibration_date >= date.today()
        )
        if not is_current:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "GAUGE_OUT_OF_CAL",
                    "detail": (
                        f"Gauge '{equipment.name}' ({equipment.equipment_id}) is not calibration-current — "
                        "use a current gauge or route this one to calibration"
                    ),
                    "equipment_id": equipment.id,
                    "status": (equipment.status.value if hasattr(equipment.status, "value") else equipment.status),
                    "next_calibration_date": (
                        equipment.next_calibration_date.isoformat() if equipment.next_calibration_date else None
                    ),
                },
            )


def _reject_stray_values(step_type: str, provided: Dict[str, Any], allowed: Sequence[str]) -> None:
    stray = [field for field, value in provided.items() if value is not None and field not in allowed]
    if stray:
        raise HTTPException(
            status_code=400,
            detail=f"{', '.join(sorted(stray))} not valid for a {step_type} step",
        )


def build_step_record(
    db: Session,
    company_id: int,
    *,
    work_order: WorkOrder,
    operation: WorkOrderOperation,
    step: WOOperationStep,
    serial_number: Optional[str],
    value_numeric: Optional[float],
    value_bool: Optional[bool],
    value_text: Optional[str],
    equipment_id: Optional[int],
    attachment_document_id: Optional[int],
    recorded_by: int,
    source: Optional[str],
    equipment_code: Optional[str] = None,
) -> OperationStepRecord:
    """Run the capture validation ladder and return the (unflushed) record row.

    Ladder (order matters — the callers already enforced WO-not-terminal /
    operation-IN_PROGRESS and that the step belongs to the operation):
      1. INSTRUCTION steps take no records (400).
      2. serialized WO -> serial_number required and must be one of the WO's serials;
         non-serialized -> serial_number must be absent (400).
      3. gauge (PR 4): the reference resolves from ``equipment_id`` OR ``equipment_code``
         (the gauge's marked identifier — kiosk scan/type path; both -> 400, unknown
         code -> 404). ``requires_gauge`` MEASUREMENT steps demand a MANDATORY,
         calibration-current gauge — missing -> 400, stale/inactive -> 409
         ``GAUGE_OUT_OF_CAL`` with NO row; other steps keep the optional
         tenant-validated passthrough. Checked BEFORE tolerance on purpose (see
         ``_validate_gauge``).
      4. type-shaped value (exactly the fields the step type takes; 400 otherwise);
         MEASUREMENT values are rounded per config ``decimals`` before storing.
      5. MEASUREMENT conformance from the SNAPSHOT lsl/usl — out-of-tolerance is
         REFUSED with 409 ``OUT_OF_TOLERANCE`` and NO row (hold+NCR via the PR 4
         quality-hold one-tap, or a corrected re-measurement, are the only paths
         forward). CHECKBOX conformance is the checkbox itself
         (``is_conforming = value_bool``): a False record is honest evidence that
         never satisfies the completion gate.
    """
    if step.step_type == StepType.INSTRUCTION.value:
        raise HTTPException(status_code=400, detail="INSTRUCTION steps are display-only and take no records")

    serials = parse_work_order_serials(work_order)
    serial_number = serial_number.strip() if isinstance(serial_number, str) else serial_number
    if serials:
        if not serial_number:
            raise HTTPException(
                status_code=400,
                detail="This work order is serialized — serial_number is required for step records",
            )
        if serial_number not in serials:
            raise HTTPException(
                status_code=400,
                detail=f"Serial '{serial_number}' is not one of this work order's serial numbers",
            )
    elif serial_number:
        raise HTTPException(
            status_code=400,
            detail="This work order is not serialized — serial_number must be omitted",
        )

    equipment = _resolve_equipment(db, company_id, equipment_id=equipment_id, equipment_code=equipment_code)
    _validate_gauge(step, equipment)

    provided = {
        "value_numeric": value_numeric,
        "value_bool": value_bool,
        "value_text": value_text,
        "attachment_document_id": attachment_document_id,
    }
    config = step.config or {}
    is_conforming: Optional[bool] = None

    if step.step_type == StepType.MEASUREMENT.value:
        _reject_stray_values(step.step_type, provided, allowed=("value_numeric",))
        if value_numeric is None:
            raise HTTPException(status_code=400, detail="MEASUREMENT steps require value_numeric")
        if math.isnan(value_numeric) or math.isinf(value_numeric):
            raise HTTPException(status_code=400, detail="value_numeric must be a valid number")
        value_numeric = _round_measurement(float(value_numeric), config)
        lsl, usl = config.get("lsl"), config.get("usl")
        if _is_number(lsl) and _is_number(usl):
            is_conforming = bool(lsl <= value_numeric <= usl)
            if not is_conforming:
                # Blocks recording as passed — 409, and NO record row is written.
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "OUT_OF_TOLERANCE",
                        "detail": f"Measured {value_numeric} is outside tolerance ({lsl} to {usl})",
                        "measured": value_numeric,
                        "lsl": lsl,
                        "usl": usl,
                    },
                )
        else:
            # Snapshot without numeric limits (authoring validation makes this rare):
            # nothing to violate, record as conforming.
            is_conforming = True
    elif step.step_type == StepType.CHECKBOX.value:
        _reject_stray_values(step.step_type, provided, allowed=("value_bool",))
        if value_bool is None:
            raise HTTPException(status_code=400, detail="CHECKBOX steps require value_bool")
        # The checkbox IS the conformance assertion: an honest "not done" (False)
        # record is accepted as evidence but is non-conforming, so it never
        # satisfies the completion gate — supersede-to-true is the correction route.
        is_conforming = bool(value_bool)
    elif step.step_type == StepType.LIST.value:
        _reject_stray_values(step.step_type, provided, allowed=("value_text",))
        options = config.get("options") if isinstance(config.get("options"), list) else []
        if value_text is None or value_text not in [str(option) for option in options]:
            raise HTTPException(
                status_code=400,
                detail=f"LIST steps require value_text matching one of the configured options: {options}",
            )
    elif step.step_type == StepType.VALUE.value:
        _reject_stray_values(step.step_type, provided, allowed=("value_text",))
        if value_text is None or not value_text.strip():
            raise HTTPException(status_code=400, detail="VALUE steps require a non-empty value_text")
    elif step.step_type in (StepType.PHOTO.value, StepType.FILE.value):
        _reject_stray_values(step.step_type, provided, allowed=("attachment_document_id",))
        if attachment_document_id is None:
            raise HTTPException(
                status_code=400,
                detail=f"{step.step_type.upper()} steps require attachment_document_id "
                "(upload evidence via the step attachment endpoint first)",
            )
        document = tenant_query(db, Document, company_id).filter(Document.id == attachment_document_id).first()
        if not document:
            raise HTTPException(status_code=404, detail="Attachment document not found")
        # Evidence-laundering guard: the referenced document must be exactly what the
        # in-fence step-attachment upload produces — a QUALITY_RECORD linked to THIS
        # work order. Any other in-tenant document (a drawing, a cert, another WO's
        # quality record) is not objective evidence for this operation's step.
        if document.document_type != DocumentType.QUALITY_RECORD or document.work_order_id != work_order.id:
            raise HTTPException(
                status_code=400,
                detail="attachment_document_id must reference a QUALITY_RECORD document belonging to this "
                "work order — upload evidence via the step attachment endpoint first",
            )
    else:  # pragma: no cover — enum is closed; defensive against bad snapshot data
        raise HTTPException(status_code=400, detail=f"Unknown step type: {step.step_type}")

    return OperationStepRecord(
        company_id=company_id,
        wo_operation_step_id=step.id,
        work_order_operation_id=operation.id,
        serial_number=serial_number,
        value_text=value_text,
        value_numeric=value_numeric,
        value_bool=value_bool,
        is_conforming=is_conforming,
        recorded_by=recorded_by,
        recorded_at=datetime.utcnow(),
        source=source,
        equipment_id=equipment.id if equipment else None,
        attachment_document_id=attachment_document_id,
    )


def _record_identifier(work_order: WorkOrder, operation: WorkOrderOperation, step: WOOperationStep) -> str:
    return f"WO {work_order.work_order_number} {operation.operation_number or operation.sequence} step {step.sequence}"


def build_qualification_snapshot(
    db: Session, company_id: int, *, user_id: int, operation: WorkOrderOperation
) -> Optional[Dict[str, Any]]:
    """Warn-and-record operator-qualification snapshot for a step record (PR 4).

    Evaluates ``evaluate_operator_qualification`` (read-only, tenant-scoped) for
    (recorded_by, the operation's work center) and returns the JSON to freeze onto
    ``operation_step_records.qualification_snapshot``. NEVER blocks — an unqualified
    recorder still records; the snapshot just makes it discoverable at the record.
    Called once per request (each request writes exactly one record, so one
    evaluation per (user, work_center) per request). ``None`` when the operation has
    no work center — there is no gate to evaluate against.
    """
    if operation.work_center_id is None:
        return None
    exceptions = evaluate_operator_qualification(
        db, user_id=user_id, work_center_id=operation.work_center_id, company_id=company_id
    )
    return {
        "evaluated_at": to_utc_iso(datetime.utcnow()),
        "user_id": user_id,
        "work_center_id": operation.work_center_id,
        "qualified": not exceptions,
        "exceptions": [exc.as_dict() for exc in exceptions],
    }


def _feed_spc_measurement(
    db: Session,
    company_id: int,
    *,
    work_order: WorkOrder,
    operation: WorkOrderOperation,
    step: WOOperationStep,
    record: OperationStepRecord,
) -> Tuple[Optional[SPCMeasurement], Optional[str]]:
    """Auto-insert the SPC data point for a conforming MEASUREMENT record (PR 4).

    Fires only for MEASUREMENT steps carrying ``spc_characteristic_id`` whose record is
    conforming — refused OOT values never reach here (no row, no point), and a
    superseding re-measurement inserts a NEW point (SPC sees reality; the chart is a
    time series, not a corrected ledger). Flushes, never commits — the point lands in
    the same transaction as the record and its audit row.

    The characteristic was validated at AUTHORING time, but sheets outlive
    characteristics: a missing/cross-tenant characteristic at RECORD time degrades to
    (None, note) — the caller stamps the note into the audit ``extra_data`` — and never
    fails the record.

    Returns ``(measurement, None)`` on insert, ``(None, note)`` on a degrade,
    ``(None, None)`` when the step simply doesn't feed SPC.
    """
    if step.step_type != StepType.MEASUREMENT.value or step.spc_characteristic_id is None:
        return None, None
    if record.is_conforming is not True or record.value_numeric is None:
        return None, None

    characteristic = (
        tenant_query(db, SPCCharacteristic, company_id)
        .filter(SPCCharacteristic.id == step.spc_characteristic_id)
        .first()
    )
    if characteristic is None:
        return None, (
            f"SPC characteristic {step.spc_characteristic_id} no longer exists in this company — "
            "measurement recorded without an SPC point"
        )

    # Each capture is an individual point: next subgroup for the characteristic,
    # sample 1. Computed under no lock on purpose — a rare concurrent capture placing
    # two points in one subgroup number is still valid chart data (no unique
    # constraint), and the capture path must stay cheap.
    last_subgroup = (
        db.query(SPCMeasurement.subgroup_number)
        .filter(
            SPCMeasurement.characteristic_id == characteristic.id,
            SPCMeasurement.company_id == company_id,
        )
        .order_by(SPCMeasurement.subgroup_number.desc())
        .limit(1)
        .scalar()
    )
    measurement = SPCMeasurement(
        company_id=company_id,
        characteristic_id=characteristic.id,
        subgroup_number=int(last_subgroup or 0) + 1,
        sample_number=1,
        measurement_value=float(record.value_numeric),
        measured_at=record.recorded_at,
        measured_by=record.recorded_by,
        work_order_id=work_order.id,
        operation_id=operation.id,
        lot_number=work_order.lot_number,
        serial_number=record.serial_number,
        notes=f"Auto-captured from process step '{step.label}' ({_record_identifier(work_order, operation, step)})",
    )
    db.add(measurement)
    db.flush()
    return measurement, None


def _integration_extra_data(
    spc_measurement: Optional[SPCMeasurement], spc_note: Optional[str], snapshot: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    """PR 4 additions to the record-create audit ``extra_data`` (only what applies)."""
    extra: Dict[str, Any] = {}
    if spc_measurement is not None:
        extra["spc_measurement_id"] = spc_measurement.id
        extra["spc_characteristic_id"] = spc_measurement.characteristic_id
    if spc_note:
        extra["spc_note"] = spc_note
    if snapshot is not None and not snapshot.get("qualified", True):
        extra["qualification_exceptions"] = snapshot.get("exceptions", [])
    return extra


def create_step_record(
    db: Session,
    company_id: int,
    *,
    work_order: WorkOrder,
    operation: WorkOrderOperation,
    step: WOOperationStep,
    data: OperationStepRecordCreate,
    user: User,
    audit: AuditService,
    source: Optional[str],
) -> OperationStepRecord:
    """Capture one step record (append-only objective evidence). Audited before commit.

    PR 4 integrations, all inside this one transaction: the operator-qualification
    snapshot (warn-and-record, never blocks) is frozen onto the record, and a
    conforming MEASUREMENT with ``spc_characteristic_id`` feeds an ``SPCMeasurement``
    point (a missing characteristic degrades to a note on the audit row).
    """
    record = build_step_record(
        db,
        company_id,
        work_order=work_order,
        operation=operation,
        step=step,
        serial_number=data.serial_number,
        value_numeric=data.value_numeric,
        value_bool=data.value_bool,
        value_text=data.value_text,
        equipment_id=data.equipment_id,
        equipment_code=data.equipment_code,
        attachment_document_id=data.attachment_document_id,
        recorded_by=user.id,
        source=source,
    )
    record.qualification_snapshot = build_qualification_snapshot(db, company_id, user_id=user.id, operation=operation)
    db.add(record)
    db.flush()
    spc_measurement, spc_note = _feed_spc_measurement(
        db, company_id, work_order=work_order, operation=operation, step=step, record=record
    )
    audit.log_create(
        "operation_step_record",
        record.id,
        _record_identifier(work_order, operation, step),
        new_values=record,
        extra_data={
            "work_order_id": work_order.id,
            "work_order_operation_id": operation.id,
            "wo_operation_step_id": step.id,
            "step_label": step.label,
            "step_type": step.step_type,
            "serial_number": record.serial_number,
            "source": source,
            **_integration_extra_data(spc_measurement, spc_note, record.qualification_snapshot),
        },
    )
    db.commit()
    db.refresh(record)
    record.recorded_by_name = _display_name(user)  # transient, read by the response schema
    record.gauge = equipment_ref(record.equipment)  # transient echo of the resolved gauge (PR 4 addendum)
    return record


def supersede_step_record(
    db: Session,
    company_id: int,
    *,
    work_order: WorkOrder,
    operation: WorkOrderOperation,
    step: WOOperationStep,
    record_id: int,
    data: OperationStepRecordSupersede,
    user: User,
    audit: AuditService,
    source: Optional[str],
) -> OperationStepRecord:
    """Correction path: a NEW record replaces the old one; the old row is stamped once.

    The replacement runs the FULL validation ladder (including the out-of-tolerance
    refusal) and inherits the superseded record's serial — a correction always targets
    the same (step, serial) evidence slot. An already-superseded record 409s, and the
    old-row fetch is FOR UPDATE so two concurrent corrections serialize on the stamp.
    """
    old = (
        tenant_query(db, OperationStepRecord, company_id)
        .filter(
            OperationStepRecord.id == record_id,
            OperationStepRecord.wo_operation_step_id == step.id,
            OperationStepRecord.work_order_operation_id == operation.id,
        )
        .with_for_update()
        .first()
    )
    if not old:
        raise HTTPException(status_code=404, detail="Step record not found")
    if old.superseded_by_id is not None:
        raise HTTPException(
            status_code=409,
            detail="This record has already been superseded — correct the latest record instead",
        )

    replacement = build_step_record(
        db,
        company_id,
        work_order=work_order,
        operation=operation,
        step=step,
        serial_number=old.serial_number,
        value_numeric=data.value_numeric,
        value_bool=data.value_bool,
        value_text=data.value_text,
        equipment_id=data.equipment_id,
        equipment_code=data.equipment_code,
        attachment_document_id=data.attachment_document_id,
        recorded_by=user.id,
        source=source,
    )
    replacement.qualification_snapshot = build_qualification_snapshot(
        db, company_id, user_id=user.id, operation=operation
    )
    db.add(replacement)
    db.flush()

    # The ONE permitted mutation of an existing record: stamp the correction chain.
    old.superseded_by_id = replacement.id
    old.supersede_reason = data.reason

    # PR 4 SPC feed: a superseding re-measurement inserts a NEW point — SPC sees
    # reality (the chart is a time series); the correction chain lives on the records.
    spc_measurement, spc_note = _feed_spc_measurement(
        db, company_id, work_order=work_order, operation=operation, step=step, record=replacement
    )

    identifier = _record_identifier(work_order, operation, step)
    audit.log_create(
        "operation_step_record",
        replacement.id,
        identifier,
        new_values=replacement,
        description=f"Correction record for {identifier}: {data.reason}",
        extra_data={
            "work_order_id": work_order.id,
            "work_order_operation_id": operation.id,
            "wo_operation_step_id": step.id,
            "supersedes_record_id": old.id,
            "supersede_reason": data.reason,
            "serial_number": replacement.serial_number,
            "source": source,
            **_integration_extra_data(spc_measurement, spc_note, replacement.qualification_snapshot),
        },
    )
    audit.log_update(
        "operation_step_record",
        old.id,
        identifier,
        old_values={"superseded_by_id": None, "supersede_reason": None},
        new_values={"superseded_by_id": replacement.id, "supersede_reason": data.reason},
        description=f"Superseded by record {replacement.id}: {data.reason}",
    )
    db.commit()
    db.refresh(replacement)
    replacement.recorded_by_name = _display_name(user)  # transient, read by the response schema
    replacement.gauge = equipment_ref(replacement.equipment)  # transient echo of the resolved gauge (PR 4 addendum)
    return replacement


# ---------- PR 4: OOT -> NCR one-tap quality hold ----------


def _measurement_specification_text(step: WOOperationStep) -> Tuple[str, str]:
    """(specification, required_value) strings for the NCR, from the SNAPSHOT config."""
    config = step.config or {}
    lsl, nominal, usl = config.get("lsl"), config.get("nominal"), config.get("usl")
    unit = config.get("unit")
    unit_suffix = f" {unit}" if unit else ""
    required = f"{lsl} to {usl}{unit_suffix}"
    specification = f"{step.label}: nominal {nominal}, LSL {lsl}, USL {usl}{unit_suffix}"
    return specification[:255], required[:255]


def create_quality_hold(
    db: Session,
    company_id: int,
    *,
    work_order: WorkOrder,
    operation: WorkOrderOperation,
    step: WOOperationStep,
    data: QualityHoldRequest,
    user: User,
    audit: AuditService,
    source: Optional[str],
) -> Dict[str, Any]:
    """One-tap escape hatch for an out-of-tolerance measurement (PR 4).

    The OOT record was REFUSED (409, no row) — this is the sanctioned path forward.
    Atomically (one transaction, audited before the terminal commit):

      1. Creates an ``IN_PROCESS`` NCR pre-filled from the SNAPSHOT step config
         (``specification``/``required_value`` from lsl/nominal/usl, ``actual_value``
         = the refused measurement, part/lot/serial from the WO).
      2. Files a QUALITY_HOLD ``WorkOrderBlocker`` carrying the new ``ncr_id`` FK via
         the existing blocker service — the SAME hold pathway the kiosk hold button
         uses — which flips the operation ON_HOLD and audits the status change.
      3. Closes any open TimeEntries on the operation (mirrors ``PUT .../hold``): a
         held op accrues no labor; channels are filled, never overwritten.

    Only MEASUREMENT steps take a quality hold (400 otherwise), and the same
    recordable-state / serial rules as the capture endpoint apply, so a hold can only
    be filed where the refused measurement could have been recorded.

    SF-1 (compliance audit): the value must genuinely be OUT of tolerance. It is
    rounded per config ``decimals`` (exactly as capture would have) and then REQUIRED
    to fall outside [lsl, usl] — an in-band value is a 409 ``VALUE_IN_TOLERANCE``
    (record it as a normal step record instead), and a snapshot config without numeric
    limits is a 400 (an unbounded measurement has no tolerance to violate). Otherwise
    the NCR's auto-description would falsely assert "Recording was refused at capture"
    and the same value could exist as BOTH a conforming record and an OOT NCR.

    N-1: the gauge used may be supplied as ``equipment_id`` OR ``equipment_code``
    (same resolution rules as capture: tenant-scoped, unknown code -> 404, both ->
    400) but with NO calibration gating — the escape hatch must never trap the
    operator behind a stale gauge. The resolved identity lands server-side in the NCR
    description and the audit ``extra_data``.
    """
    # Service->endpoint edge kept function-local on purpose (laser_nest_service
    # precedent): quality.py owns the canonical company-scoped NCR number generator
    # and importing it beats a third copy drifting. TERMINAL_WO_STATUSES stays
    # function-local for the same reason work_order_state_service imports THIS module
    # function-locally — keep the pss<->wosss edge out of the module import graph.
    from app.api.endpoints.quality import generate_ncr_number
    from app.services.work_order_state_service import TERMINAL_WO_STATUSES

    # Code-review fix (concurrent double-tap): re-fetch the operation under
    # SELECT ... FOR UPDATE (same locking pattern as both /complete twins) and
    # RE-VERIFY the state after the lock is granted. Two concurrent one-taps both
    # pass the endpoint's unlocked pre-check; without this, the second files a
    # duplicate NCR + blocker and audits a no-op on_hold -> on_hold transition.
    # The second tap now blocks on the row lock, re-reads ON_HOLD, and is refused.
    locked_operation = (
        tenant_query(db, WorkOrderOperation, company_id)
        .filter(WorkOrderOperation.id == operation.id)
        .with_for_update()
        .first()
    )
    if not locked_operation:
        raise HTTPException(status_code=404, detail="Operation not found")
    operation = locked_operation
    if work_order.status in TERMINAL_WO_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"cannot raise a quality hold: work order is {work_order.status.value}",
        )
    if operation.status != OperationStatus.IN_PROGRESS:
        raise HTTPException(status_code=400, detail="Operation must be in progress to raise a quality hold")

    if step.step_type != StepType.MEASUREMENT.value:
        raise HTTPException(status_code=400, detail="Only MEASUREMENT steps can raise a quality hold")
    if data.measured_value is None or math.isnan(data.measured_value) or math.isinf(data.measured_value):
        raise HTTPException(status_code=400, detail="measured_value must be a valid number")

    # SF-1: verify the claim. Round exactly as capture would, then require the value
    # to fall OUTSIDE the snapshot tolerance band.
    config = step.config or {}
    lsl, usl = config.get("lsl"), config.get("usl")
    if not (_is_number(lsl) and _is_number(usl)):
        raise HTTPException(
            status_code=400,
            detail="This measurement step has no numeric tolerance limits — an unbounded "
            "measurement cannot be out of tolerance, so it takes no quality hold",
        )
    measured_value = _round_measurement(float(data.measured_value), config)
    if lsl <= measured_value <= usl:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "VALUE_IN_TOLERANCE",
                "detail": (
                    f"Measured {measured_value} is within tolerance ({lsl} to {usl}) — "
                    "record it as a step record instead of raising a quality hold"
                ),
                "measured": measured_value,
                "lsl": lsl,
                "usl": usl,
            },
        )

    # N-1: resolve the gauge reference (id/code, tenant-scoped) WITHOUT the
    # calibration gate — never trap the OOT escape hatch behind a stale gauge.
    equipment = _resolve_equipment(db, company_id, equipment_id=data.equipment_id, equipment_code=data.equipment_code)

    serial_number = data.serial_number.strip() if isinstance(data.serial_number, str) else data.serial_number
    serials = parse_work_order_serials(work_order)
    if serials:
        if not serial_number:
            raise HTTPException(
                status_code=400,
                detail="This work order is serialized — serial_number is required for a quality hold",
            )
        if serial_number not in serials:
            raise HTTPException(
                status_code=400,
                detail=f"Serial '{serial_number}' is not one of this work order's serial numbers",
            )
    elif serial_number:
        raise HTTPException(
            status_code=400,
            detail="This work order is not serialized — serial_number must be omitted",
        )

    identifier = _record_identifier(work_order, operation, step)
    specification, required_value = _measurement_specification_text(step)

    description = (
        f"Out-of-tolerance measurement on process step '{step.label}' "
        f"({identifier}): measured {measured_value}, required {required_value}. "
        "Recording was refused at capture; part placed on quality hold."
    )
    if equipment is not None:
        # N-1: server-resolved gauge identity — not client prose.
        description += f"\nMeasured with gauge {equipment.equipment_id} — {equipment.name}."
    if data.notes:
        description += f"\n\nOperator notes: {data.notes}"

    ncr = NonConformanceReport(
        ncr_number=generate_ncr_number(db, company_id),
        part_id=work_order.part_id,
        work_order_id=work_order.id,
        lot_number=work_order.lot_number,
        serial_number=serial_number,
        quantity_affected=1.0,
        source=NCRSource.IN_PROCESS,
        title=f"Out of tolerance: {step.label} ({identifier})"[:255],
        description=description,
        specification=specification,
        actual_value=str(measured_value)[:255],
        required_value=required_value,
        detected_by=user.id,
        detected_date=date.today(),
    )
    ncr.company_id = company_id
    db.add(ncr)
    db.flush()
    audit.log_create(
        "ncr",
        ncr.id,
        ncr.ncr_number,
        new_values=ncr,
        description=f"Auto-created NCR {ncr.ncr_number} from out-of-tolerance process step '{step.label}'",
        extra_data={
            "work_order_id": work_order.id,
            "work_order_operation_id": operation.id,
            "wo_operation_step_id": step.id,
            "measured_value": measured_value,
            "serial_number": serial_number,
            "source": source,
            # N-1: the server-resolved gauge identity (None when no gauge supplied).
            "equipment_id": equipment.id if equipment else None,
            "equipment_code": equipment.equipment_id if equipment else None,
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
        user_id=user.id,
        severity="high",
        event_payload={
            "ncr_number": ncr.ncr_number,
            "title": ncr.title,
            "source": NCRSource.IN_PROCESS.value,
            "step_label": step.label,
            "measured_value": measured_value,
        },
    )

    # The existing hold pathway: the blocker service flips the op ON_HOLD and audits
    # both the blocker creation and the status change (same as the kiosk hold flow).
    blocker = WorkOrderBlockerService(db).create_blocker(
        company_id=company_id,
        user=user,
        work_order_id=work_order.id,
        data=WorkOrderBlockerCreate(
            operation_id=operation.id,
            category=WorkOrderBlockerCategory.QUALITY_HOLD,
            severity=WorkOrderBlockerSeverity.HIGH,
            title=f"Quality hold: {step.label} out of tolerance ({ncr.ncr_number})"[:255],
            note=data.notes,
            ncr_id=ncr.id,
            put_operation_on_hold=True,
        ),
        audit=audit,
        source=source,
    )

    # Mirror PUT .../hold: a held operation accrues no labor — close open entries,
    # filling (never overwriting) their adoption-telemetry channel.
    now = datetime.utcnow()
    open_entries = (
        tenant_query(db, TimeEntry, company_id)
        .filter(TimeEntry.operation_id == operation.id, TimeEntry.clock_out.is_(None))
        .all()
    )
    for entry in open_entries:
        entry.clock_out = now
        if entry.clock_in:
            entry.duration_hours = (now - entry.clock_in).total_seconds() / 3600.0
        if source and entry.source is None:
            entry.source = source

    db.commit()
    db.refresh(ncr)
    db.refresh(blocker)
    return {
        "message": "Quality hold filed — NCR created and operation placed on hold",
        "ncr_id": ncr.id,
        "ncr_number": ncr.ncr_number,
        "blocker_id": blocker.id,
        "operation_id": operation.id,
        "operation_status": (operation.status.value if hasattr(operation.status, "value") else operation.status),
        "closed_time_entry_ids": [entry.id for entry in open_entries],
    }


# ---------- PR 3: completeness / gating / views ----------


def _record_satisfies(record: OperationStepRecord) -> bool:
    """A record satisfies its (step, serial) slot when live (non-superseded) and conforming.

    ``is_conforming`` is NULL for non-measurement types — NULL counts as conforming
    (there is no tolerance to violate); only an explicit False disqualifies.
    """
    return record.superseded_by_id is None and record.is_conforming is not False


def _live_records_for_operations(
    db: Session, company_id: int, operation_ids: Sequence[int]
) -> List[OperationStepRecord]:
    if not operation_ids:
        return []
    return (
        tenant_query(db, OperationStepRecord, company_id)
        .filter(
            OperationStepRecord.work_order_operation_id.in_(operation_ids),
            OperationStepRecord.superseded_by_id.is_(None),
        )
        .all()
    )


def _step_is_gating(step: WOOperationStep) -> bool:
    return bool(step.is_required) and step.step_type != StepType.INSTRUCTION.value


def _satisfied_keys(records: Iterable[OperationStepRecord]) -> set:
    return {(r.wo_operation_step_id, r.serial_number) for r in records if _record_satisfies(r)}


def _step_missing_serials(step: WOOperationStep, satisfied: set, serials: List[str]) -> List[str]:
    """Serials still missing a satisfying record for this step ([] when complete).

    Non-serialized WOs use the single ``None`` serial slot; a missing record surfaces
    as an empty ``serials`` list on the gate payload but ``step_is_complete`` is False.
    """
    if serials:
        return [s for s in serials if (step.id, s) not in satisfied]
    return []


def _step_is_complete(step: WOOperationStep, satisfied: set, serials: List[str]) -> bool:
    if serials:
        return all((step.id, s) in satisfied for s in serials)
    return (step.id, None) in satisfied


def missing_required_steps(
    db: Session, company_id: int, operation: WorkOrderOperation, work_order: WorkOrder
) -> List[Dict[str, Any]]:
    """Completion-gate predicate: required snapshot steps lacking live conforming records.

    Returns ``[]`` when the operation may complete (including the zero-step case —
    operations without snapshot steps complete exactly as before PR 3). Serialized WOs
    gate per serial: every serial needs its own record for every required step.
    """
    steps = (
        tenant_query(db, WOOperationStep, company_id)
        .filter(WOOperationStep.work_order_operation_id == operation.id)
        .order_by(WOOperationStep.sequence, WOOperationStep.id)
        .all()
    )
    gating = [step for step in steps if _step_is_gating(step)]
    if not gating:
        return []

    satisfied = _satisfied_keys(_live_records_for_operations(db, company_id, [operation.id]))
    serials = parse_work_order_serials(work_order)

    missing: List[Dict[str, Any]] = []
    for step in gating:
        if not _step_is_complete(step, satisfied, serials):
            missing.append(
                {
                    "step_id": step.id,
                    "label": step.label,
                    "serials": _step_missing_serials(step, satisfied, serials),
                }
            )
    return missing


def build_steps_view(
    db: Session, company_id: int, operation: WorkOrderOperation, work_order: WorkOrder
) -> Dict[str, Any]:
    """Assemble the kiosk steps view: ordered snapshot steps + live records + completeness."""
    steps = (
        tenant_query(db, WOOperationStep, company_id)
        .filter(WOOperationStep.work_order_operation_id == operation.id)
        .order_by(WOOperationStep.sequence, WOOperationStep.id)
        .all()
    )
    records = (
        tenant_query(db, OperationStepRecord, company_id)
        .filter(
            OperationStepRecord.work_order_operation_id == operation.id,
            OperationStepRecord.superseded_by_id.is_(None),
        )
        .options(joinedload(OperationStepRecord.recorder), joinedload(OperationStepRecord.equipment))
        .order_by(OperationStepRecord.recorded_at, OperationStepRecord.id)
        .all()
    )
    serials = parse_work_order_serials(work_order)
    satisfied = _satisfied_keys(records)

    records_by_step: Dict[int, List[OperationStepRecord]] = {}
    for record in records:
        record.recorded_by_name = _display_name(record.recorder)  # transient, read by the response schema
        record.gauge = equipment_ref(record.equipment)  # transient resolved-gauge echo (PR 4 addendum)
        records_by_step.setdefault(record.wo_operation_step_id, []).append(record)

    step_payloads: List[Dict[str, Any]] = []
    completeness: Dict[int, Dict[str, bool]] = {}
    gating_total = 0
    gating_recorded = 0
    for step in steps:
        complete = _step_is_complete(step, satisfied, serials)
        if _step_is_gating(step):
            gating_total += 1
            if complete:
                gating_recorded += 1
        if serials:
            completeness[step.id] = {serial: (step.id, serial) in satisfied for serial in serials}
        step_payloads.append(
            {
                "id": step.id,
                "work_order_operation_id": step.work_order_operation_id,
                "source_sheet_id": step.source_sheet_id,
                "source_sheet_revision": step.source_sheet_revision,
                "sequence": step.sequence,
                "label": step.label,
                "instruction_text": step.instruction_text,
                "step_type": step.step_type,
                "is_required": step.is_required,
                "config": step.config,
                "requires_gauge": step.requires_gauge,
                "spc_characteristic_id": step.spc_characteristic_id,
                "created_at": step.created_at,
                "records": records_by_step.get(step.id, []),
                "complete": complete,
                "missing_serials": _step_missing_serials(step, satisfied, serials),
            }
        )

    return {
        "operation_id": operation.id,
        "work_order_id": work_order.id,
        "work_order_number": work_order.work_order_number,
        "operation_status": (operation.status.value if hasattr(operation.status, "value") else operation.status),
        "is_serialized": bool(serials),
        "serial_numbers": serials,
        "steps": step_payloads,
        "steps_total": gating_total,
        "steps_recorded": gating_recorded,
        "completeness": completeness,
    }


def step_counts_for_operations(
    db: Session, company_id: int, operations: Sequence[WorkOrderOperation]
) -> Dict[int, Dict[str, int]]:
    """Lightweight per-operation gating-step counts for the work-center queue chip.

    ``{operation_id: {"steps_total": n, "steps_recorded": m}}`` counting REQUIRED
    (non-INSTRUCTION) snapshot steps; a step only counts as recorded when its live
    conforming records cover every serial on a serialized WO. Two bulk queries total.
    """
    counts = {op.id: {"steps_total": 0, "steps_recorded": 0} for op in operations}
    if not counts:
        return counts

    steps = (
        tenant_query(db, WOOperationStep, company_id)
        .filter(WOOperationStep.work_order_operation_id.in_(list(counts.keys())))
        .all()
    )
    gating = [step for step in steps if _step_is_gating(step)]
    if not gating:
        return counts

    satisfied = _satisfied_keys(_live_records_for_operations(db, company_id, list(counts.keys())))
    serials_by_operation = {op.id: parse_work_order_serials(op.work_order) for op in operations if op.work_order}
    for step in gating:
        bucket = counts.get(step.work_order_operation_id)
        if bucket is None:
            continue
        bucket["steps_total"] += 1
        if _step_is_complete(step, satisfied, serials_by_operation.get(step.work_order_operation_id, [])):
            bucket["steps_recorded"] += 1
    return counts


def gated_operation_ids(db: Session, operations: Sequence[WorkOrderOperation]) -> set:
    """Bulk gate check: ids of operations whose required steps lack conforming records.

    Used by the read-time reconcile (``work_order_state_service``) so closed-TimeEntry
    evidence at target can never auto-complete an operation the /complete endpoints
    would refuse — otherwise the completion gate would be undone by the next WO page
    load. Operations are grouped by their own ``company_id`` (reconcile inputs are
    already tenant-scoped rows) and each group reuses the tenant-scoped bulk count
    queries — at most two queries per company present.
    """
    gated: set = set()
    by_company: Dict[int, List[WorkOrderOperation]] = {}
    for op in operations:
        if op.id is not None and op.company_id is not None:
            by_company.setdefault(op.company_id, []).append(op)
    for company_id, company_ops in by_company.items():
        counts = step_counts_for_operations(db, company_id, company_ops)
        for op_id, count in counts.items():
            if count["steps_recorded"] < count["steps_total"]:
                gated.add(op_id)
    return gated


# ---------- PR 3: PHOTO/FILE evidence upload ----------


def store_step_attachment(
    db: Session,
    company_id: int,
    *,
    work_order: WorkOrder,
    operation: WorkOrderOperation,
    step: WOOperationStep,
    content: bytes,
    filename: Optional[str],
    content_type: Optional[str],
    user: User,
    audit: AuditService,
) -> Document:
    """Persist PHOTO/FILE step evidence as a QUALITY_RECORD Document (StorageBackend).

    Exists because kiosk-scoped operator tokens are path-fenced to ``/shop-floor`` and
    cannot reach ``/documents/upload`` — this is the in-fence evidence path. Mirrors the
    receiving-label Document persistence (``print_service._store_label_document``).
    """
    if step.step_type not in (StepType.PHOTO.value, StepType.FILE.value):
        raise HTTPException(status_code=400, detail="Only PHOTO and FILE steps take attachments")
    if not content:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")
    if len(content) > MAX_STEP_ATTACHMENT_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Attachment exceeds the {MAX_STEP_ATTACHMENT_BYTES // (1024 * 1024)} MB limit",
        )
    allowed = ALLOWED_PHOTO_MIME_TYPES if step.step_type == StepType.PHOTO.value else ALLOWED_FILE_MIME_TYPES
    normalized_type = (content_type or "").split(";")[0].strip().lower()
    if normalized_type not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported attachment type '{normalized_type or 'unknown'}' for a {step.step_type} step",
        )

    storage = get_storage()
    unique_name = f"{uuid.uuid4()}{sanitize_ext(filename)}"
    if storage.is_remote:
        key = f"{company_id}/step-evidence/{unique_name}"
    else:
        key = os.path.join(resolve_upload_dir(), unique_name)
    file_path = storage.save(content, key=key)

    document = Document(
        document_number=generate_document_number(db, DocumentType.QUALITY_RECORD.value),
        revision="A",
        title=f"Step evidence — {_record_identifier(work_order, operation, step)}",
        description=step.label,
        document_type=DocumentType.QUALITY_RECORD,
        work_order_id=work_order.id,
        file_name=filename,
        file_path=file_path,
        file_size=len(content),
        mime_type=normalized_type,
        status="released",
        created_by=user.id,
    )
    document.company_id = company_id
    db.add(document)
    db.flush()
    audit.log_create(
        "document",
        document.id,
        document.document_number,
        new_values=document,
        description=f"Uploaded step evidence for {_record_identifier(work_order, operation, step)}",
        extra_data={
            "work_order_id": work_order.id,
            "work_order_operation_id": operation.id,
            "wo_operation_step_id": step.id,
            "file_size": len(content),
            "mime_type": normalized_type,
        },
    )
    db.commit()
    db.refresh(document)
    return document


# ---------- PR 4: FAI pre-fill from step records ----------


def _parse_float(value: Any) -> Optional[float]:
    """Best-effort float parse of the FAI characteristic's String spec columns."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    try:
        return float(str(value).strip())
    except ValueError:
        return None


def _fai_spec_mismatch(char: FAICharacteristic, config: Dict[str, Any]) -> Optional[str]:
    """Reason the characteristic's nominal/tolerances CONTRADICT the step config, or None.

    Comparison is best-effort by design (the FAI columns are free-text strings): a
    blank or unparseable side is treated as not-comparable and does not block the
    label match — only a parseable, materially different value refuses the pre-fill.
    """
    char_nominal = _parse_float(char.nominal)
    config_nominal = config.get("nominal") if _is_number(config.get("nominal")) else None
    if char_nominal is not None and config_nominal is not None and abs(char_nominal - config_nominal) > 1e-9:
        return f"nominal mismatch (characteristic {char.nominal} vs step {config_nominal})"

    lsl = config.get("lsl") if _is_number(config.get("lsl")) else None
    usl = config.get("usl") if _is_number(config.get("usl")) else None
    if char_nominal is not None:
        tol_plus = _parse_float(char.tolerance_plus)
        if tol_plus is not None and usl is not None and abs((char_nominal + abs(tol_plus)) - usl) > 1e-9:
            return f"tolerance mismatch (characteristic USL {char_nominal + abs(tol_plus):g} vs step {usl})"
        tol_minus = _parse_float(char.tolerance_minus)
        if tol_minus is not None and lsl is not None and abs((char_nominal - abs(tol_minus)) - lsl) > 1e-9:
            return f"tolerance mismatch (characteristic LSL {char_nominal - abs(tol_minus):g} vs step {lsl})"
    return None


def prefill_fai_from_step_records(
    db: Session, company_id: int, *, fai_id: int, user: User, audit: AuditService
) -> Dict[str, Any]:
    """Pre-fill AS9102 FAI characteristics from the WO's conforming measurement records (PR 4).

    For a FAI linked to a work order: each characteristic with no ``actual_value`` yet
    is matched to a live (non-superseded) CONFORMING measurement step record by the v1
    heuristic — characteristic description == step label (trimmed, case-insensitive) —
    cross-checked against the snapshot config (a parseable nominal/tolerance that
    CONTRADICTS the step config refuses the match). Matches copy the LATEST record's
    value into ``actual_value`` and the gauge's name into ``measuring_device``.

    Never guesses: ambiguous labels (two measurement steps sharing one label), spec
    contradictions, already-recorded characteristics and label misses are all reported
    in ``unmatched`` with a reason instead of being filled. When the FAI carries a
    ``serial_number``, only that serial's records are considered. ``is_conforming`` is
    NOT set here — the inspector still disposition each characteristic through the
    existing FAI update endpoint. Audited; commits (owns its unit of work).
    """
    fai = (
        tenant_query(db, FirstArticleInspection, company_id)
        .options(selectinload(FirstArticleInspection.characteristics))
        .filter(FirstArticleInspection.id == fai_id)
        .first()
    )
    if not fai:
        raise HTTPException(status_code=404, detail="FAI not found")
    if fai.work_order_id is None:
        raise HTTPException(status_code=400, detail="This FAI is not linked to a work order — nothing to pre-fill from")
    work_order = (
        tenant_query(db, WorkOrder, company_id)
        .filter(WorkOrder.id == fai.work_order_id, WorkOrder.is_deleted == False)  # noqa: E712
        .first()
    )
    if not work_order:
        raise HTTPException(status_code=404, detail="Work order not found for this FAI")

    operation_ids = [
        row.id
        for row in tenant_query(db, WorkOrderOperation, company_id)
        .filter(WorkOrderOperation.work_order_id == work_order.id)
        .all()
    ]

    rows: List[Tuple[OperationStepRecord, WOOperationStep]] = []
    if operation_ids:
        query = (
            db.query(OperationStepRecord, WOOperationStep)
            .join(WOOperationStep, OperationStepRecord.wo_operation_step_id == WOOperationStep.id)
            .filter(
                OperationStepRecord.company_id == company_id,
                WOOperationStep.company_id == company_id,
                OperationStepRecord.work_order_operation_id.in_(operation_ids),
                OperationStepRecord.superseded_by_id.is_(None),
                OperationStepRecord.is_conforming.is_(True),
                WOOperationStep.step_type == StepType.MEASUREMENT.value,
            )
            .order_by(OperationStepRecord.recorded_at, OperationStepRecord.id)
        )
        if fai.serial_number:
            query = query.filter(OperationStepRecord.serial_number == fai.serial_number)
        rows = query.all()

    # label -> {step_id -> (step, latest record)}; two DIFFERENT steps sharing a label
    # make the label ambiguous (never guess which balloon a value belongs to).
    by_label: Dict[str, Dict[int, Tuple[WOOperationStep, OperationStepRecord]]] = {}
    for record, step in rows:
        by_label.setdefault(step.label.strip().lower(), {})[step.id] = (step, record)

    equipment_ids = {record.equipment_id for record, _ in rows if record.equipment_id}
    equipment_names: Dict[int, str] = {}
    if equipment_ids:
        equipment_names = {
            equipment.id: equipment.name
            for equipment in tenant_query(db, Equipment, company_id).filter(Equipment.id.in_(equipment_ids)).all()
        }

    prefilled: List[Dict[str, Any]] = []
    unmatched: List[Dict[str, Any]] = []
    old_devices: Dict[str, Any] = {}  # SF-2: measuring_device audit pairs (chars we SET)
    new_devices: Dict[str, Any] = {}
    for char in sorted(fai.characteristics, key=lambda c: (c.char_number or 0, c.id)):

        def _skip(reason: str) -> None:
            unmatched.append({"char_number": char.char_number, "characteristic": char.characteristic, "reason": reason})

        if char.actual_value is not None and str(char.actual_value).strip():
            _skip("actual_value already recorded — not overwritten")
            continue
        candidates = by_label.get((char.characteristic or "").strip().lower())
        if not candidates:
            _skip("no conforming measurement step record with a matching label")
            continue
        if len(candidates) > 1:
            _skip("ambiguous — multiple measurement steps share this label")
            continue
        step, record = next(iter(candidates.values()))
        mismatch = _fai_spec_mismatch(char, step.config or {})
        if mismatch:
            _skip(mismatch)
            continue

        # ``.10g`` (not bare ``g``): the default 6 significant digits would silently
        # truncate a recorded value like 1234.5678 on the AS9102 form.
        char.actual_value = f"{record.value_numeric:.10g}"[:100]
        # SF-2 (compliance audit): measuring_device is only WRITTEN when currently
        # blank — an inspector-entered device is never overwritten by the pre-fill
        # (mirrors the actual_value never-overwrite rule). ``device_preserved`` is the
        # honest report: True when an existing device was kept.
        gauge_name = equipment_names.get(record.equipment_id) if record.equipment_id else None
        device_preserved = bool(char.measuring_device and str(char.measuring_device).strip())
        if gauge_name and not device_preserved:
            old_devices[str(char.char_number)] = char.measuring_device
            char.measuring_device = gauge_name[:255]
            new_devices[str(char.char_number)] = char.measuring_device
        prefilled.append(
            {
                "char_number": char.char_number,
                "characteristic": char.characteristic,
                "actual_value": char.actual_value,
                "measuring_device": char.measuring_device,
                "device_preserved": device_preserved,
                "wo_operation_step_id": step.id,
                "record_id": record.id,
                "serial_number": record.serial_number,
            }
        )

    if prefilled:
        old_values: Dict[str, Any] = {"actual_values": {str(e["char_number"]): None for e in prefilled}}
        new_values: Dict[str, Any] = {"actual_values": {str(e["char_number"]): e["actual_value"] for e in prefilled}}
        if new_devices:
            # SF-2: device changes are part of the tamper-evident diff, not just prose.
            old_values["measuring_devices"] = old_devices
            new_values["measuring_devices"] = new_devices
        audit.log_update(
            "fai",
            fai.id,
            fai.fai_number,
            old_values=old_values,
            new_values=new_values,
            description=(
                f"Pre-filled {len(prefilled)} FAI characteristic(s) on {fai.fai_number} from process-sheet "
                f"step records of WO {work_order.work_order_number}"
            ),
            extra_data={"work_order_id": work_order.id, "prefilled": prefilled, "unmatched": unmatched},
        )
    db.commit()

    return {
        "fai_id": fai.id,
        "fai_number": fai.fai_number,
        "work_order_id": work_order.id,
        "prefilled": prefilled,
        "unmatched": unmatched,
        "prefilled_count": len(prefilled),
        "unmatched_count": len(unmatched),
    }
