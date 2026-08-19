"""Add unit_number to work_orders

Revision ID: 083_wo_unit_number
Revises: 082_vendor_active_before_delete
Create Date: 2026-08-19

Context
-------
Adds ONE nullable column plus its index to the existing ``work_orders`` table
(app/models/work_order.py::WorkOrder):

- ``unit_number`` (String(50), NULLABLE, indexed ``ix_work_orders_unit_number``) —
  the build identity of a one-unit-per-work-order job.

Why the column exists
---------------------
The weld-assembly work orders are built one unit at a time and the shop tracks
WHICH unit each work order is building. That number was typed into
``work_orders.notes``. The 2026-08-14 kiosk work made notes visible on the floor
(``_job_guidance_fields``), which fixed "the welder cannot read it at all" but not
"the welder has to read a paragraph to find it": the value stayed unsearchable,
absent from the work-order list card, and — the binding constraint — unshowable on
the wallboard, because ``notes`` is unbounded free text of exactly the class
``wallboard_service`` withholds from unattended screens. A bounded, purpose-specific
column is what makes the TV surface possible at all.

Why not an existing column
--------------------------
- ``serial_numbers`` is a JSON LIST validated as exactly one entry per unit
  (``count == quantity_ordered``) and is the switch that puts a work order into
  per-serial process-sheet capture. One unit id there would either break that
  validator or silently change how steps get recorded.
- ``lot_number`` is auto-assigned at completion as ``LOT-<wo_number>`` by
  ``completion_inventory_service._assign_finished_good_lot`` and drives FG receipt
  and backflush matching, so a hand-typed value would collide with it.

No constraint, no backfill
--------------------------
NULLABLE with no server default and deliberately NO unique index: most work orders
never carry a unit number, and a rework work order legitimately names the same unit
as the original. Existing rows keep whatever is in their ``notes`` — nothing parses
it out. A backfill would mutate released records with no ``AuditService`` row
(invariant 2) and could not tell a unit number from any other text in a note, so
this is forward-only and the open jobs are re-keyed by hand.

The index exists because both search paths (``search_service.run_global_search``
and the ``GET /work-orders`` ``search`` param) match on the column.

Shape / compliance
------------------
``work_orders`` is an EXISTING table: it already carries the TenantMixin non-null
``company_id`` + index and predates 059, which enabled ROW LEVEL SECURITY on every
``public`` table. So this additive migration needs no RLS statement of its own —
same rationale as 065/070/071/081. The tamper-evident ``audit_log`` table is
untouched (pure DDL).

Idempotent and reversible
-------------------------
- Upgrade guards both the ADD COLUMN and the CREATE INDEX so the create_all ->
  stamp -> upgrade bootstrap path (where the model already built both) no-ops
  rather than erroring (precedent 065/070/071/081).
- Downgrade drops the index then the column, both guarded. On SQLite the column
  drop runs in batch mode (recreates the table without it; precedent 063/064/065);
  on Postgres it is a plain guarded ``DROP COLUMN``. No data step is needed —
  dropping it simply removes a field nothing else reads.

Dialect note
------------
Dialect-neutral DDL: no ``_is_postgres`` early return (precedent 068/070/071/081).

Locking / operations note
-------------------------
ADD COLUMN NULLABLE with no default is metadata-only on PostgreSQL (no rewrite, no
backfill pass; brief ACCESS EXCLUSIVE lock). The CREATE INDEX is a plain blocking
build — ``work_orders`` is small enough that this is a non-event; if it ever is
not, build it CONCURRENTLY out of band and let the guard here no-op.
Deploy ordering: run before app code that reads/writes the column; old code simply
ignores it.

Revision id ``083_wo_unit_number`` is 19 chars (<= 32) per the create_all -> stamp
-> upgrade bootstrap constraint (alembic_version.version_num is varchar(32) on a
freshly bootstrapped DB); see docs/DEVELOPMENT.md.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "083_wo_unit_number"
down_revision = "082_vendor_active_before_delete"
branch_labels = None
depends_on = None

TABLE_NAME = "work_orders"
COLUMN_NAME = "unit_number"
INDEX_NAME = "ix_work_orders_unit_number"


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    return _inspector().has_table(table_name)


def _has_column(table_name: str, column_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(col["name"] == column_name for col in _inspector().get_columns(table_name))


def _has_index(table_name: str, index_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(idx["name"] == index_name for idx in _inspector().get_indexes(table_name))


def upgrade() -> None:
    # Nullable with no default: metadata-only on PG, no backfill pass. Guarded so
    # the create_all bootstrap path (model already built the column) no-ops.
    if not _has_column(TABLE_NAME, COLUMN_NAME):
        op.add_column(
            TABLE_NAME,
            sa.Column(COLUMN_NAME, sa.String(length=50), nullable=True),
        )

    # Mirrors Column(index=True) on the model. Guarded separately: create_all
    # builds BOTH, but a hand-patched database may have the column and not the index.
    if not _has_index(TABLE_NAME, INDEX_NAME):
        op.create_index(INDEX_NAME, TABLE_NAME, [COLUMN_NAME], unique=False)


def downgrade() -> None:
    # Index first -- SQLite's batch-mode table rebuild below would otherwise carry
    # a dangling index definition for a column that no longer exists.
    if _has_index(TABLE_NAME, INDEX_NAME):
        op.drop_index(INDEX_NAME, table_name=TABLE_NAME)

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
