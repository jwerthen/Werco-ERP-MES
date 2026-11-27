"""Operation SKIPPED status: a routing step taken off a released job stays on the traveler

Revision ID: 135_operation_skipped
Revises: 134_part_customer_supplied
Create Date: 2026-11-25

Context
-------
Owner request (2026-11-25): "I need to be able to update routing for work orders once a work
order releases." Removing a step from a RELEASED job never deletes it: the step is marked
**Skipped** -- it stays on the traveler with who / when / why, and the steps after it can proceed
(``POST /work-orders/operations/{id}/skip``; ``/unskip`` restores it). This revision is the schema
half, over the EXISTING ``work_order_operations`` table
(``app/models/work_order.py::WorkOrderOperation``):

1. ``ALTER TYPE operationstatus ADD VALUE IF NOT EXISTS 'SKIPPED'`` -- PostgreSQL only.
2. Three NULLABLE columns, no default, no backfill:

   - ``skipped_at``  DateTime (naive UTC, like ``actual_start`` / ``actual_end``);
   - ``skipped_by``  Integer, FK ``fk_work_order_operations_skipped_by`` -> ``users.id``
     ``ON DELETE SET NULL`` (the shape of the sibling ``started_by`` / ``completed_by`` FKs);
   - ``skip_reason`` String(500), the reason the person typed.

The enum label is UPPERCASE ``SKIPPED`` (load-bearing)
------------------------------------------------------
``work_order_operations.status`` is the unnamed ``SQLEnum(OperationStatus)`` -- native type
``operationstatus``, declared WITHOUT ``values_callable``, so SQLAlchemy stores the member NAME
(``PENDING`` / ``READY`` / ``IN_PROGRESS`` / ``COMPLETE`` / ``ON_HOLD``), never the lowercase
``.value``. The label added is therefore ``SKIPPED``; ``skipped`` is only the wire form. Same
reasoning, verbatim, as 066 (``inspectionstatus``). No migration before this one ever touched
``operationstatus`` -- ``create_all`` built it from the model.

Transaction handling: the ``ALTER TYPE`` runs inside ``op.get_context().autocommit_block()``
(precedent 047 / 052 / 066), so it neither runs inside nor is unusable until the end of
Alembic's per-migration transaction. ``IF NOT EXISTS`` makes it a no-op on a re-run and on a
database ``create_all`` built from the updated model. On SQLite there is no native enum: the
column is a VARCHAR sized to the longest member name (``IN_PROGRESS``), which ``SKIPPED`` fits,
and SQLAlchemy 2's ``Enum`` emits no CHECK by default, so nothing is needed there.

No backfill (deliberate)
------------------------
No existing operation is skipped, so every pre-existing row reads NULL in all three columns --
the truthful state. Nothing infers a skip from anything else.

Shape / compliance
------------------
- ``work_order_operations`` is an existing tenant table (TenantMixin non-null ``company_id``)
  with RLS enabled since ``059``; this revision creates no table, so the ENABLE-RLS new-table
  convention does not apply, and it emits no RLS / ``REVOKE`` / role statement.
- Model lock-step: ``WorkOrderOperation.skipped_at`` / ``.skipped_by`` / ``.skip_reason`` carry
  the identical types, nullability and FK name / ``ondelete``. No index (nothing filters on
  them; ``status`` is already indexed by ``ix_woo_status``).

Idempotent and reversible
-------------------------
- Every object is guarded independently (088 style): each column by ``_has_column`` and the FK
  by ``_has_foreign_key``, so over a ``create_all``-bootstrapped schema and on a re-run the
  upgrade emits nothing, and a partial schema recovers exactly what is missing.
- PostgreSQL: plain ``ALTER TABLE ... ADD COLUMN`` (metadata-only, nullable, no default) and an
  ``ADD CONSTRAINT`` FK under ``SET LOCAL lock_timeout = '5s'``, handed back afterwards
  (precedent 114-134). SQLite: one batch (copy-and-rename) so the named FK is created too --
  the fresh-bootstrap round trip diffs FK names against ``create_all`` (precedent 122).
- ``--sql`` (offline) renders the canonical statements.

Downgrade is LOSSY -- and cannot remove the enum value
------------------------------------------------------
The downgrade drops the FK and the three columns (who / when / why of every skip is lost; the
audit rows the skip verb wrote still record it). PostgreSQL cannot drop a value from an enum
type without recreating the type and rewriting every dependent column, so ``SKIPPED`` STAYS in
``operationstatus`` (same stance as 002 / 014 / 017 / 026 / 047 / 052 / 066). Before running a
downgrade, restore every skipped operation (``POST /work-orders/operations/{id}/unskip``): an
operation still holding ``SKIPPED`` is unreadable by pre-135 code, whose ``OperationStatus`` has
no such member. For a CODE rollback prefer a STAMP to this revision's ``down_revision`` after
restoring skipped operations -- the columns stay, and a re-upgrade is a guarded no-op.

Deploy ordering: run BEFORE (or with) the release that maps the columns -- the migration runs at
API boot (``backend/start.sh``). New code on the un-migrated schema would select missing columns
on every operation load. Deploy the API before or with the ARQ worker.

Revision id ``135_operation_skipped`` is 21 chars (<= 32, ``alembic_version.version_num``).
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "135_operation_skipped"
down_revision = "134_part_customer_supplied"
branch_labels = None
depends_on = None

TABLE_NAME = "work_order_operations"
ENUM_TYPE_NAME = "operationstatus"
# The member NAME (see the module docstring), never the lowercase ``.value``.
NEW_ENUM_VALUE = "SKIPPED"

SKIPPED_AT = "skipped_at"
SKIPPED_BY = "skipped_by"
SKIP_REASON = "skip_reason"
SKIPPED_BY_FK = "fk_work_order_operations_skipped_by"

LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = '5s'"
RESET_LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = DEFAULT"


def _columns() -> list[sa.Column]:
    """Fresh ``Column`` objects (a Column can belong to one table only)."""
    return [
        sa.Column(SKIPPED_AT, sa.DateTime(), nullable=True),
        sa.Column(SKIPPED_BY, sa.Integer(), nullable=True),
        sa.Column(SKIP_REASON, sa.String(length=500), nullable=True),
    ]


COLUMN_NAMES = tuple(column.name for column in _columns())


def _offline() -> bool:
    return bool(op.get_context().as_sql)


def _is_postgres() -> bool:
    return op.get_context().dialect.name == "postgresql"


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    return _inspector().has_table(table_name)


def _has_column(table_name: str, column_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(col["name"] == column_name for col in _inspector().get_columns(table_name))


def _has_foreign_key(table_name: str, constraint_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(fk["name"] == constraint_name for fk in _inspector().get_foreign_keys(table_name))


def upgrade() -> None:
    offline = _offline()
    postgres = _is_postgres()

    # 1. The enum label, PostgreSQL only, in autocommit (precedent 066). Independent of the
    #    column guards below: a database create_all built from the updated model already has it,
    #    and IF NOT EXISTS makes that a no-op.
    if postgres:
        with op.get_context().autocommit_block():
            op.execute(f"ALTER TYPE {ENUM_TYPE_NAME} ADD VALUE IF NOT EXISTS '{NEW_ENUM_VALUE}'")

    if not offline and not _has_table(TABLE_NAME):
        return  # no table at all: create_all builds the columns from the model

    columns = [column for column in _columns() if offline or not _has_column(TABLE_NAME, column.name)]
    add_fk = offline or not _has_foreign_key(TABLE_NAME, SKIPPED_BY_FK)
    if not (columns or add_fk):
        return  # the create_all bootstrap (or a re-run) already built every object

    if postgres:
        op.execute(LOCK_TIMEOUT_SQL)
    with op.batch_alter_table(TABLE_NAME) as batch:
        for column in columns:
            batch.add_column(column)
        if add_fk:
            batch.create_foreign_key(SKIPPED_BY_FK, "users", [SKIPPED_BY], ["id"], ondelete="SET NULL")
    if postgres:
        op.execute(RESET_LOCK_TIMEOUT_SQL)


def downgrade() -> None:
    # LOSSY, and the enum value stays -- see the module docstring before running it.
    offline = _offline()
    drop_fk = offline or _has_foreign_key(TABLE_NAME, SKIPPED_BY_FK)
    columns = [name for name in COLUMN_NAMES if offline or _has_column(TABLE_NAME, name)]
    if not (drop_fk or columns):
        return

    postgres = _is_postgres()
    if postgres:
        op.execute(LOCK_TIMEOUT_SQL)
    with op.batch_alter_table(TABLE_NAME) as batch:
        if drop_fk:
            batch.drop_constraint(SKIPPED_BY_FK, type_="foreignkey")
        for name in reversed(columns):
            batch.drop_column(name)
    if postgres:
        op.execute(RESET_LOCK_TIMEOUT_SQL)
    # No-op for the enum: PostgreSQL cannot drop a value from an enum type (module docstring).
