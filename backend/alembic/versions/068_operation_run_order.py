"""Add work_order_operations.run_order (manual dispatch rank per work center)

Revision ID: 068_operation_run_order
Revises: 067_standalone_laser_nest_wo
Create Date: 2026-07-20

Context
-------
Adds ONE nullable, indexed Integer column ``run_order`` to the existing
``work_order_operations`` table (app/models/work_order.py::WorkOrderOperation).

The operator kiosk queue is per WORK CENTER and currently orders by
``scheduled_start`` alone, which is usually NULL -> effectively arbitrary order.
``run_order`` lets a manager dictate, on the dispatch board, the order operators
run jobs at a machine:

- Manual dispatch rank within the operation's CURRENT work center. Dense 1..N
  per work center; NULL = unranked, and unranked work sorts AFTER all ranked
  work.
- DISTINCT from ``sequence``. ``sequence`` is routing-step precedence WITHIN a
  single work order and drives predecessor gating (see
  app/services/work_order_state_service.py and the composite index
  ``ix_woo_work_order_sequence``). ``run_order`` is cross-work-order, ADVISORY
  only, and never gates anything -- it changes presentation order, not what an
  operator is permitted to start.
- Deliberately NOT unique-constrained: ranks are rewritten wholesale for a work
  center in one transaction, so transient duplicates exist mid-rewrite; a
  (company_id, work_center_id, run_order) partial unique index would fight that
  rewrite and buy nothing, since the value is advisory.

No backfill (deliberate)
------------------------
Every pre-existing row is legitimately NULL -- "not yet ranked by a manager" is
the correct state for historical operations, and inventing ranks would assert a
dispatch decision nobody made. Ranking is a forward-only, manager-initiated act.
No data is written; the tamper-evident ``audit_log`` table is untouched (the
rank writes themselves are audited by the service layer via ``AuditService``).

Shape / compliance
------------------
``work_order_operations`` is an EXISTING table: it already carries the
TenantMixin non-null ``company_id`` + index, and RLS is already enabled on it --
059 (``059_supabase_rls_hardening``) enabled ROW LEVEL SECURITY dynamically on
every ``public`` table. This migration creates NO table (no ``op.create_table``),
so the "ENABLE ROW LEVEL SECURITY on every new table" convention does not apply
and no RLS DDL is emitted (precedent 064/065/066/067).

The index is NON-unique and uses the model's ``index=True`` default name
``ix_work_order_operations_run_order`` (SQLAlchemy's ``ix_<table>_<column>``) so
the create_all bootstrap path and this migration converge on ONE index object
(precedent 050/065).

Idempotent and reversible
-------------------------
- Upgrade guards the ADD COLUMN with ``_has_column`` and guards the index by
  COVERED COLUMN (``_index_names_on_column``) rather than by name, so the
  create_all -> stamp -> upgrade bootstrap path -- where the model's
  ``index=True`` already built the identically-named index -- no-ops instead of
  erroring (precedent 065; by-column guard precedent 064).
- Downgrade drops any index covering the column by REFLECTED name (covers both
  the migration-built and any create_all-built variant), then drops the column,
  guarded. On SQLite the drop runs in batch mode, which recreates the table
  without the column (precedent 063/064/065), rather than relying on SQLite's
  version-gated ``ALTER TABLE ... DROP COLUMN``.
- Dialect-neutral DDL in both directions: plain nullable ADD COLUMN + plain
  CREATE INDEX work identically on PostgreSQL and on the SQLite dev/pytest DBs,
  so unlike 066/067 there is no ``_is_postgres`` early return here.

Locking / operations note
-------------------------
ADD COLUMN (nullable, NO default, no server_default) is metadata-only on
PostgreSQL: a brief ACCESS EXCLUSIVE lock, no table rewrite, no backfill pass.
CREATE INDEX (non-CONCURRENT) takes a SHARE lock on ``work_order_operations``
for the duration of the build, which blocks WRITES (shop-floor clock-in /
quantity posts) but not reads. That table is the largest in the dispatch path --
tens of thousands of rows at this shop's scale -- so the build is expected to be
sub-second to a couple of seconds. If it is ever run against a materially larger
table, or during a shift, prefer building the index CONCURRENTLY out-of-band
(``CREATE INDEX CONCURRENTLY ix_work_order_operations_run_order ON
work_order_operations (run_order)`` in autocommit, then re-run this migration --
the by-column guard makes it a clean no-op).

Deploy ordering: run BEFORE (or with) the app deploy that reads/writes
``run_order``. Old code neither writes nor selects the column and is unaffected
either way; a downgrade only loses manual ranks (advisory data), never
production history.

Revision id ``068_operation_run_order`` is 23 chars (<= 32) per the
create_all -> stamp -> upgrade bootstrap constraint (alembic_version.version_num
is varchar(32) on a freshly bootstrapped DB); see docs/DEVELOPMENT.md.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "068_operation_run_order"
down_revision = "067_standalone_laser_nest_wo"
branch_labels = None
depends_on = None

TABLE_NAME = "work_order_operations"
COLUMN_NAME = "run_order"
# Matches the model's index=True default naming (ix_<table>_<column>) so the
# create_all bootstrap path and this migration converge on one index.
INDEX_NAME = "ix_work_order_operations_run_order"


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    return _inspector().has_table(table_name)


def _has_column(table_name: str, column_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(col["name"] == column_name for col in _inspector().get_columns(table_name))


def _index_names_on_column(table_name: str, column_name: str) -> list:
    """Names of indexes covering exactly this column.

    Checked by covered column rather than by name so both the migration-built
    and the create_all-bootstrapped index (identically named here, but the guard
    does not depend on that) are found (precedent 065).
    """
    if not _has_table(table_name):
        return []
    return [
        ix["name"]
        for ix in _inspector().get_indexes(table_name)
        if ix.get("column_names") == [column_name] and ix.get("name")
    ]


def upgrade() -> None:
    # Nullable, no default -> metadata-only ADD COLUMN, no rewrite, no backfill.
    if not _has_column(TABLE_NAME, COLUMN_NAME):
        op.add_column(TABLE_NAME, sa.Column(COLUMN_NAME, sa.Integer(), nullable=True))

    # Non-unique dispatch-ordering index. No-ops when the create_all bootstrap
    # path already built it via the model's index=True.
    if not _index_names_on_column(TABLE_NAME, COLUMN_NAME):
        op.create_index(INDEX_NAME, TABLE_NAME, [COLUMN_NAME], unique=False)


def downgrade() -> None:
    conn = op.get_bind()

    # Drop the index first (by reflected name -- covers the migration-built and
    # any differently-named create_all-built variant). SQLite also cannot drop a
    # column that is still named in an index.
    for actual_index_name in _index_names_on_column(TABLE_NAME, COLUMN_NAME):
        op.drop_index(actual_index_name, table_name=TABLE_NAME)

    if not _has_column(TABLE_NAME, COLUMN_NAME):
        return

    if conn.dialect.name == "sqlite":
        # Batch mode recreates the table without the column (precedent 063/064/065)
        # rather than relying on SQLite's version-gated ALTER ... DROP COLUMN.
        with op.batch_alter_table(TABLE_NAME) as batch_op:
            batch_op.drop_column(COLUMN_NAME)
    else:
        op.drop_column(TABLE_NAME, COLUMN_NAME)
