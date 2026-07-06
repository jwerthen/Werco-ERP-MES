"""Create process-sheet tables + attach/traceability FK columns (typed op steps, PR 1)

Revision ID: 058_process_sheets
Revises: 057_kiosk_stations
Create Date: 2026-07-06

Context
-------
Process Sheets library (branch ``feat/process-sheets-library``, PR 1 of
docs/PROCESS_SHEETS_SCOPE.md) adds four new tenant-scoped tables, all in
``app/models/process_sheet.py``:

  * ``process_sheets`` -- ``ProcessSheet``. The reusable, revision-controlled
    library entity (draft -> released -> obsolete; revisions are separate rows
    sharing ``sheet_number``, same pattern as ``routings``). TenantMixin +
    SoftDeleteMixin + OptimisticLockMixin.
  * ``process_sheet_steps`` -- ``ProcessSheetStep``. Typed step definitions on
    a sheet (measurement/checkbox/list/value/photo/file/instruction).
  * ``wo_operation_steps`` -- ``WOOperationStep``. The immutable per-WO
    snapshot of a step definition, copied at WO creation (PR 3 populates it;
    table only here).
  * ``operation_step_records`` -- ``OperationStepRecord``. APPEND-ONLY captured
    evidence (AS9100D objective evidence). Deliberately NO SoftDeleteMixin:
    corrections are NEW records chained via ``superseded_by_id``; rows are
    never updated or deleted.

Plus three NULLABLE FK columns on existing tenant tables (each indexed):

  * ``routing_operations.process_sheet_id`` -> ``process_sheets.id``
    (attach a released sheet to a routing operation; app/models/routing.py)
  * ``spc_measurements.operation_id`` -> ``work_order_operations.id``
    (step-level SPC traceability, populated in PR 4; app/models/spc.py)
  * ``work_order_blockers.ncr_id`` -> ``ncrs.id``
    (QUALITY_HOLD <-> NCR link, PR 4; app/models/work_order_blocker.py)

This migration writes no data, backfills nothing, and never touches the
tamper-evident ``audit_log`` table. Status/type columns are plain VARCHARs
carrying the co-located str-enum values (house pattern) -- no enum types, so
no ``ALTER TYPE`` on either path.

Tenant / compliance shape
-------------------------
All four tables: TenantMixin -> ``company_id`` Integer FK ``companies.id``
NOT NULL, indexed (``ix_<table>_company_id``); mixin columns are appended
after the class's own columns, so ``company_id`` sits after the declared
columns (and, on ``process_sheets``, before the SoftDelete/OptimisticLock
mixin columns -- MRO order; verified against ``Base.metadata``). Every query
against these tables MUST be company-scoped. ``process_sheets`` keeps the
full soft-delete column set (``is_deleted``/``deleted_at``/``deleted_by``);
nothing here hard-deletes rows. ``operation_step_records`` omits soft-delete
ON PURPOSE (evidence-integrity invariant -- see the model docstring).

Server defaults mirror the mixins byte-for-byte (create_all parity):
``is_deleted`` DEFAULT 'false', ``version`` DEFAULT '1', ``updated_at``
DEFAULT 'now()' -- only on ``process_sheets``. Every other column carries an
app-side default only (NO server default), matching the models: quantities
like ``is_active``/``is_required``/``requires_gauge`` are NOT NULL with
Python-side defaults; all DateTimes here are ``timezone=True`` (unlike 057's
naive columns -- these models declare tz-aware). The three added FK columns
are nullable with no default and no backfill: NULL means "not attached / not
recorded" on all historical rows, never a guessed value, so no
backfill-then-NOT-NULL dance is needed.

Idempotent and reversible
-------------------------
Bootstrap is ``create_all() -> stamp -> upgrade`` (docs/DEVELOPMENT.md), NOT a
bare ``upgrade head`` on an empty DB, so a DB bootstrapped from the updated
models already has all of this when the migration runs over the stamp.

- Upgrade guards every ``create_table`` with ``_has_table``, every
  ``create_index`` with ``_has_index``, and every ``add_column`` with
  ``_has_column`` (precedent 046/048/050/055/056/057): bootstrapped DBs and
  re-runs are clean no-ops.
- Downgrade reverses in strict dependency order: the three added columns
  first (index then column, guarded -- ``routing_operations.process_sheet_id``
  references ``process_sheets`` so it must go before the table), then the
  tables children-first (``operation_step_records`` -> ``wo_operation_steps``
  -> ``process_sheet_steps`` -> ``process_sheets``), indexes before each
  table, all guarded.
- Dialects: table creates/drops and index ops are dialect-agnostic. The
  three FK-column add/drops branch on dialect -- plain ALTERs on Postgres
  (ADD COLUMN + ADD CONSTRAINT up, metadata-only DROP COLUMN down), but
  ``batch_alter_table`` (copy-and-move recreate) on SQLite ONLY, in BOTH
  directions: SQLite can neither ALTER-add an FK constraint (alembic raises
  ``NotImplementedError``) nor DROP COLUMN a column named in a table-level
  ``FOREIGN KEY`` clause, which is exactly what a create_all-bootstrapped DB
  has (verified: plain drop fails with "unknown column ... in foreign key
  definition"). Batch mode is confined to SQLite (local dev / pytest scale)
  so Postgres never pays a table rewrite.

Locking / operations note
-------------------------
The four CREATE TABLEs are brand-new empty tables: instantaneous, no lock on
any existing table. The three ADD COLUMNs are nullable with no default =
metadata-only on PostgreSQL (brief ACCESS EXCLUSIVE catalog update, no
rewrite); each is followed by an ADD FOREIGN KEY whose validation scan is
trivial because every row is NULL in the brand-new column. The three new
single-column indexes DO scan their tables while
holding a SHARE lock (writes blocked for the build); ``routing_operations``
and ``work_order_blockers`` are modest, ``spc_measurements`` is the largest
of the three -- at current scale this is seconds, but if it ever grows large,
build ``ix_spc_measurements_operation_id`` CONCURRENTLY out-of-band and let
the guarded ``create_index`` no-op. No deploy-ordering constraint beyond the
usual: old code ignores all of this; new code (the process-sheets endpoints)
must not ship before this migration runs.

Revision id ``058_process_sheets`` is 18 chars (<= 32) per the
create_all -> stamp -> upgrade bootstrap constraint (alembic_version.version_num
is varchar(32) on a freshly bootstrapped DB); see docs/DEVELOPMENT.md.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "058_process_sheets"
down_revision = "057_kiosk_stations"
branch_labels = None
depends_on = None

# Per-table (index_name, columns, unique), mirroring index=True / TenantMixin /
# SoftDeleteMixin declarations plus the explicit __table_args__ composites, so
# create_all and upgrade converge and autogenerate stays quiet.
INDEXES = {
    "process_sheets": [
        ("ix_process_sheets_company_id", ["company_id"], False),
        ("ix_process_sheets_company_number", ["company_id", "sheet_number"], False),
        ("ix_process_sheets_company_status", ["company_id", "status"], False),
        ("ix_process_sheets_id", ["id"], False),
        ("ix_process_sheets_is_deleted", ["is_deleted"], False),
        ("ix_process_sheets_sheet_number", ["sheet_number"], False),
        ("ix_process_sheets_status", ["status"], False),
    ],
    "process_sheet_steps": [
        ("ix_process_sheet_steps_company_id", ["company_id"], False),
        ("ix_process_sheet_steps_company_sheet", ["company_id", "process_sheet_id"], False),
        ("ix_process_sheet_steps_id", ["id"], False),
        ("ix_process_sheet_steps_process_sheet_id", ["process_sheet_id"], False),
        ("ix_process_sheet_steps_spc_characteristic_id", ["spc_characteristic_id"], False),
    ],
    "wo_operation_steps": [
        ("ix_wo_operation_steps_company_id", ["company_id"], False),
        ("ix_wo_operation_steps_company_operation", ["company_id", "work_order_operation_id"], False),
        ("ix_wo_operation_steps_id", ["id"], False),
        ("ix_wo_operation_steps_source_sheet_id", ["source_sheet_id"], False),
        ("ix_wo_operation_steps_spc_characteristic_id", ["spc_characteristic_id"], False),
        ("ix_wo_operation_steps_work_order_operation_id", ["work_order_operation_id"], False),
    ],
    "operation_step_records": [
        ("ix_operation_step_records_attachment_document_id", ["attachment_document_id"], False),
        ("ix_operation_step_records_company_id", ["company_id"], False),
        ("ix_operation_step_records_company_operation", ["company_id", "work_order_operation_id"], False),
        (
            "ix_operation_step_records_company_step_serial",
            ["company_id", "wo_operation_step_id", "serial_number"],
            False,
        ),
        ("ix_operation_step_records_equipment_id", ["equipment_id"], False),
        ("ix_operation_step_records_id", ["id"], False),
        ("ix_operation_step_records_recorded_by", ["recorded_by"], False),
        ("ix_operation_step_records_superseded_by_id", ["superseded_by_id"], False),
        ("ix_operation_step_records_wo_operation_step_id", ["wo_operation_step_id"], False),
        ("ix_operation_step_records_work_order_operation_id", ["work_order_operation_id"], False),
    ],
}

# Creation order (FK dependency order); downgrade drops in reverse.
TABLES = ["process_sheets", "process_sheet_steps", "wo_operation_steps", "operation_step_records"]

# The three nullable FK columns added to existing tenant tables:
# (table, column_name, fk_target, index_name). Column shape is identical for
# all three -- Integer, nullable, unnamed inline FK, single-column index --
# matching the models (routing.py / spc.py / work_order_blocker.py).
ADDED_COLUMNS = [
    ("routing_operations", "process_sheet_id", "process_sheets.id", "ix_routing_operations_process_sheet_id"),
    ("spc_measurements", "operation_id", "work_order_operations.id", "ix_spc_measurements_operation_id"),
    ("work_order_blockers", "ncr_id", "ncrs.id", "ix_work_order_blockers_ncr_id"),
]


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    return _inspector().has_table(table_name)


def _has_index(table_name: str, index_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(ix["name"] == index_name for ix in _inspector().get_indexes(table_name))


def _has_column(table_name: str, column_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(col["name"] == column_name for col in _inspector().get_columns(table_name))


def _create_process_sheets() -> None:
    # Lock-step with app/models/process_sheet.py::ProcessSheet. Mixin columns
    # (TenantMixin, then SoftDeleteMixin, then OptimisticLockMixin -- MRO
    # order) are appended after the declared columns, matching create_all.
    op.create_table(
        "process_sheets",
        sa.Column("id", sa.Integer(), nullable=False),
        # Auto-assigned "PS-000123"; shared across revisions (same pattern as routings).
        sa.Column("sheet_number", sa.String(length=50), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("revision", sa.String(length=20), nullable=False),
        # ProcessSheetStatus values (draft/released/obsolete); app-side default only.
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("effective_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("obsolete_date", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        # TenantMixin -- non-null company scope.
        sa.Column("company_id", sa.Integer(), nullable=False),
        # SoftDeleteMixin -- server_default='false' per the mixin.
        sa.Column("is_deleted", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by", sa.Integer(), nullable=True),
        # OptimisticLockMixin -- server defaults per the mixin.
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default="now()", nullable=False),
        # FK clause order mirrors create_all's emission order (cosmetic parity).
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["updated_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("company_id", "sheet_number", "revision", name="uq_process_sheets_company_number_revision"),
    )


def _create_process_sheet_steps() -> None:
    # Lock-step with app/models/process_sheet.py::ProcessSheetStep.
    op.create_table(
        "process_sheet_steps",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("process_sheet_id", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("instruction_text", sa.Text(), nullable=True),
        # StepType values; validated per-type config lives in JSON.
        sa.Column("step_type", sa.String(length=20), nullable=False),
        sa.Column("is_required", sa.Boolean(), nullable=False),
        sa.Column("config", sa.JSON(), nullable=True),
        sa.Column("requires_gauge", sa.Boolean(), nullable=False),
        sa.Column("spc_characteristic_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["process_sheet_id"], ["process_sheets.id"]),
        sa.ForeignKeyConstraint(["spc_characteristic_id"], ["spc_characteristics.id"]),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def _create_wo_operation_steps() -> None:
    # Lock-step with app/models/process_sheet.py::WOOperationStep (the
    # immutable per-WO traveler snapshot; PR 3 populates it).
    op.create_table(
        "wo_operation_steps",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("work_order_operation_id", sa.Integer(), nullable=False),
        # Traceability back to the released library sheet.
        sa.Column("source_sheet_id", sa.Integer(), nullable=False),
        sa.Column("source_sheet_revision", sa.String(length=20), nullable=False),
        # Snapshot copies of the ProcessSheetStep definition columns.
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("instruction_text", sa.Text(), nullable=True),
        sa.Column("step_type", sa.String(length=20), nullable=False),
        sa.Column("is_required", sa.Boolean(), nullable=False),
        sa.Column("config", sa.JSON(), nullable=True),
        sa.Column("requires_gauge", sa.Boolean(), nullable=False),
        sa.Column("spc_characteristic_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["work_order_operation_id"], ["work_order_operations.id"]),
        sa.ForeignKeyConstraint(["source_sheet_id"], ["process_sheets.id"]),
        sa.ForeignKeyConstraint(["spc_characteristic_id"], ["spc_characteristics.id"]),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def _create_operation_step_records() -> None:
    # Lock-step with app/models/process_sheet.py::OperationStepRecord.
    # APPEND-ONLY evidence: no soft-delete columns ON PURPOSE; corrections
    # chain through the self-referential superseded_by_id FK.
    op.create_table(
        "operation_step_records",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("wo_operation_step_id", sa.Integer(), nullable=False),
        # Denormalized for cheap completion-gating queries (PR 3).
        sa.Column("work_order_operation_id", sa.Integer(), nullable=False),
        sa.Column("serial_number", sa.String(length=100), nullable=True),
        # One populated per step type.
        sa.Column("value_text", sa.Text(), nullable=True),
        sa.Column("value_numeric", sa.Float(), nullable=True),
        sa.Column("value_bool", sa.Boolean(), nullable=True),
        sa.Column("is_conforming", sa.Boolean(), nullable=True),
        sa.Column("recorded_by", sa.Integer(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        # TimeEntrySource vocabulary; NULL = not reported, never guessed.
        sa.Column("source", sa.String(length=20), nullable=True),
        sa.Column("equipment_id", sa.Integer(), nullable=True),
        sa.Column("qualification_snapshot", sa.JSON(), nullable=True),
        sa.Column("attachment_document_id", sa.Integer(), nullable=True),
        # Correction chain -- stamped once, never cleared.
        sa.Column("superseded_by_id", sa.Integer(), nullable=True),
        sa.Column("supersede_reason", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["wo_operation_step_id"], ["wo_operation_steps.id"]),
        sa.ForeignKeyConstraint(["work_order_operation_id"], ["work_order_operations.id"]),
        sa.ForeignKeyConstraint(["recorded_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["equipment_id"], ["equipment.id"]),
        sa.ForeignKeyConstraint(["attachment_document_id"], ["documents.id"]),
        sa.ForeignKeyConstraint(["superseded_by_id"], ["operation_step_records.id"]),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


_CREATORS = {
    "process_sheets": _create_process_sheets,
    "process_sheet_steps": _create_process_sheet_steps,
    "wo_operation_steps": _create_wo_operation_steps,
    "operation_step_records": _create_operation_step_records,
}


def upgrade() -> None:
    # 1) New tables in FK dependency order, then their indexes. All guarded so
    #    a create_all-bootstrapped DB and re-runs no-op.
    for table_name in TABLES:
        if not _has_table(table_name):
            _CREATORS[table_name]()
        for index_name, columns, unique in INDEXES[table_name]:
            if not _has_index(table_name, index_name):
                op.create_index(index_name, table_name, columns, unique=unique)

    # 2) Nullable FK columns on existing tenant tables (no default, no
    #    backfill -- NULL is the correct value for every historical row), then
    #    their single-column indexes. Guarded per column/index. SQLite cannot
    #    ALTER-add the FK constraint (alembic emits ADD COLUMN + ADD
    #    CONSTRAINT), so it takes the batch (table-recreate) path; Postgres
    #    keeps plain ALTERs (see module docstring).
    is_sqlite = op.get_bind().dialect.name == "sqlite"
    for table_name, column_name, fk_target, index_name in ADDED_COLUMNS:
        if not _has_column(table_name, column_name):
            if is_sqlite:
                # Batch mode requires NAMED constraints; the name exists only
                # on SQLite dev DBs that took this migration path (bootstrap
                # and Postgres both keep the auto-/un-named FK).
                referent_table, referent_col = fk_target.split(".")
                with op.batch_alter_table(table_name) as batch_op:
                    batch_op.add_column(sa.Column(column_name, sa.Integer(), nullable=True))
                    batch_op.create_foreign_key(
                        f"fk_{table_name}_{column_name}", referent_table, [column_name], [referent_col]
                    )
            else:
                op.add_column(
                    table_name,
                    sa.Column(column_name, sa.Integer(), sa.ForeignKey(fk_target), nullable=True),
                )
        if not _has_index(table_name, index_name):
            op.create_index(index_name, table_name, [column_name], unique=False)


def downgrade() -> None:
    # 1) Added columns first (routing_operations.process_sheet_id references
    #    process_sheets, so it must go before the table drop): index, then
    #    column, guarded. SQLite needs a batch (table-recreate) drop because
    #    the column sits in a table-level FOREIGN KEY clause on bootstrapped
    #    DBs; Postgres keeps the plain, metadata-only DROP COLUMN (see module
    #    docstring).
    is_sqlite = op.get_bind().dialect.name == "sqlite"
    for table_name, column_name, _fk_target, index_name in reversed(ADDED_COLUMNS):
        if _has_index(table_name, index_name):
            op.drop_index(index_name, table_name=table_name)
        if _has_column(table_name, column_name):
            if is_sqlite:
                with op.batch_alter_table(table_name) as batch_op:
                    batch_op.drop_column(column_name)
            else:
                op.drop_column(table_name, column_name)

    # 2) Tables children-first (reverse creation order); indexes before each
    #    table, all guarded.
    for table_name in reversed(TABLES):
        if _has_table(table_name):
            for index_name, _columns, _unique in reversed(INDEXES[table_name]):
                if _has_index(table_name, index_name):
                    op.drop_index(index_name, table_name=table_name)
            op.drop_table(table_name)
