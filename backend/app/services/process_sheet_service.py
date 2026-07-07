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
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from fastapi import HTTPException
from sqlalchemy.orm import Session, joinedload, selectinload

from app.db.locks import acquire_generator_lock
from app.db.tenant_filter import tenant_query
from app.models.calibration import Equipment
from app.models.document import Document, DocumentType
from app.models.process_sheet import (
    OperationStepRecord,
    ProcessSheet,
    ProcessSheetStatus,
    ProcessSheetStep,
    StepType,
    WOOperationStep,
)
from app.models.spc import SPCCharacteristic
from app.models.user import User
from app.models.work_order import WorkOrder, WorkOrderOperation
from app.schemas.process_sheet import (
    OperationStepRecordCreate,
    OperationStepRecordSupersede,
    ProcessSheetCreate,
    ProcessSheetStepCreate,
    ProcessSheetStepUpdate,
    ProcessSheetUpdate,
)
from app.services.audit_service import AuditService
from app.services.storage_service import get_storage, resolve_upload_dir, sanitize_ext

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


def parse_work_order_serials(work_order: WorkOrder) -> List[str]:
    """Parse ``WorkOrder.serial_numbers`` (JSON Text) into a list, guarding non-JSON values.

    Same defensive shape as ``coc_service._parse_serial_numbers``: a WO is "serialized"
    exactly when this returns a non-empty list.
    """
    raw = work_order.serial_numbers
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
) -> OperationStepRecord:
    """Run the capture validation ladder and return the (unflushed) record row.

    Ladder (order matters — the callers already enforced WO-not-terminal /
    operation-IN_PROGRESS and that the step belongs to the operation):
      1. INSTRUCTION steps take no records (400).
      2. serialized WO -> serial_number required and must be one of the WO's serials;
         non-serialized -> serial_number must be absent (400).
      3. type-shaped value (exactly the fields the step type takes; 400 otherwise);
         MEASUREMENT values are rounded per config ``decimals`` before storing.
      4. MEASUREMENT conformance from the SNAPSHOT lsl/usl — out-of-tolerance is
         REFUSED with 409 ``OUT_OF_TOLERANCE`` and NO row (hold+NCR or a corrected
         re-measurement are the only paths forward; the NCR one-tap lands in PR 4).
         CHECKBOX conformance is the checkbox itself (``is_conforming = value_bool``):
         a False record is honest evidence that never satisfies the completion gate.
      5. ``equipment_id`` is an optional tenant-validated passthrough this PR
         (calibration-currency enforcement is PR 4).
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

    if equipment_id is not None:
        equipment = tenant_query(db, Equipment, company_id).filter(Equipment.id == equipment_id).first()
        if not equipment:
            raise HTTPException(status_code=404, detail="Equipment not found")

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
        equipment_id=equipment_id,
        attachment_document_id=attachment_document_id,
    )


def _record_identifier(work_order: WorkOrder, operation: WorkOrderOperation, step: WOOperationStep) -> str:
    return f"WO {work_order.work_order_number} {operation.operation_number or operation.sequence} step {step.sequence}"


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
    """Capture one step record (append-only objective evidence). Audited before commit."""
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
        attachment_document_id=data.attachment_document_id,
        recorded_by=user.id,
        source=source,
    )
    db.add(record)
    db.flush()
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
        },
    )
    db.commit()
    db.refresh(record)
    record.recorded_by_name = _display_name(user)  # transient, read by the response schema
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
        attachment_document_id=data.attachment_document_id,
        recorded_by=user.id,
        source=source,
    )
    db.add(replacement)
    db.flush()

    # The ONE permitted mutation of an existing record: stamp the correction chain.
    old.superseded_by_id = replacement.id
    old.supersede_reason = data.reason

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
    return replacement


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
        .options(joinedload(OperationStepRecord.recorder))
        .order_by(OperationStepRecord.recorded_at, OperationStepRecord.id)
        .all()
    )
    serials = parse_work_order_serials(work_order)
    satisfied = _satisfied_keys(records)

    records_by_step: Dict[int, List[OperationStepRecord]] = {}
    for record in records:
        record.recorded_by_name = _display_name(record.recorder)  # transient, read by the response schema
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


def _generate_document_number(db: Session, doc_type: str) -> str:
    """Sequential document number under the global advisory lock (print_service precedent).

    ``document_number`` is a globally-unique column and the scan is intentionally
    unscoped, so the lock is global too — concurrent kiosk uploads can't compute the
    same number and collide on the unique constraint.
    """
    acquire_generator_lock(db, "document_number")
    prefix = doc_type[:3].upper()
    today = datetime.now().strftime("%Y%m")
    last_doc = (
        db.query(Document)
        .filter(Document.document_number.like(f"{prefix}-{today}-%"))
        .order_by(Document.document_number.desc())
        .first()
    )
    new_num = 1
    if last_doc:
        try:
            new_num = int(last_doc.document_number.split("-")[-1]) + 1
        except (ValueError, IndexError):
            new_num = 1
    return f"{prefix}-{today}-{new_num:04d}"


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
        document_number=_generate_document_number(db, DocumentType.QUALITY_RECORD.value),
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
