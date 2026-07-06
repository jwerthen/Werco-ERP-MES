"""Process Sheets library business logic (PR 1 of docs/PROCESS_SHEETS_SCOPE.md).

Owns the sheet lifecycle (draft -> released -> obsolete + new revisions), sheet-number
generation, and the per-type step-definition validation. Each mutating function owns its
unit of work (commits at the end) and writes the tamper-evident audit row BEFORE the
terminal commit so the state change and its audit trail commit atomically (AuditService
only flushes; a request session never commits on teardown).

Invariants enforced here:
- Only DRAFT sheets are mutable (sheet fields, step CRUD, delete). Anything else -> 409.
- INSTRUCTION steps are never required; ``requires_gauge`` is MEASUREMENT-only.
- MEASUREMENT config needs numeric lsl/nominal/usl with lsl <= nominal <= usl, lsl < usl.
- LIST config needs a non-empty ``options`` array.
- ``spc_characteristic_id`` is MEASUREMENT-only and must resolve in the active company.
- All queries tenant-scoped via tenant_query(); soft delete only, never physical.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session, selectinload

from app.db.locks import acquire_generator_lock
from app.db.tenant_filter import tenant_query
from app.models.process_sheet import ProcessSheet, ProcessSheetStatus, ProcessSheetStep, StepType
from app.models.spc import SPCCharacteristic
from app.models.user import User
from app.schemas.process_sheet import (
    ProcessSheetCreate,
    ProcessSheetStepCreate,
    ProcessSheetStepUpdate,
    ProcessSheetUpdate,
)
from app.services.audit_service import AuditService

SHEET_NUMBER_PREFIX = "PS-"


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
