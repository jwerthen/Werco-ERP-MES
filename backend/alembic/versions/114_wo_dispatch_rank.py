"""Add work_orders.dispatch_rank and the two dispatch-board read indexes

Revision ID: 114_wo_dispatch_rank
Revises: 113_data_api_revoke_parity
Create Date: 2026-11-04

Context
-------
Dispatch Board v2 ("work-order order wins"): the planner sets ONE shop-wide order of
work orders and every machine queue follows it. This revision adds the storage for that
order plus the index the floor's "the order changed" notice probe needs. Three objects,
over two EXISTING tables, nothing else:

- ``work_orders.dispatch_rank``  Integer, NULLABLE, no server default.
  The work order's position in the company's plan (dense 1..N); NULL = not in the plan.
- ``ix_work_orders_company_dispatch_rank``  ``(company_id, dispatch_rank)``, non-unique.
  The board's "ranked work orders of this company, in plan order" read, tenant-led.
- ``ix_operational_events_company_entity_time``
  ``(company_id, entity_type, entity_id, occurred_at)``, non-unique.
  The per-work-center "latest ``dispatch_order_changed`` notice" probe that every kiosk /
  shop-floor queue poll runs (``entity_type='work_center'``, ``entity_id IN (...)``,
  recent ``occurred_at`` window, newest first).

``dispatch_rank`` is DISTINCT from ``work_order_operations.run_order`` (068 -- the order
of operations at ONE machine) and from ``priority``. Like ``run_order`` it is ADVISORY:
it changes the order operators see and never gates start / clock-in / complete.

No backfill, no default, no constraint (deliberate)
---------------------------------------------------
Every pre-existing work order is legitimately NULL -- "not placed in the plan by a
planner" is the true state of every historical and in-flight job, and inventing ranks
would assert a dispatch decision nobody made (the same reasoning as 068). Ranking is a
forward-only, planner-initiated act, written and audited by the service layer through
``AuditService``. Deliberately NOT unique-constrained: the plan is rewritten wholesale in
one transaction, so transient duplicates exist mid-rewrite and a unique index would fight
that rewrite for an advisory value. This revision writes ZERO rows; the tamper-evident
``audit_log`` table and its hash-chain columns are untouched (pure DDL).

Shape / compliance
------------------
Both tables EXIST and already carry the TenantMixin non-null ``company_id`` + index, and
both predate 059, which enabled ROW LEVEL SECURITY on every ``public`` table. This
revision creates no table, so the ENABLE-RLS new-table convention does not apply and no
RLS / REVOKE statement is emitted (precedent 065/070/071/081/083). Neither table is
soft-delete-relevant here: no row is touched.

Lock-step with the models (the stamped-over hazard)
---------------------------------------------------
All three objects are declared on the models -- ``WorkOrder.dispatch_rank`` plus the
named ``Index`` in ``WorkOrder.__table_args__`` (NOT ``index=True``: the read is
tenant-led), and the named ``Index`` in ``OperationalEvent.__table_args__`` -- so the
``create_all`` -> stamp -> upgrade bootstrap builds them too and can never stamp past
them. Names and column lists here must stay byte-identical to the models;
``tests/test_migration_114_wo_dispatch_rank.py`` pins that, and
``tests/test_migration_fresh_bootstrap.py``'s round-trip diffs the result against
``create_all``.

Idempotent and reversible
-------------------------
- Upgrade guards EACH object independently (088 style): the ADD COLUMN by
  ``_has_column``, each CREATE INDEX by name. On the create_all bootstrap path, where the
  models already built all three, every guard fires and the upgrade is a clean no-op; a
  hand-patched database holding only some of them gets exactly the missing ones.
- The index guard is VALIDITY-aware on PostgreSQL (precedent 042/078). An interrupted
  ``CREATE INDEX CONCURRENTLY`` -- the out-of-band build recommended below -- leaves an
  INVALID index that ``inspector.get_indexes`` still reports, so a plain name guard
  would skip it forever while the planner ignores it. ``_index_state`` reads
  ``pg_index.indisvalid``; an INVALID leftover is dropped and rebuilt, a valid one is
  left alone. On SQLite there is no such state and the probe is not run.
- Downgrade drops both indexes (by name, guarded) and THEN the column (guarded).
  Index-before-column matters on SQLite, whose batch-mode table rebuild would otherwise
  carry a dangling index definition. The column drop runs in batch mode on SQLite
  (recreates ``work_orders`` without it; precedent 063/064/065/083) and as a plain
  ``DROP COLUMN`` on PostgreSQL. Downgrading discards the planner's work-order order --
  advisory data, never production history; the audit rows that recorded each reorder
  survive.

Dialect note
------------
Dialect-neutral DDL in both directions: no ``_is_postgres`` early return (precedent
068/070/071/081/083). The only dialect branches are the Postgres validity probe and the
SQLite batch-mode column drop.

Locking / operations note
-------------------------
- ``ADD COLUMN`` nullable with no default is metadata-only on PostgreSQL: a brief ACCESS
  EXCLUSIVE lock on ``work_orders``, no rewrite, no backfill pass.
- ``CREATE INDEX`` (non-CONCURRENT) takes a SHARE lock for the build: reads proceed,
  WRITES to that table wait. ``work_orders`` is small (a non-event).
  ``operational_events`` is append-heavy (every operation start/complete/ready emits a
  row), so its build briefly blocks those emits; at this shop's volume that is expected
  to be well under a few seconds. If it is ever not -- or to deploy mid-shift -- build it
  out of band first and let the guard here no-op::

      CREATE INDEX CONCURRENTLY ix_operational_events_company_entity_time
          ON operational_events (company_id, entity_type, entity_id, occurred_at);

  (autocommit; if that build is interrupted, the validity-aware guard rebuilds it.)
- Deploy ordering: run BEFORE (or with) the app deploy that reads/writes
  ``dispatch_rank``. Old code neither selects nor writes the column, so the migration
  can land ahead of the code safely; new code against an un-migrated database would
  fail on the column, so the code must not ship first.

Revision id ``114_wo_dispatch_rank`` is 20 chars (<= 32) per the create_all -> stamp ->
upgrade bootstrap constraint (``alembic_version.version_num`` is varchar(32) on a freshly
bootstrapped DB); see docs/DEVELOPMENT.md.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "114_wo_dispatch_rank"
down_revision = "113_data_api_revoke_parity"
branch_labels = None
depends_on = None

TABLE_NAME = "work_orders"
COLUMN_NAME = "dispatch_rank"

WO_INDEX_NAME = "ix_work_orders_company_dispatch_rank"
EVENTS_TABLE_NAME = "operational_events"
EVENTS_INDEX_NAME = "ix_operational_events_company_entity_time"

# (table, index_name, columns) -- both non-unique, non-partial btree indexes, mirrored
# byte-for-byte on the models' ``__table_args__``. The work-order index is listed first:
# it names the column this revision adds, so it must be built after it and dropped
# before it.
INDEXES = [
    (TABLE_NAME, WO_INDEX_NAME, ["company_id", COLUMN_NAME]),
    (EVENTS_TABLE_NAME, EVENTS_INDEX_NAME, ["company_id", "entity_type", "entity_id", "occurred_at"]),
]

_INDEX_VALIDITY_SQL = (
    "SELECT i.indisvalid "
    "FROM pg_index i "
    "JOIN pg_class ic ON ic.oid = i.indexrelid "
    "JOIN pg_class tc ON tc.oid = i.indrelid "
    "WHERE ic.relname = :index_name AND tc.relname = :table_name "
    "AND pg_catalog.pg_table_is_visible(ic.oid)"
)


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


def _index_state(table_name: str, index_name: str) -> str:
    """``"absent"`` | ``"valid"`` | ``"invalid"`` for one index, by name.

    ``"invalid"`` is only ever reported on PostgreSQL, for an index an interrupted
    ``CREATE INDEX CONCURRENTLY`` left behind (``pg_index.indisvalid = false``). The
    inspector does not filter on validity, so without this probe the name guard would
    treat that dead index as present and never rebuild it (see the module docstring).
    """
    if not _has_index(table_name, index_name):
        return "absent"
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return "valid"
    row = bind.execute(
        sa.text(_INDEX_VALIDITY_SQL),
        {"index_name": index_name, "table_name": table_name},
    ).fetchone()
    if row is None or row[0]:
        return "valid"
    return "invalid"


def upgrade() -> None:
    # 1) The column. Nullable, no default -> metadata-only on PG, no backfill pass.
    #    Guarded so the create_all bootstrap path (model already built it) no-ops.
    if not _has_column(TABLE_NAME, COLUMN_NAME):
        op.add_column(TABLE_NAME, sa.Column(COLUMN_NAME, sa.Integer(), nullable=True))

    # 2) The indexes, each guarded on its own: create_all builds both, but a
    #    hand-patched database may hold the column without its index, or one index and
    #    not the other. An INVALID leftover (PG only) is dropped and rebuilt.
    for table_name, index_name, columns in INDEXES:
        state = _index_state(table_name, index_name)
        if state == "valid":
            continue
        if state == "invalid":
            op.drop_index(index_name, table_name=table_name)
        op.create_index(index_name, table_name, columns, unique=False)


def downgrade() -> None:
    # 1) Indexes first -- the work-order one names the column dropped below, and
    #    SQLite's batch-mode rebuild would otherwise carry a dangling index definition.
    #    A drop does not care whether the index is valid, so a plain name guard is
    #    enough here.
    for table_name, index_name, _columns in INDEXES:
        if _has_index(table_name, index_name):
            op.drop_index(index_name, table_name=table_name)

    # 2) The column.
    if not _has_column(TABLE_NAME, COLUMN_NAME):
        return

    if op.get_bind().dialect.name == "sqlite":
        # Batch mode recreates the table without the column (precedent 063/064/065/083)
        # rather than relying on SQLite's version-gated ALTER ... DROP COLUMN.
        with op.batch_alter_table(TABLE_NAME) as batch_op:
            batch_op.drop_column(COLUMN_NAME)
    else:
        op.drop_column(TABLE_NAME, COLUMN_NAME)
