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
  ``CREATE INDEX CONCURRENTLY`` -- which is how this revision builds the events index on
  PostgreSQL, see below -- leaves an INVALID index that ``inspector.get_indexes`` still
  reports, so a plain name guard would skip it forever while the planner ignores it.
  ``_index_state`` reads ``pg_index.indisvalid``; an INVALID leftover is dropped and
  rebuilt (``DROP INDEX CONCURRENTLY IF EXISTS`` then ``CREATE INDEX CONCURRENTLY IF NOT
  EXISTS`` for the events index), a valid one is left alone. On SQLite there is no such
  state and the probe is not run.
- A failed concurrent build is safe to re-run: the revision is stamped only after
  ``upgrade()`` returns, the column and the work-order index were already committed and
  their guards skip them, and the INVALID events index is rebuilt.
- Downgrade drops the events index first, then the work-order index, and THEN the
  column (each guarded). Index-before-column matters on SQLite, whose batch-mode table
  rebuild would otherwise carry a dangling index definition. The column drop runs in
  batch mode on SQLite (recreates ``work_orders`` without it; precedent
  063/064/065/083) and as a plain ``DROP COLUMN`` on PostgreSQL. Downgrading discards the
  planner's work-order order -- advisory data, never production history; the audit rows
  that recorded each reorder survive.

Dialect note
------------
Dialect-neutral DDL for the column and the work-order index in both directions: no
``_is_postgres`` early return (precedent 068/070/071/081/083). The dialect branches are
the Postgres validity probe and ``lock_timeout``, the Postgres CONCURRENTLY build/drop of
the events index (precedent 042/078/079), and the SQLite batch-mode column drop. SQLite
builds and drops the events index with a plain ``CREATE`` / ``DROP INDEX``.

Locking / operations note
-------------------------
``alembic/env.py`` runs every pending revision in ONE transaction, and PostgreSQL holds a
lock until COMMIT, so what matters is which lock is held while what runs. On PostgreSQL:

- In the migration transaction: ``SET LOCAL lock_timeout = '5s'``, then ``ADD COLUMN``
  (nullable, no default: metadata-only, no rewrite, no backfill) and the small
  ``ix_work_orders_company_dispatch_rank`` build. ``ADD COLUMN`` takes an ACCESS
  EXCLUSIVE lock on ``work_orders`` -- reads included -- for the rest of that
  transaction, which is only the ``ADD COLUMN`` and that small index build.
- ``op.get_context().autocommit_block()`` then COMMITS that transaction, releasing the
  ``work_orders`` lock, and builds ``ix_operational_events_company_entity_time`` with
  ``CREATE INDEX CONCURRENTLY IF NOT EXISTS`` outside any transaction. A concurrent build
  blocks neither reads nor event writes on ``operational_events`` (it only waits for
  transactions already running to finish), so no request stalls on it however large the
  table has grown. If it is interrupted it leaves an INVALID index that the next run
  drops and rebuilds (above).
- The events index is deliberately NOT built first. Built inside the transaction before
  ``ADD COLUMN`` it would hold SHARE on ``operational_events`` while ``ADD COLUMN`` waits
  for requests that read ``work_orders`` and then emit an event -- requests that are
  themselves waiting on that SHARE lock: a deadlock the migration loses.
- ``lock_timeout`` is ``SET LOCAL`` (precedent 059/060 set it for the same reason): if a
  long transaction holds ``work_orders``, ``ADD COLUMN`` fails after 5 s instead of
  queueing -- and queueing every later ``work_orders`` query behind it -- for as long as
  that transaction runs. The deploy then fails fast and a re-run is safe. LOCAL scopes it
  to the migration transaction, so it ends at the COMMIT above and never reaches the
  concurrent build (whose waits on older transactions are expected and harmless). It is
  also reset (``SET LOCAL lock_timeout = DEFAULT``) as soon as the work_orders DDL is
  done, because when the events index is already valid no COMMIT follows inside this
  revision and the same transaction carries on into any later revision in the run.
- Pre-building the events index out of band is no longer needed; it remains harmless
  (the guard no-ops on a valid index)::

      CREATE INDEX CONCURRENTLY ix_operational_events_company_entity_time
          ON operational_events (company_id, entity_type, entity_id, occurred_at);

- Downgrade mirrors it: the events index is dropped CONCURRENTLY in an autocommit block
  before this revision takes any lock on ``work_orders``; then, in a fresh transaction
  under the same ``lock_timeout``, the work-order index and the column are dropped.
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
WO_INDEX_COLUMNS = ["company_id", COLUMN_NAME]
EVENTS_TABLE_NAME = "operational_events"
EVENTS_INDEX_NAME = "ix_operational_events_company_entity_time"
EVENTS_INDEX_COLUMNS = ["company_id", "entity_type", "entity_id", "occurred_at"]

# (table, index_name, columns) -- both non-unique, non-partial btree indexes, mirrored
# byte-for-byte on the models' ``__table_args__``. The work-order index is listed first:
# it names the column this revision adds, so it must be built after it and dropped
# before it. On PostgreSQL the two are built differently (see the module docstring).
INDEXES = [
    (TABLE_NAME, WO_INDEX_NAME, WO_INDEX_COLUMNS),
    (EVENTS_TABLE_NAME, EVENTS_INDEX_NAME, EVENTS_INDEX_COLUMNS),
]

# PostgreSQL only, and LOCAL: it lasts until the migration transaction ends, so it bounds
# the wait for the work_orders locks and never reaches the concurrent build (which runs
# after the autocommit block's COMMIT). Same value as 059/060.
LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = '5s'"
# ...and handed back as soon as that DDL is done. When no COMMIT follows inside this
# revision (the events index is already valid, e.g. pre-built out of band), the migration
# transaction carries on into any later revision in the same ``alembic upgrade head`` run,
# which must get its normal lock_timeout -- not this revision's 5 s bound.
RESET_LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = DEFAULT"

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


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _ensure_index_in_transaction(table_name: str, index_name: str, columns, state: str) -> None:
    """Plain (transactional) build of one index in ``state``; an INVALID leftover is rebuilt."""
    if state == "valid":
        return
    if state == "invalid":
        op.drop_index(index_name, table_name=table_name)
    op.create_index(index_name, table_name, columns, unique=False)


def _ensure_index_concurrently(table_name: str, index_name: str, columns, state: str) -> None:
    """PostgreSQL ``CONCURRENTLY`` build of one index in ``state`` (precedent 042).

    Caller must already be inside ``op.get_context().autocommit_block()``: CONCURRENTLY
    cannot run in a transaction. An INVALID leftover of an interrupted build is dropped
    CONCURRENTLY first -- ``IF NOT EXISTS`` alone would no-op on the dead name and never
    rebuild it.
    """
    if state == "valid":
        return
    if state == "invalid":
        op.drop_index(index_name, table_name=table_name, postgresql_concurrently=True, if_exists=True)
    op.create_index(index_name, table_name, columns, unique=False, postgresql_concurrently=True, if_not_exists=True)


def upgrade() -> None:
    postgres = _is_postgres()

    # 1) The column and its small work-order index, in the migration transaction. Each is
    #    guarded on its own: create_all builds both (the bootstrap path no-ops), but a
    #    hand-patched database may hold the column without its index. The column is
    #    nullable with no default -> metadata-only on PG, no backfill pass.
    add_column = not _has_column(TABLE_NAME, COLUMN_NAME)
    wo_index_state = _index_state(TABLE_NAME, WO_INDEX_NAME)
    bound_lock_wait = postgres and (add_column or wo_index_state != "valid")
    if bound_lock_wait:
        # Bound the wait for the work_orders locks; see LOCK_TIMEOUT_SQL.
        op.execute(LOCK_TIMEOUT_SQL)
    if add_column:
        op.add_column(TABLE_NAME, sa.Column(COLUMN_NAME, sa.Integer(), nullable=True))
    _ensure_index_in_transaction(TABLE_NAME, WO_INDEX_NAME, WO_INDEX_COLUMNS, wo_index_state)
    if bound_lock_wait:
        op.execute(RESET_LOCK_TIMEOUT_SQL)

    # 2) The events index. Nothing to do on the bootstrap path, so no early COMMIT there.
    events_state = _index_state(EVENTS_TABLE_NAME, EVENTS_INDEX_NAME)
    if events_state == "valid":
        return
    if not postgres:
        _ensure_index_in_transaction(EVENTS_TABLE_NAME, EVENTS_INDEX_NAME, EVENTS_INDEX_COLUMNS, events_state)
        return
    # The autocommit block COMMITS the migration transaction first, releasing the ACCESS
    # EXCLUSIVE lock ADD COLUMN took on work_orders, then builds CONCURRENTLY: neither
    # work_orders reads nor operational_events reads/writes wait on this build. Do NOT move
    # this build ahead of step 1 (see the module docstring: that order deadlocks).
    with op.get_context().autocommit_block():
        _ensure_index_concurrently(EVENTS_TABLE_NAME, EVENTS_INDEX_NAME, EVENTS_INDEX_COLUMNS, events_state)


def downgrade() -> None:
    postgres = _is_postgres()

    # 1) The events index first: on PG dropped CONCURRENTLY in an autocommit block, before
    #    this revision takes any lock on work_orders. A drop does not care whether the
    #    index is valid, so a plain name guard is enough here.
    if _has_index(EVENTS_TABLE_NAME, EVENTS_INDEX_NAME):
        if postgres:
            with op.get_context().autocommit_block():
                op.drop_index(
                    EVENTS_INDEX_NAME, table_name=EVENTS_TABLE_NAME, postgresql_concurrently=True, if_exists=True
                )
        else:
            op.drop_index(EVENTS_INDEX_NAME, table_name=EVENTS_TABLE_NAME)

    # 2) The work-order index BEFORE the column it names -- SQLite's batch-mode rebuild
    #    would otherwise carry a dangling index definition -- and then the column.
    drop_wo_index = _has_index(TABLE_NAME, WO_INDEX_NAME)
    drop_column = _has_column(TABLE_NAME, COLUMN_NAME)
    bound_lock_wait = postgres and (drop_wo_index or drop_column)
    if bound_lock_wait:
        op.execute(LOCK_TIMEOUT_SQL)
    if drop_wo_index:
        op.drop_index(WO_INDEX_NAME, table_name=TABLE_NAME)

    if drop_column:
        if op.get_bind().dialect.name == "sqlite":
            # Batch mode recreates the table without the column (precedent 063/064/065/083)
            # rather than relying on SQLite's version-gated ALTER ... DROP COLUMN.
            with op.batch_alter_table(TABLE_NAME) as batch_op:
                batch_op.drop_column(COLUMN_NAME)
        else:
            op.drop_column(TABLE_NAME, COLUMN_NAME)

    if bound_lock_wait:
        op.execute(RESET_LOCK_TIMEOUT_SQL)
