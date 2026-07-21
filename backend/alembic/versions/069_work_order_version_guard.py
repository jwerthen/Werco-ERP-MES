"""Guard work_orders.version before the WorkOrder version_id_col mapping ships

Revision ID: 069_work_order_version_guard
Revises: 068_operation_run_order
Create Date: 2026-07-21

Context
-------
Optimistic locking on ``WorkOrder`` is currently fake: ``WorkOrderUpdate.version``
is required and the frontend sends it, but the ORM model
(app/models/work_order.py::WorkOrder) never mapped the column -- the endpoint
blind-setattrs it as a transient attribute and every response serializes
``version: 0``. The same release as this migration maps the column for real,
mirroring the fixed precedent on ``WorkOrderOperation`` / ``TimeEntry``::

    version = Column(Integer, nullable=False, server_default="1", default=1)
    __mapper_args__ = {"version_id_col": version}

SQLAlchemy's native ``version_id_col`` REQUIRES every row to carry a non-null
managed version: an UPDATE of a row whose ``version IS NULL`` fails / breaks the
version comparison, and with ``version_id_col`` EVERY WorkOrder write path
(release/start/complete, priority, kiosk status flips, state-service reconcile,
soft delete/restore, migration import) becomes a locked write.

``004_add_optimistic_locking`` already added ``version`` to ``work_orders``
(NOT NULL, server_default '1', plus index ``ix_work_orders_version``) on every
normally-migrated DB, so no row should be NULL there. This revision is the same
belt-and-suspenders guard ``038_optimistic_lock_backfill`` applied to
``work_order_operations`` / ``time_entries`` before THEIR version_id_col mapping
shipped, scoped to ``work_orders`` only:

  1. if the column is missing entirely (004 skipped / partial bootstrap), add it
     with the exact shape 004 would have created -- NOT NULL + server_default
     '1' is legal against populated rows because the default fills them;
  2. otherwise backfill ``version = 1 WHERE version IS NULL`` (no data
     destroyed; expected to match ZERO rows on a normally-migrated DB), then
     re-assert NOT NULL + server_default '1'.

The server_default stays PERMANENTLY (004/038/046/061 precedent -- never drop
it): it is what makes raw-SQL inserts and the create_all bootstrap converge on
version 1. The 004 index ``ix_work_orders_version`` is deliberately untouched
-- its lifecycle belongs to 004.

No backfill beyond NULL -> 1 (deliberate)
-----------------------------------------
The version counter has no meaningful absolute value -- only its
compare-and-increment semantics matter. Seeding every unversioned row at 1 is
the only honest value; deriving anything else would fabricate an edit history
nobody recorded. No other table is touched, and the tamper-evident
``audit_log`` table is untouched (invariant: never write it out of band).

Shape / compliance
------------------
``work_orders`` is an EXISTING table: it already carries the TenantMixin
non-null ``company_id`` + index, and RLS is already enabled on it -- 059
(``059_supabase_rls_hardening``) enabled ROW LEVEL SECURITY dynamically on
every ``public`` table. This migration creates NO table (no ``op.create_table``),
so the "ENABLE ROW LEVEL SECURITY on every new table" convention does not apply
and no RLS DDL is emitted (precedent 064/065/066/067/068).

Dialect decision
----------------
Plain transactional DDL/DML in 038's style: no ``_is_postgres`` early return
(unlike 066/067) and no ``batch_alter_table`` (038 used neither). The only
dialect-sensitive statement -- ``op.alter_column ... SET NOT NULL / SET
DEFAULT``, which SQLite cannot ALTER outside batch mode -- is guarded by
reflection: it only executes when the constraint or default is actually
missing. The SQLite dev/pytest databases are built by
``Base.metadata.create_all`` from the ORM (which now maps the column NOT NULL
+ server_default '1'), so a bootstrapped SQLite DB always reflects the
constraints as present and takes the guarded skip; only a genuinely broken
Postgres schema ever reaches the ALTER. Guard-by-inspection is the current
house hardening style (precedent 064/065/068).

Idempotent and reversible
-------------------------
Safe to re-run: every branch is inspector-guarded (missing table -> no-op,
missing column -> add, healthy column -> zero-row UPDATE + skipped ALTER).
Downgrade is a documented no-op, mirroring 038's posture: you cannot
"un-backfill" data, and the column lifecycle is owned by
``004_add_optimistic_locking`` on migrated DBs and by
``Base.metadata.create_all`` on bootstrapped DBs (the ORM model now maps the
column). Dropping the column here would orphan the version_id_col mapping. The
backfilled value (1) is correct under either mapping state.

Locking / operations note
-------------------------
On the normal path (004-migrated Postgres) this migration performs one
zero-row UPDATE and no DDL -- effectively free. The defensive ADD COLUMN
branch uses a constant default, which on PostgreSQL 11+ is a metadata-only
change (no table rewrite). Deploy ordering: run WITH or BEFORE the app deploy
that maps ``version_id_col`` on WorkOrder -- the mapping must never run
against a DB where ``work_orders.version`` can be NULL. Old code is unaffected
either way: the column has existed since 004 and pre-mapping code neither
selects nor writes it.

Revision id ``069_work_order_version_guard`` is 28 chars (<= 32) per the
create_all -> stamp -> upgrade bootstrap constraint (alembic_version.version_num
is varchar(32) on a freshly bootstrapped DB); see docs/DEVELOPMENT.md.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "069_work_order_version_guard"
down_revision = "068_operation_run_order"
branch_labels = None
depends_on = None

TABLE_NAME = "work_orders"
COLUMN_NAME = "version"


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    return _inspector().has_table(table_name)


def _reflected_column(table_name: str, column_name: str):
    """Reflected column dict for table.column, or None if table/column is absent."""
    if not _has_table(table_name):
        return None
    for col in _inspector().get_columns(table_name):
        if col["name"] == column_name:
            return col
    return None


def upgrade() -> None:
    if not _has_table(TABLE_NAME):
        # Partial bootstrap with no work_orders table at all: nothing to guard;
        # Base.metadata.create_all builds the column from the ORM mapping.
        return

    column = _reflected_column(TABLE_NAME, COLUMN_NAME)

    if column is None:
        # Defensive: if 004 was skipped for this table, add the column with the
        # same shape 004 would have created. NOT NULL + server_default '1' is
        # legal against populated rows (the default fills existing rows), and
        # keeps this migration self-sufficient for partially-migrated databases.
        op.add_column(
            TABLE_NAME,
            sa.Column(COLUMN_NAME, sa.Integer(), nullable=False, server_default="1"),
        )
        # Freshly added with server_default -> all rows are 1; nothing to backfill.
        return

    # 1. Backfill any NULL version values so version_id_col is safe.
    #    Parameter-free constant UPDATE; no rows are destroyed. On a normally
    #    004-migrated DB (NOT NULL since 004) this matches zero rows.
    op.execute(
        sa.text(f"UPDATE {TABLE_NAME} SET version = 1 WHERE version IS NULL")  # nosec B608 - table from module constant
    )

    # 2. Re-assert server_default '1' and NOT NULL (038's step 2), guarded by
    #    reflection so it is a true no-op -- and never reaches SQLite's
    #    unsupported non-batch ALTER -- when the constraints already hold.
    if column.get("nullable") is False and column.get("default") is not None:
        return

    op.alter_column(
        TABLE_NAME,
        COLUMN_NAME,
        existing_type=sa.Integer(),
        nullable=False,
        server_default="1",
    )


def downgrade() -> None:
    # The version column itself is owned by 004_add_optimistic_locking on
    # migrated DBs (and by Base.metadata.create_all on bootstrapped DBs, now
    # that the ORM maps it); this revision only backfilled data and re-asserted
    # constraints 004 already established, so there is nothing structural to
    # reverse here. We intentionally do NOT drop the column (that would orphan
    # the version_id_col mapping and is 004's responsibility). The backfilled
    # data is correct under either mapping state, so the downgrade is a safe
    # no-op.
    pass
