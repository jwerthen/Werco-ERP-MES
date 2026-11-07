"""Add work_order_operations.quantity_short_declared (the persisted short-close declaration)

Revision ID: 117_operation_short_declared
Revises: 116_business_keys_per_company
Create Date: 2026-11-07

Context
-------
Owner ruling 1 (2026-11-05): a short count at operation completion must be reported as
scrap with a required reason, or the operation stays open -- never short-closed at the
counted quantity. Once a stage CAN close short (8 good + 2 scrapped on a 10-piece
routing), the stages after it receive only 8, and the accounting core in
``app/services/work_order_state_service.py`` has to know that. This revision adds the ONE
column that carries it, over the EXISTING ``work_order_operations`` table
(``app/models/work_order.py::WorkOrderOperation``):

- ``quantity_short_declared``  Float, NULLABLE, no server default, no Python default,
  no index.

It is written ONLY by the two explicit close verbs (``POST /shop-floor/operations/{id}/
complete`` and ``POST /work-orders/operations/{id}/complete``), as received - good, at
the moment a named person closes the operation short with that short covered by scrap.
Downstream credit along a chain and the all-complete work-order header read it; nothing
re-derives it later.

Why a persisted column and not a read-time derivation
-----------------------------------------------------
Re-deriving the short from ``quantity_complete`` / ``quantity_scrapped`` at read time
would let later edits (an office reduce on a COMPLETE operation, a blind-``setattr``
``PUT``) silently change what every later stage was credited with and what the
finished-goods receipt booked. It would also turn every existing COMPLETE-with-scrap row
into upstream credit on the first read after deploy, because the work-order GET runs the
completion reconcile. A value written once, at the explicit close, by the verb that
audits that close, has neither problem.

No backfill, no default (deliberate, correct-forward)
-----------------------------------------------------
Every pre-existing row is NULL, and NULL is the truthful state: no operation has ever
been closed under ruling 1, so no operation has ever DECLARED a short. NULL reads as
"no declaration", which gives zero upstream credit, so clock-out, reconcile-on-read and
the header behave byte-identically on existing data after deploy. A backfill would have
to guess a declaration from today's counts -- precisely the inference the column exists
to avoid -- and would mutate released production records with no ``AuditService`` row
(invariant 2). Forward-only; never tightened to NOT NULL, because NULL is what "never
declared" means.

Shape / compliance
------------------
- ``work_order_operations`` is an EXISTING tenant table: it already carries the
  TenantMixin non-null ``company_id`` + index (invariant 1 is unaffected), and RLS has
  been enabled on it since ``059``. This revision creates no table, so the ENABLE-RLS
  new-table convention does not apply, and it emits no RLS / ``REVOKE`` / role-naming
  statement (precedent 068/070/081/083/114). Existing table grants cover a new column.
- Pure DDL: no row is read or written. The tamper-evident ``audit_log`` table and its
  ``sequence_number`` / ``previous_hash`` / ``integrity_hash`` columns are untouched.
- ``WorkOrderOperation`` is not soft-deletable (no ``SoftDeleteMixin``); nothing here
  deletes anything.
- Not on any request schema: the column is server-written by the close verbs only.

Lock-step with the model (the stamped-over hazard)
--------------------------------------------------
The column is declared on ``WorkOrderOperation`` in the same PR, with the identical
shape, so the ``create_all`` -> stamp -> upgrade bootstrap builds it from the model and
this revision's guard no-ops there. ``tests/test_migration_117_operation_short_declared.py``
pins the two equal, and ``tests/test_migration_fresh_bootstrap.py``'s chain and its
``downgrade 088`` -> ``upgrade head`` round-trip diff the result against ``create_all``.

Idempotent and reversible
-------------------------
- Upgrade guards the ADD COLUMN with ``_has_column`` (088 style), so the
  ``create_all`` bootstrap path and any re-run are clean no-ops.
- Downgrade drops exactly this one column, guarded by ``_has_column``. On SQLite it runs
  in batch mode (recreates the table without it; precedent 063/064/065/068/070/083/114);
  on PostgreSQL it is a plain ``DROP COLUMN``. No index exists to drop first.
- ``--sql`` (offline) has no database to inspect, so it renders the canonical path: the
  ADD COLUMN on upgrade, the DROP COLUMN on downgrade (precedent 089/116).

Downgrade is LOSSY -- read before running it
--------------------------------------------
Dropping the column discards every declared short. Pre-117 code never reads it, so a
code rollback does NOT need this downgrade (old code ignores an extra nullable column).
If it is run anyway and the column is later re-added, every operation that had closed
short reads NULL again: the later stages of its work order lose their upstream credit
(they would then need scrap declared again, or stay open) and the all-complete header
falls back to the no-declaration snap. The declarations are still recorded in the
``COMPLETE_OPERATION`` audit rows that wrote them, but nothing re-derives the column from
those rows. Before downgrading a database where short closes have happened, export them::

    SELECT id, company_id, work_order_id, sequence, quantity_short_declared
    FROM work_order_operations
    WHERE quantity_short_declared IS NOT NULL;

Locking / operations note
-------------------------
``ADD COLUMN`` of a nullable column with no default is metadata-only on PostgreSQL 11+
(no table rewrite, no backfill pass), but it still takes a brief ACCESS EXCLUSIVE lock on
``work_order_operations`` -- the hottest table on the shop floor (every clock-in,
clock-out, production report and queue poll touches it). ``alembic/env.py`` runs every
pending revision in one transaction, so the lock is held until the deploy commits; this
revision is the only DDL in it, so that window is the ``ADD COLUMN`` itself.
``SET LOCAL lock_timeout = '5s'`` is taken only when there is DDL to run: if a long
transaction holds the table, the ``ADD COLUMN`` fails after 5 s instead of queueing --
and queueing every later shop-floor query behind it -- for as long as that transaction
runs. The deploy then fails fast (``start.sh`` runs ``alembic upgrade head`` under
``set -e``, so the new API does not start) and a re-run is safe. It is handed back
(``SET LOCAL lock_timeout = DEFAULT``) as soon as the DDL is done so it never leaks into
a later revision in the same run (precedent 114/115/116).

Deploy ordering: run BEFORE (or with) the release that maps the column. The migration
runs at API boot (``backend/start.sh``), so the API is always migrated before it serves.
Old code is safe on the new schema (it never selects the column). New code on the
un-migrated schema is NOT: every ``WorkOrderOperation`` load would select a missing
column -- so deploy the API before or with the ARQ worker, never the worker first.

Revision id ``117_operation_short_declared`` is 28 chars (<= 32) per the create_all ->
stamp -> upgrade bootstrap constraint (``alembic_version.version_num`` is varchar(32) on
a freshly bootstrapped DB); see docs/DEVELOPMENT.md.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "117_operation_short_declared"
down_revision = "116_business_keys_per_company"
branch_labels = None
depends_on = None

TABLE_NAME = "work_order_operations"
COLUMN_NAME = "quantity_short_declared"

# PostgreSQL only, and LOCAL: it lasts until the migration transaction ends and bounds the
# wait for the ACCESS EXCLUSIVE lock on work_order_operations. Same value as 059/060/114.
LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = '5s'"
# ...and handed back as soon as the DDL is done, so a later revision in the same
# ``alembic upgrade head`` run gets its normal lock_timeout (precedent 114).
RESET_LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = DEFAULT"


def _offline() -> bool:
    # ``--sql`` has no database to inspect: render the canonical statements.
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


def upgrade() -> None:
    # Nullable, no default -> metadata-only ADD COLUMN on PostgreSQL, no rewrite, no
    # backfill. Guarded so the create_all bootstrap path (the model already built the
    # column) and any re-run are clean no-ops.
    if not _offline() and _has_column(TABLE_NAME, COLUMN_NAME):
        return

    postgres = _is_postgres()
    if postgres:
        op.execute(LOCK_TIMEOUT_SQL)
    op.add_column(TABLE_NAME, sa.Column(COLUMN_NAME, sa.Float(), nullable=True))
    if postgres:
        op.execute(RESET_LOCK_TIMEOUT_SQL)


def downgrade() -> None:
    # LOSSY: discards every declared short. See the module docstring before running it.
    if not _offline() and not _has_column(TABLE_NAME, COLUMN_NAME):
        return

    postgres = _is_postgres()
    if postgres:
        op.execute(LOCK_TIMEOUT_SQL)
    if op.get_context().dialect.name == "sqlite":
        # Batch mode recreates the table without the column (precedent
        # 063/064/065/068/070/083/114) rather than relying on SQLite's version-gated
        # ALTER TABLE ... DROP COLUMN.
        with op.batch_alter_table(TABLE_NAME) as batch_op:
            batch_op.drop_column(COLUMN_NAME)
    else:
        op.drop_column(TABLE_NAME, COLUMN_NAME)
    if postgres:
        op.execute(RESET_LOCK_TIMEOUT_SQL)
