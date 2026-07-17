"""Standalone laser-nest work orders: part_id nullable-for-laser + parentless nest packages

Revision ID: 067_standalone_laser_nest_wo
Revises: 066_inspection_not_required
Create Date: 2026-07-17

Context
-------
Laser-cutting work orders can now be created straight from an Ermaksan nest
package with NO parent work order and NO part (nest WO quantity semantics are
sheet runs, not pieces of a finished good). Two schema changes back that, both
mirrored in the models so the create_all bootstrap path builds the same shape:

1. ``work_orders.part_id`` becomes NULLABLE, but ONLY for
   ``work_order_type = 'laser_cutting'`` — enforced by a new table CHECK
   constraint ``ck_work_orders_part_required_unless_laser``::

       part_id IS NOT NULL OR work_order_type = 'laser_cutting'

   The condition text is byte-identical to the model-level ``CheckConstraint``
   in app/models/work_order.py::WorkOrder.__table_args__ so both paths converge
   on the same object. ``work_order_type`` is a plain ``String(50)`` column
   (NOT a native enum) storing the raw ``WorkOrderType.value`` literal, so the
   ``'laser_cutting'`` comparison is dialect-safe. Every existing row has
   ``part_id NOT NULL``, so adding the CHECK validates cleanly over live data.

2. ``laser_nest_packages.parent_work_order_id`` becomes NULLABLE — a standalone
   nest package has no parent assembly WO; ``child_work_order_id`` (already
   nullable) points at the standalone laser WO
   (app/models/laser_nest.py::LaserNestPackage).

No data is written or backfilled; the tamper-evident ``audit_log`` table is
untouched.

Shape / compliance
------------------
ALTER-only — no new table is created, so the "ENABLE ROW LEVEL SECURITY on
every new table" convention does not apply and no RLS DDL is emitted (precedent
064/065/066: RLS was enabled repo-wide on every ``public`` table by 059, and
altered columns/constraints on an already-covered table inherit it). Both
tables keep their TenantMixin ``company_id`` scoping unchanged.

Idempotent and dialect-aware
----------------------------
- All DDL is Postgres-only (``_is_postgres`` early return). On SQLite (local
  dev / pytest) the DB is rebuilt via ``create_all`` from the updated models,
  which already declare the nullable columns AND carry the model-level CHECK —
  this migration is a pure no-op there (dialect-guard precedent 053/064/066;
  no batch mode needed since no column is ever dropped).
- Each DROP NOT NULL is guarded by the column's CURRENT reflected nullability
  (precedent 053), and the CHECK add is guarded by
  ``inspector.get_check_constraints`` name membership — new guard, no prior
  repo precedent, but the constraint name is explicit on both the migration and
  the create_all path so a name match covers both — making the whole upgrade
  safe to re-run and a clean no-op on a create_all -> stamp -> upgrade
  bootstrapped Postgres.

Downgrade (real, and it raises on violating rows — deliberate 053 deviation)
----------------------------------------------------------------------------
Reverse order: drop the CHECK, then re-tighten NOT NULL on
``laser_nest_packages.parent_work_order_id`` and ``work_orders.part_id`` — but
ONLY after probing ``SELECT COUNT(*) ... IS NULL``. If standalone-laser rows
exist the downgrade RAISES with a clear message instead of 053's
print-and-leave-relaxed: silently keeping the wider nullability while the CHECK
is gone would let part-less NON-laser work orders be written by the downgraded
schema. Alembic runs the migration transactionally on Postgres, so the raise
also rolls back the CHECK drop — the downgrade is all-or-nothing. Resolve by
deleting/re-homing the standalone laser WOs and their packages first, then
re-run the downgrade.

Locking / operations note
-------------------------
DROP NOT NULL is a catalog-only change (brief ACCESS EXCLUSIVE, no rewrite, no
scan). ADD CONSTRAINT ... CHECK takes ACCESS EXCLUSIVE and scans the table once
to validate — ``work_orders`` is thousands of rows at most, so this is
sub-second; NOT VALID + VALIDATE staging is deliberately not used (003
precedent, table is small). Deploy ordering: run before (or with) the app
deploy that creates part-less laser WOs; old code never writes ``part_id NULL``
and is unaffected either way.

Revision id ``067_standalone_laser_nest_wo`` is 28 chars (<= 32) per the
create_all -> stamp -> upgrade bootstrap constraint (alembic_version.version_num
is varchar(32) on a freshly bootstrapped DB); see docs/DEVELOPMENT.md.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "067_standalone_laser_nest_wo"
down_revision = "066_inspection_not_required"
branch_labels = None
depends_on = None

WORK_ORDERS = "work_orders"
PART_ID_COLUMN = "part_id"

PACKAGES_TABLE = "laser_nest_packages"
PARENT_WO_COLUMN = "parent_work_order_id"

# Kept in lock-step (byte-identical condition text) with the model-level
# CheckConstraint in app/models/work_order.py::WorkOrder.__table_args__.
CHECK_NAME = "ck_work_orders_part_required_unless_laser"
CHECK_CONDITION = "part_id IS NOT NULL OR work_order_type = 'laser_cutting'"


def _is_postgres(conn) -> bool:
    return conn.dialect.name == "postgresql"


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    return _inspector().has_table(table_name)


def _column(table_name: str, column_name: str):
    """Reflected column dict, or None if the table/column does not exist."""
    if not _has_table(table_name):
        return None
    for col in _inspector().get_columns(table_name):
        if col["name"] == column_name:
            return col
    return None


def _has_check_constraint(table_name: str, constraint_name: str) -> bool:
    """True if a CHECK constraint with this name exists on the table.

    Name membership is sufficient: the constraint name is EXPLICIT on both the
    migration path (this file) and the create_all bootstrap path (the model's
    named CheckConstraint), so the two variants cannot diverge on name.
    """
    if not _has_table(table_name):
        return False
    return any(ck.get("name") == constraint_name for ck in _inspector().get_check_constraints(table_name))


def upgrade() -> None:
    conn = op.get_bind()

    # Postgres-only: on SQLite the create_all bootstrap already built both
    # nullable columns and the model-level CHECK, so there is nothing to do.
    if not _is_postgres(conn):
        return

    # 1. work_orders.part_id DROP NOT NULL (guarded by current nullability --
    #    catalog-only, no table rewrite; precedent 053).
    col = _column(WORK_ORDERS, PART_ID_COLUMN)
    if col is not None and not col["nullable"]:
        op.alter_column(WORK_ORDERS, PART_ID_COLUMN, existing_type=sa.Integer(), nullable=True)

    # 2. CHECK: part required unless laser_cutting. Existing rows all have
    #    part_id NOT NULL, so validation passes over live data.
    if not _has_check_constraint(WORK_ORDERS, CHECK_NAME):
        op.create_check_constraint(CHECK_NAME, WORK_ORDERS, CHECK_CONDITION)

    # 3. laser_nest_packages.parent_work_order_id DROP NOT NULL (guarded).
    col = _column(PACKAGES_TABLE, PARENT_WO_COLUMN)
    if col is not None and not col["nullable"]:
        op.alter_column(PACKAGES_TABLE, PARENT_WO_COLUMN, existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    conn = op.get_bind()

    # Postgres-only mirror of upgrade(): on SQLite nothing was done, so nothing
    # is undone (the model-level CHECK belongs to the create_all schema there).
    if not _is_postgres(conn):
        return

    # 1. Drop the CHECK if present. Runs in the same transaction as the NOT
    #    NULL probes below, so a raise rolls this back too (all-or-nothing).
    if _has_check_constraint(WORK_ORDERS, CHECK_NAME):
        op.drop_constraint(CHECK_NAME, WORK_ORDERS, type_="check")

    # 2. Re-tighten laser_nest_packages.parent_work_order_id -- ONLY if no
    #    standalone (parentless) packages exist; otherwise raise so the
    #    downgrade cannot half-apply (deliberate deviation from 053's
    #    print-and-continue; see module docstring).
    col = _column(PACKAGES_TABLE, PARENT_WO_COLUMN)
    if col is not None and col["nullable"]:
        null_count = conn.execute(
            sa.text(f"SELECT COUNT(*) FROM {PACKAGES_TABLE} WHERE {PARENT_WO_COLUMN} IS NULL")
        ).scalar()
        if null_count:
            raise RuntimeError(
                f"cannot downgrade 067: {null_count} standalone laser-nest package(s) have NULL "
                f"{PACKAGES_TABLE}.{PARENT_WO_COLUMN}; delete or re-home them under a parent work "
                "order before re-adding NOT NULL"
            )
        op.alter_column(PACKAGES_TABLE, PARENT_WO_COLUMN, existing_type=sa.Integer(), nullable=False)

    # 3. Re-tighten work_orders.part_id -- ONLY if no part-less (standalone
    #    laser) work orders exist; otherwise raise (same rationale as above).
    col = _column(WORK_ORDERS, PART_ID_COLUMN)
    if col is not None and col["nullable"]:
        null_count = conn.execute(
            sa.text(f"SELECT COUNT(*) FROM {WORK_ORDERS} WHERE {PART_ID_COLUMN} IS NULL")
        ).scalar()
        if null_count:
            raise RuntimeError(
                f"cannot downgrade 067: {null_count} part-less laser-cutting work order(s) exist "
                f"({WORK_ORDERS}.{PART_ID_COLUMN} IS NULL); delete them or assign a part before "
                "re-adding NOT NULL"
            )
        op.alter_column(WORK_ORDERS, PART_ID_COLUMN, existing_type=sa.Integer(), nullable=False)
