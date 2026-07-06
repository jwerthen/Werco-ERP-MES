"""Process Sheets — typed, revision-controlled operation steps (AS9100D objective evidence).

Library model (PR 1 of docs/PROCESS_SHEETS_SCOPE.md):

- ``ProcessSheet`` — the reusable, revision-controlled library entity. Lifecycle mirrors
  ``Routing`` exactly (draft → released → obsolete, revision letters as separate rows
  sharing ``sheet_number``).
- ``ProcessSheetStep`` — typed step definitions on a sheet (measurement/checkbox/list/...).
- ``WOOperationStep`` — the immutable per-work-order snapshot of a step definition, copied
  at WO creation (PR 3 populates it; routing changes never mutate open WOs).
- ``OperationStepRecord`` — append-only captured evidence. Corrections are NEW records
  linked via ``superseded_by_id``; rows are never updated or deleted (traceability
  invariant — no SoftDeleteMixin on purpose).

Status/type columns are plain strings carrying the co-located str-enum values (house
pattern — adding a value never needs an ALTER TYPE).
"""

import enum
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.db.database import Base
from app.db.mixins import OptimisticLockMixin, SoftDeleteMixin, TenantMixin


class ProcessSheetStatus(str, enum.Enum):
    DRAFT = "draft"
    RELEASED = "released"
    OBSOLETE = "obsolete"


class StepType(str, enum.Enum):
    """Typed step kinds. INSTRUCTION is display-only (never required, no record)."""

    MEASUREMENT = "measurement"
    CHECKBOX = "checkbox"
    LIST = "list"
    VALUE = "value"
    PHOTO = "photo"
    FILE = "file"
    INSTRUCTION = "instruction"


class ProcessSheet(Base, TenantMixin, SoftDeleteMixin, OptimisticLockMixin):
    """Reusable process-sheet library entity (revision-controlled, per company).

    Revisions are separate rows sharing ``sheet_number`` — same pattern as routing
    revisions. Only DRAFT sheets are mutable; RELEASED content changes require a new
    revision (``process_sheet_service.new_revision``).
    """

    __tablename__ = "process_sheets"
    __table_args__ = (
        UniqueConstraint("company_id", "sheet_number", "revision", name="uq_process_sheets_company_number_revision"),
        Index("ix_process_sheets_company_status", "company_id", "status"),
        Index("ix_process_sheets_company_number", "company_id", "sheet_number"),
    )

    id = Column(Integer, primary_key=True, index=True)

    sheet_number = Column(String(50), nullable=False, index=True)  # auto "PS-000123", shared across revisions
    title = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)

    revision = Column(String(20), nullable=False, default="A")
    status = Column(String(20), nullable=False, default=ProcessSheetStatus.DRAFT.value, index=True)
    effective_date = Column(DateTime(timezone=True), nullable=True)  # set on release
    obsolete_date = Column(DateTime(timezone=True), nullable=True)  # set on obsolete
    is_active = Column(Boolean, nullable=False, default=True)

    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    updated_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    # updated_at + version come from OptimisticLockMixin

    steps = relationship(
        "ProcessSheetStep",
        back_populates="process_sheet",
        cascade="all, delete-orphan",
        order_by="ProcessSheetStep.sequence",
    )


class ProcessSheetStep(Base, TenantMixin):
    """Typed step definition on a process sheet.

    ``config`` shape per type (validated in process_sheet_service):
    measurement ``{nominal, lsl, usl, unit, decimals}``; list ``{options: []}``;
    photo/file ``{hint}``. INSTRUCTION steps are display-only and never required.
    ``requires_gauge`` is only valid on MEASUREMENT steps.
    """

    __tablename__ = "process_sheet_steps"
    __table_args__ = (Index("ix_process_sheet_steps_company_sheet", "company_id", "process_sheet_id"),)

    id = Column(Integer, primary_key=True, index=True)
    process_sheet_id = Column(Integer, ForeignKey("process_sheets.id"), nullable=False, index=True)

    sequence = Column(Integer, nullable=False)  # 10, 20, 30 convention (same as operations)
    label = Column(String(255), nullable=False)
    instruction_text = Column(Text, nullable=True)

    step_type = Column(String(20), nullable=False)  # StepType values
    is_required = Column(Boolean, nullable=False, default=True)  # gates operation completion (PR 3)
    config = Column(JSON, nullable=True)
    requires_gauge = Column(Boolean, nullable=False, default=False)  # MEASUREMENT only
    spc_characteristic_id = Column(Integer, ForeignKey("spc_characteristics.id"), nullable=True, index=True)

    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    process_sheet = relationship("ProcessSheet", back_populates="steps")
    spc_characteristic = relationship("SPCCharacteristic")


class WOOperationStep(Base, TenantMixin):
    """Immutable snapshot of a step definition on a work-order operation (the traveler).

    Copied from the RELEASED sheet at WO creation (PR 3 populates this inside
    ``create_routing_operations_for_work_order``) — routing/sheet changes never mutate
    open WOs. ``source_sheet_id``/``source_sheet_revision`` preserve traceability back to
    the released sheet. Model + table only in PR 1; no behavior yet.
    """

    __tablename__ = "wo_operation_steps"
    __table_args__ = (Index("ix_wo_operation_steps_company_operation", "company_id", "work_order_operation_id"),)

    id = Column(Integer, primary_key=True, index=True)
    work_order_operation_id = Column(Integer, ForeignKey("work_order_operations.id"), nullable=False, index=True)

    # Traceability back to the released library sheet this snapshot was taken from.
    source_sheet_id = Column(Integer, ForeignKey("process_sheets.id"), nullable=False, index=True)
    source_sheet_revision = Column(String(20), nullable=False)

    # Snapshot copies of the step-definition columns (see ProcessSheetStep).
    sequence = Column(Integer, nullable=False)
    label = Column(String(255), nullable=False)
    instruction_text = Column(Text, nullable=True)
    step_type = Column(String(20), nullable=False)  # StepType values
    is_required = Column(Boolean, nullable=False, default=True)
    config = Column(JSON, nullable=True)
    requires_gauge = Column(Boolean, nullable=False, default=False)
    spc_characteristic_id = Column(Integer, ForeignKey("spc_characteristics.id"), nullable=True, index=True)

    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)

    work_order_operation = relationship("WorkOrderOperation")
    source_sheet = relationship("ProcessSheet")
    spc_characteristic = relationship("SPCCharacteristic")


class OperationStepRecord(Base, TenantMixin):
    """Append-only captured evidence for a snapshot step (AS9100D objective evidence).

    APPEND-ONLY: rows are never updated or deleted. A correction is a NEW record; the
    superseded row gets ``superseded_by_id`` + ``supersede_reason`` stamped exactly once
    (PR 3's supersede endpoint). No SoftDeleteMixin on purpose — evidence integrity.
    """

    __tablename__ = "operation_step_records"
    __table_args__ = (
        Index("ix_operation_step_records_company_operation", "company_id", "work_order_operation_id"),
        Index("ix_operation_step_records_company_step_serial", "company_id", "wo_operation_step_id", "serial_number"),
    )

    id = Column(Integer, primary_key=True, index=True)
    wo_operation_step_id = Column(Integer, ForeignKey("wo_operation_steps.id"), nullable=False, index=True)
    # Denormalized for cheap completion-gating queries (PR 3).
    work_order_operation_id = Column(Integer, ForeignKey("work_order_operations.id"), nullable=False, index=True)

    # Required when the WO carries serials; validated against WorkOrder.serial_numbers (PR 3).
    serial_number = Column(String(100), nullable=True)

    # One populated per step type (measurement→numeric, checkbox→bool, list/value→text, ...).
    value_text = Column(Text, nullable=True)
    value_numeric = Column(Float, nullable=True)
    value_bool = Column(Boolean, nullable=True)
    is_conforming = Column(Boolean, nullable=True)  # server-computed for measurements

    recorded_by = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    recorded_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)
    # Adoption-telemetry channel — same value vocabulary as TimeEntry.source (TimeEntrySource);
    # plain nullable String: NULL = not reported, never a guessed default.
    source = Column(String(20), nullable=True)

    equipment_id = Column(Integer, ForeignKey("equipment.id"), nullable=True, index=True)  # gauge used
    qualification_snapshot = Column(JSON, nullable=True)  # warn-and-record cert/skill result at capture
    attachment_document_id = Column(Integer, ForeignKey("documents.id"), nullable=True, index=True)

    # Correction chain: the replacing record's id + why. Stamped once; never cleared.
    superseded_by_id = Column(Integer, ForeignKey("operation_step_records.id"), nullable=True, index=True)
    supersede_reason = Column(String(255), nullable=True)

    created_at = Column(DateTime(timezone=True), nullable=False, default=datetime.utcnow)

    wo_operation_step = relationship("WOOperationStep")
    work_order_operation = relationship("WorkOrderOperation")
    recorder = relationship("User", foreign_keys=[recorded_by])
    equipment = relationship("Equipment")
    attachment_document = relationship("Document")
    superseded_by = relationship("OperationStepRecord", remote_side=[id], foreign_keys=[superseded_by_id])
