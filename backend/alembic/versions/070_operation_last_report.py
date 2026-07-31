"""Add last-report telemetry columns to work_order_operations (kiosk Foundry redesign)

Revision ID: 070_operation_last_report
Revises: 069_work_order_version_guard
Create Date: 2026-07-23

Context
-------
Adds THREE nullable, un-indexed columns to the existing
``work_order_operations`` table (app/models/work_order.py::WorkOrderOperation)
for the kiosk Foundry redesign:

- ``last_reported_at``       DateTime (naive UTC, like the sibling timestamps)
- ``last_reported_good``     Float
- ``last_reported_scrapped`` Float

They record the most recent production-evidence report on the operation --
stamped by ``POST /shop-floor/operations/{id}/production`` and by a
quantity-carrying clock-out. The good/scrapped values are THAT report's DELTAS
(the kiosk renders "LAST REPORT 14:02 +48"), not running totals; the running
totals stay on the existing ``quantity_complete`` / ``quantity_scrapped``
columns and the monotonic-up / evidence-floor reconcile model is untouched.
Deliberately NOT indexed: the columns are only ever read row-wise off
operations already selected by the existing queue queries -- never filtered or
sorted on -- so an index would cost writes and buy nothing.

No backfill (deliberate, correct-forward)
-----------------------------------------
Every pre-existing row is legitimately NULL -- "no report recorded yet" is the
truthful state for historical operations, and reconstructing a fake "last
report" from quantity totals or time entries would fabricate telemetry nobody
recorded (the same correct-forward/no-backfill posture as 066). The API/kiosk
payload treats NULL as "no report yet". No data is written; the tamper-evident
``audit_log`` table is untouched (invariant: never write it out of band -- the
production reports themselves are audited by the service layer via
``AuditService``).

Shape / compliance
------------------
``work_order_operations`` is an EXISTING table: it already carries the
TenantMixin non-null ``company_id`` + index, and RLS is already enabled on it --
059 (``059_supabase_rls_hardening``) enabled ROW LEVEL SECURITY dynamically on
every ``public`` table. This migration creates NO table (no ``op.create_table``),
so the "ENABLE ROW LEVEL SECURITY on every new table" convention does not apply
and no RLS DDL is emitted (precedent 064/065/066/067/068/069).

Idempotent and reversible
-------------------------
- Upgrade guards each ADD COLUMN with ``_has_column``, so the
  create_all -> stamp -> upgrade bootstrap path -- where ``Base.metadata
  .create_all`` already built the columns from the ORM mapping -- no-ops
  instead of erroring, and a partial prior run resumes cleanly
  (precedent 064/065/068).
- Downgrade drops EXACTLY the three columns this revision owns, each guarded by
  ``_has_column``. On SQLite the drops run in ONE batch context, which
  recreates the table without the columns (precedent 063/064/065/068), rather
  than relying on SQLite's version-gated ``ALTER TABLE ... DROP COLUMN``. No
  index cleanup is needed (none is created, and the ORM maps none).
- Dialect-neutral DDL in both directions: plain nullable ADD COLUMN works
  identically on PostgreSQL and on the SQLite dev/pytest DBs, so there is no
  ``_is_postgres`` early return here (precedent 068).

Locking / operations note
-------------------------
Each ADD COLUMN (nullable, NO default, no server_default) is metadata-only on
PostgreSQL: a brief ACCESS EXCLUSIVE lock per statement, no table rewrite, no
backfill pass -- effectively free even on the largest dispatch-path table. No
index is built, so unlike 068 there is no SHARE-lock window blocking shop-floor
writes.

Deploy ordering: run BEFORE (or with) the app deploy that stamps the columns.
Old code neither writes nor selects them and is unaffected either way; a
downgrade only loses last-report telemetry (display data), never production
quantities or history.

Revision id ``070_operation_last_report`` is 25 chars (<= 32) per the
create_all -> stamp -> upgrade bootstrap constraint (alembic_version.version_num
is varchar(32) on a freshly bootstrapped DB); see docs/DEVELOPMENT.md.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "070_operation_last_report"
down_revision = "069_work_order_version_guard"
branch_labels = None
depends_on = None

TABLE_NAME = "work_order_operations"

# Column name -> type factory, in model declaration order. The exact set this
# revision owns: upgrade adds these and nothing else; downgrade drops these and
# nothing else.
COLUMNS = (
    ("last_reported_at", sa.DateTime),
    ("last_reported_good", sa.Float),
    ("last_reported_scrapped", sa.Float),
)


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    return _inspector().has_table(table_name)


def _has_column(table_name: str, column_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(col["name"] == column_name for col in _inspector().get_columns(table_name))


def upgrade() -> None:
    if not _has_table(TABLE_NAME):
        # Partial bootstrap with no work_order_operations table at all: nothing
        # to alter; Base.metadata.create_all builds the columns from the ORM
        # mapping (precedent 069).
        return

    # Nullable, no default -> metadata-only ADD COLUMN, no rewrite, no backfill.
    for column_name, column_type in COLUMNS:
        if not _has_column(TABLE_NAME, column_name):
            op.add_column(TABLE_NAME, sa.Column(column_name, column_type(), nullable=True))


def downgrade() -> None:
    if not _has_table(TABLE_NAME):
        return

    present = [name for name, _ in COLUMNS if _has_column(TABLE_NAME, name)]
    if not present:
        return

    if op.get_bind().dialect.name == "sqlite":
        # One batch context recreates the table without the columns
        # (precedent 063/064/065/068) rather than relying on SQLite's
        # version-gated ALTER TABLE ... DROP COLUMN.
        with op.batch_alter_table(TABLE_NAME) as batch_op:
            for column_name in present:
                batch_op.drop_column(column_name)
    else:
        for column_name in present:
            op.drop_column(TABLE_NAME, column_name)
