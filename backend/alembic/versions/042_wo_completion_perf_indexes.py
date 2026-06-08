"""Performance indexes for the WO completion reconcile / predecessor query paths (Batch 9 / PERF-1)

Revision ID: 042_wo_completion_perf_indexes
Revises: 041_uq_wo_inventory_idempotency
Create Date: 2026-06-08

Context
-------
The work-order completion / reconcile-on-read paths in
``app/services/work_order_state_service.py`` run two hot, repeated query shapes that
today have no supporting index and fall back to sequential scans on high-row tables:

    ix_time_entries_operation_clock_out
        BTREE ON time_entries (operation_id, clock_out)
        -> backs ``reconcile_work_orders_from_completion_evidence``:
             * the per-operation production/scrap rollups that
               ``WHERE operation_id IN (...) GROUP BY operation_id`` (the leading
               ``operation_id`` column serves the IN-list probe + grouping),
             * the closed-only rollup that adds ``AND clock_out IS NOT NULL``
               (the trailing ``clock_out`` column makes that an index-only filter),
             * the latest-entry scan
               ``... AND clock_out IS NOT NULL ORDER BY operation_id, clock_out DESC``
               (both columns of the ORDER BY are covered by the index, so no sort).

    ix_woo_work_order_sequence
        BTREE ON work_order_operations (work_order_id, sequence)
        -> backs ``has_incomplete_predecessors``
               (``WHERE work_order_id = ? AND sequence < ?``) and
           ``release_next_ready_operation``
               (``WHERE work_order_id = ? ORDER BY sequence``) -- the leading
           ``work_order_id`` equality plus the ordered ``sequence`` column serves both
           the range predicate and the ordering with no extra sort.

Both indexes are NON-unique btree indexes: they exist purely to speed reads on the
completion path. They do NOT enforce any invariant (unlike 041's partial UNIQUE
indexes), so -- unlike 041 -- there is NO pre-flight duplicate guard: there is
nothing to validate before the build, and the build cannot fail on existing data.

Locking / operations note
-------------------------
``time_entries`` and ``work_order_operations`` are both high-write tables (shop-floor
clock-ins/outs and operation status churn on a live multi-tenant DB), so each index
is built with ``CREATE INDEX CONCURRENTLY`` inside an autocommit block to avoid the
ACCESS EXCLUSIVE lock a plain ``CREATE INDEX`` would take, which would block writers
for the duration of the build during deploy. CONCURRENTLY cannot run inside a
transaction, hence the ``op.get_context().autocommit_block()``. The downgrade drops
both indexes CONCURRENTLY too so a rollback doesn't take ACCESS EXCLUSIVE either.

No deploy-ordering constraint: these are pure read-path indexes (metadata only --
they touch no tenant-isolation, audit, or soft-delete behavior), so they are safe to
apply before or after the backend rollout in any order.

Lock-step with the model ``__table_args__`` (load-bearing)
----------------------------------------------------------
Following the precedent set in 041, the model classes declare the identical indexes
so the ``create_all`` bootstrap path produces them byte-for-byte:
``WorkOrderOperation.__table_args__`` declares ``ix_woo_work_order_sequence`` and
``TimeEntry.__table_args__`` declares ``ix_time_entries_operation_clock_out``. Keep
this migration and those model declarations in lock-step.

Bootstrap / revision-id-length note
-----------------------------------
Revision id is 30 chars (<= 32) per the create_all -> stamp -> upgrade bootstrap
constraint documented in docs/DEVELOPMENT.md (``alembic_version.version_num`` is
``varchar(32)`` on a freshly bootstrapped DB).

Idempotent (``_index_exists`` guard + ``if_not_exists`` / ``if_exists``) so a re-run
is a clean no-op, and reversible (downgrade drops both indexes, also CONCURRENTLY).
Non-Postgres (the SQLite local create_all / pytest path) is skipped gracefully:
``CREATE INDEX CONCURRENTLY`` is a Postgres feature and SQLite is not a concurrent
multi-writer target; on that path ``create_all`` already emits both indexes from the
model ``__table_args__`` declarations above.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "042_wo_completion_perf_indexes"
down_revision = "041_uq_wo_inventory_idempotency"
branch_labels = None
depends_on = None

# (index_name, table, columns). Non-unique btree indexes -- kept in lock-step with
# the model __table_args__ (TimeEntry / WorkOrderOperation) so create_all matches.
TIME_ENTRY_TABLE = "time_entries"
TIME_ENTRY_INDEX = "ix_time_entries_operation_clock_out"
TIME_ENTRY_COLUMNS = ["operation_id", "clock_out"]

WOO_TABLE = "work_order_operations"
WOO_INDEX = "ix_woo_work_order_sequence"
WOO_COLUMNS = ["work_order_id", "sequence"]


def _is_postgres(conn) -> bool:
    return conn.dialect.name == "postgresql"


def _index_validity(conn, index_name: str) -> str:
    """Return 'valid' | 'invalid' | 'absent' for a Postgres index (by name).

    Validity-aware on purpose. An interrupted ``CREATE INDEX CONCURRENTLY`` (deploy
    killed, statement_timeout, lock cancel mid-build) leaves an **INVALID** index
    (``pg_index.indisvalid = false``) behind. That dead index still shows up in both
    ``inspector.get_indexes`` AND ``pg_indexes`` (neither filters on validity), so a
    plain existence probe would report it present -- and then BOTH the
    ``if not _index_exists(...)`` wrapper and ``if_not_exists=True`` would skip the
    rebuild, masking the dead index permanently: the planner ignores it (no read
    speedup -- the whole point of these indexes) while it still costs on every write.
    By reading ``indisvalid`` we can tell an interrupted build apart from a healthy one
    and rebuild it (see ``_ensure_index``).
    """
    row = conn.execute(
        sa.text(
            "SELECT i.indisvalid "
            "FROM pg_class c "
            "JOIN pg_index i ON i.indexrelid = c.oid "
            "WHERE c.relname = :name AND c.relkind = 'i'"
        ),
        {"name": index_name},
    ).fetchone()
    if row is None:
        return "absent"
    return "valid" if row[0] else "invalid"


def _ensure_index(table_name: str, index_name: str, columns) -> None:
    """Idempotently build a CONCURRENTLY index, self-healing a masked INVALID one.

    Caller must already be inside an ``autocommit_block`` (CONCURRENTLY cannot run in a
    transaction). If a prior interrupted build left an INVALID index of this name, drop
    it CONCURRENTLY first (``if_not_exists`` would otherwise no-op on the dead name and
    never rebuild), then create. A valid index is left untouched.
    """
    conn = op.get_bind()
    state = _index_validity(conn, index_name)
    if state == "invalid":
        op.drop_index(
            index_name,
            table_name=table_name,
            postgresql_concurrently=True,
            if_exists=True,
        )
        state = "absent"
    if state == "absent":
        op.create_index(
            index_name,
            table_name,
            columns,
            unique=False,
            postgresql_concurrently=True,
            if_not_exists=True,
        )


def upgrade() -> None:
    conn = op.get_bind()

    if not _is_postgres(conn):
        # CREATE INDEX CONCURRENTLY is a Postgres feature. On SQLite (local dev /
        # test create_all path) we skip; create_all already emits both indexes from
        # the model __table_args__, and SQLite is not a concurrent multi-writer
        # target so the non-concurrent build there is harmless.
        return

    # Build each index CONCURRENTLY in an autocommit block so we never take ACCESS
    # EXCLUSIVE on these high-write tables. CONCURRENTLY cannot run in a transaction.
    # _ensure_index is idempotent and self-heals an INVALID index from an interrupted
    # prior build.
    with op.get_context().autocommit_block():
        _ensure_index(TIME_ENTRY_TABLE, TIME_ENTRY_INDEX, TIME_ENTRY_COLUMNS)
        _ensure_index(WOO_TABLE, WOO_INDEX, WOO_COLUMNS)


def downgrade() -> None:
    conn = op.get_bind()

    if not _is_postgres(conn):
        return

    # Drop CONCURRENTLY too so rollback doesn't take ACCESS EXCLUSIVE on the
    # high-write tables. Must run outside a transaction. ``if_exists=True`` makes this a
    # no-op when absent, and it drops an INVALID leftover index too (DROP does not care
    # about indisvalid).
    with op.get_context().autocommit_block():
        if _index_validity(conn, WOO_INDEX) != "absent":
            op.drop_index(
                WOO_INDEX,
                table_name=WOO_TABLE,
                postgresql_concurrently=True,
                if_exists=True,
            )
        if _index_validity(conn, TIME_ENTRY_INDEX) != "absent":
            op.drop_index(
                TIME_ENTRY_INDEX,
                table_name=TIME_ENTRY_TABLE,
                postgresql_concurrently=True,
                if_exists=True,
            )
