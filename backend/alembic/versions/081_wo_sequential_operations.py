"""Add sequential_operations opt-out to work_orders

Revision ID: 081_wo_sequential_operations
Revises: 080_restore_stamped_over_con
Create Date: 2026-08-14

Context
-------
Adds ONE NOT-NULL boolean column to the existing ``work_orders`` table
(app/models/work_order.py::WorkOrder):

- ``sequential_operations`` (Boolean, NOT NULL, server_default false) — whether
  this work order's operations are a sequenced ROUTING (an operation reaches
  READY only once every lower-sequence operation is COMPLETE, its own work
  center included) or a DISPATCH POOL (operations sharing a work center are
  mutually startable and promote together).

Why the column exists
---------------------
READY promotion has been pooled by work center since the batch/laser work:
``promote_ready_operations`` passes ``allow_same_work_center=True``, so every
operation of a work order that shares one machine goes READY at once. That is
correct for a laser nest package and for the 18-item press-brake / weld-
subassembly batch WOs, where the pool IS the job. It is wrong for a real
routing: WO-20260807-006 is a 4-operation weld assembly (Skid Fit -> Wall Fit Up
-> Accessory Fit Up -> Weld Out) whose first three operations sit on one weld
cell, so all three unlocked simultaneously and the floor lost the build order.
This column is the discriminator between the two, per work order.

THE TWO DEFAULTS ARE DIFFERENT ON PURPOSE
-----------------------------------------
``server_default false`` here vs ``default=True`` on the model. That asymmetry is
the entire data-migration story and it is deliberate:

- Every row that already exists when this runs backfills to ``false`` = pooled =
  the exact behavior it was released and scheduled under. An in-flight job does
  not change rules underneath the floor, and the batch WOs (WO-20260807-003 /
  -004, 18 operations each on one machine) keep showing every item on the kiosk.
- Every work order created after the app code ships is inserted by the ORM,
  which supplies ``default=True`` — a sequenced routing, the common case.

There is deliberately NO backfill pass and no ``UPDATE``: converting an existing
job is an explicit, audited flip of the field through ``PUT /work-orders/{id}``,
never a migration guessing at intent. The tamper-evident ``audit_log`` table is
untouched (this migration is pure DDL).

No effect on laser work orders
------------------------------
``is_laser_dispatch_work_order`` short-circuits ABOVE this flag at every seam and
is strictly fuller (it drops predecessor gating entirely, across work centers),
so the column's value is ignored on ``work_order_type='laser_cutting'`` rows.

Shape / compliance
------------------
``work_orders`` is an EXISTING table: it already carries the TenantMixin non-null
``company_id`` + index and predates 059, which enabled ROW LEVEL SECURITY on
every ``public`` table. So this additive migration needs no RLS statement of its
own — same rationale as 065/070/071.

Idempotent and reversible
-------------------------
- Upgrade guards the ADD COLUMN with ``_has_column`` so the create_all -> stamp
  -> upgrade bootstrap path (where the model already built the column) no-ops
  rather than erroring (precedent 065/070/071).
- Downgrade drops the column, guarded. On SQLite the drop runs in batch mode
  (recreates the table without it; precedent 063/064/065); on Postgres it is a
  plain guarded ``DROP COLUMN``. Dropping it returns every work order to pooled
  promotion — the pre-081 rule — which is why the downgrade needs no data step.

Dialect note
------------
Dialect-neutral DDL: no ``_is_postgres`` early return (precedent 068/070/071).

Locking / operations note
-------------------------
ADD COLUMN NOT NULL with a CONSTANT server default is metadata-only on
PostgreSQL 11+ (no table rewrite, no backfill pass; brief ACCESS EXCLUSIVE lock).
Deploy ordering: run before app code that reads/writes the column; old code
simply ignores it.

Revision id ``081_wo_sequential_operations`` is 28 chars (<= 32) per the
create_all -> stamp -> upgrade bootstrap constraint (alembic_version.version_num
is varchar(32) on a freshly bootstrapped DB); see docs/DEVELOPMENT.md.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "081_wo_sequential_operations"
down_revision = "080_restore_stamped_over_con"
branch_labels = None
depends_on = None

TABLE_NAME = "work_orders"
COLUMN_NAME = "sequential_operations"


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    return _inspector().has_table(table_name)


def _has_column(table_name: str, column_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(col["name"] == column_name for col in _inspector().get_columns(table_name))


def upgrade() -> None:
    # NOT NULL with a constant server default: every existing work order backfills
    # to False (= pooled, today's behavior) in place, metadata-only on PG 11+.
    # Guarded so the create_all bootstrap path (model already built the column)
    # no-ops rather than erroring.
    if not _has_column(TABLE_NAME, COLUMN_NAME):
        op.add_column(
            TABLE_NAME,
            sa.Column(
                COLUMN_NAME,
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )


def downgrade() -> None:
    if not _has_column(TABLE_NAME, COLUMN_NAME):
        return

    conn = op.get_bind()
    if conn.dialect.name == "sqlite":
        # Batch mode recreates the table without the column (precedent 063/064/065)
        # rather than relying on SQLite's version-gated ALTER ... DROP COLUMN.
        with op.batch_alter_table(TABLE_NAME) as batch_op:
            batch_op.drop_column(COLUMN_NAME)
    else:
        op.drop_column(TABLE_NAME, COLUMN_NAME)
