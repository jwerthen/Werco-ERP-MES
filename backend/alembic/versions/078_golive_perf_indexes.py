"""Go-live scale-audit performance indexes (2026-07-31): labor costing, lot/serial
traceability, SPC measurement reads, per-part/WO document lookups, audit list view.

Revision ID: 078_golive_perf_indexes
Revises: 077_audit_seq_paused_chain
Create Date: 2026-07-31

Context
-------
A verified 2026-07-31 go-live scale audit confirmed eight hot read paths that have no
supporting index today and degrade to sequential scans as row counts grow. Each index
below documents the exact query shape it serves:

    ix_time_entries_work_order_clock_out
        BTREE ON time_entries (work_order_id, clock_out)
        -> job costing (app/services/job_costing_service.py ~102-106): the closed-labor
           rollup ``WHERE work_order_id = ? AND company_id = ? AND clock_out IS NOT
           NULL`` (leading equality probe + trailing clock_out null-test served from
           the index), and the per-WO duration SUM loop in
           app/api/endpoints/reports.py (~372-386):
           ``WHERE work_order_id = ? AND duration_hours IS NOT NULL``.
           ``time_entries.work_order_id`` currently has NO index at all (only
           ``operation_id`` got one, via 042), so every per-WO labor read scans.

    ix_inv_txn_company_lot          PARTIAL: WHERE lot_number IS NOT NULL
        BTREE ON inventory_transactions (company_id, lot_number)
        -> lot traceability (app/api/endpoints/traceability.py ~161):
           ``WHERE lot_number = ? AND company_id = ?``, and the ledger lot filter
           (app/api/endpoints/inventory.py ~1534). Partial on purpose: most ledger
           rows carry no lot, and every serving query implies
           ``lot_number IS NOT NULL`` (it filters on lot equality), so the predicate
           keeps the index small without excluding any serveable row.

    ix_inv_txn_company_serial       PARTIAL: WHERE serial_number IS NOT NULL
        BTREE ON inventory_transactions (company_id, serial_number)
        -> serial traceability (traceability.py ~465), same shape as the lot trace.

    ix_spc_measurements_char_subgroup
        BTREE ON spc_measurements (characteristic_id, subgroup_number, sample_number)
        -> the SPC read endpoints (app/api/endpoints/spc.py ~333/~360/~454/~532/~579):
           ``WHERE characteristic_id = ? ORDER BY subgroup_number, sample_number`` --
           the full key covers the filter AND the ORDER BY, so no sort node -- and the
           kiosk capture path's next-subgroup lookup
           (app/services/process_sheet_service.py ~1068-1076:
           ``ORDER BY subgroup_number DESC LIMIT 1``, an index-tip read via a backward
           scan on the same key).

    ix_documents_company_part       PARTIAL: WHERE part_id IS NOT NULL
        BTREE ON documents (company_id, part_id)
        -> the document list filters (app/api/endpoints/documents.py ~93-96) and the
           kiosk operation-open per-part controlled-drawing lookup
           (app/api/endpoints/shop_floor.py ~993-1001). Partial: association FKs on
           documents are sparse, and both call sites filter on part_id equality.

    ix_documents_company_work_order PARTIAL: WHERE work_order_id IS NOT NULL
        BTREE ON documents (company_id, work_order_id)
        -> the same call sites, filtered by work order instead of part.

    ix_audit_logs_company_timestamp
        BTREE ON audit_logs (company_id, timestamp)
        -> the audit list view (app/api/endpoints/audit.py ~60-85):
           ``WHERE company_id = ? ORDER BY timestamp DESC OFFSET ? LIMIT ?``, and the
           /summary cutoff counts (~102-141):
           ``WHERE company_id = ? AND timestamp >= ?`` (equality + range on the two
           key columns).

    ix_audit_logs_company_user_timestamp
        BTREE ON audit_logs (company_id, user_id, timestamp)
        -> the user-filtered audit view (audit.py ~68-69 plus the same
           ``ORDER BY timestamp DESC`` pagination).

All eight are NON-unique btree indexes: pure read-path speedups. They enforce no
invariant (unlike 041/076's partial UNIQUE indexes), so there is no pre-flight data
guard -- there is nothing to validate and the build cannot fail on existing data.

ASCENDING on purpose (the audit recommendation said "timestamp DESC")
---------------------------------------------------------------------
Every column is declared ASC, including the ``timestamp`` columns behind the audit
list's ``ORDER BY timestamp DESC``. Postgres serves a DESC ``ORDER BY`` from an
ascending btree with a backward index scan at identical cost, so a DESC column buys
nothing for a single-direction ordering -- and ascending keeps the migration and the
mirrored model ``__table_args__`` declarations dialect-clean (no
``postgresql_ops``/DESC modifiers that SQLite's create_all path would have to carry).

audit_logs triggers note (why touching this table is safe)
----------------------------------------------------------
``audit_logs`` carries DB-level triggers that refuse UPDATE and DELETE (migrations
008/060 -- the tamper-evidence layer). ``CREATE INDEX`` is DDL on the table's access
paths and fires no row-level trigger, touches no row, and rewrites no data, so those
triggers are unaffected and unviolated. This migration deliberately reads/writes ZERO
rows in every table it touches -- the integrity columns (``sequence_number``,
``previous_hash``, ``integrity_hash``) and the hash chain are untouched.

Locking / operations note
-------------------------
``time_entries``, ``inventory_transactions``, and ``audit_logs`` are high-write
tables on a live multi-tenant DB (shop-floor clock-ins/outs, every stock movement,
every audited state change), so each index is built with ``CREATE INDEX
CONCURRENTLY`` inside an autocommit block to avoid the ACCESS EXCLUSIVE lock a plain
``CREATE INDEX`` would take (which would block writers for the duration of each
build during deploy). CONCURRENTLY cannot run inside a transaction, hence
``op.get_context().autocommit_block()``. The downgrade drops CONCURRENTLY too, so a
rollback doesn't take ACCESS EXCLUSIVE either.

No deploy-ordering constraint: these are pure read-path indexes (metadata only --
no tenant-isolation, audit, or soft-delete behavior changes), safe to apply before
or after the backend rollout in any order.

Idempotency / self-heal (same guard as 042)
-------------------------------------------
An interrupted ``CREATE INDEX CONCURRENTLY`` (deploy killed, statement_timeout, lock
cancel mid-build) leaves an INVALID index behind that both ``inspector.get_indexes``
and ``if_not_exists=True`` would treat as present, permanently masking the dead
index. ``_ensure_index`` therefore probes ``pg_index.indisvalid`` and drops+rebuilds
an INVALID leftover; a valid index is left untouched, so a re-run is a clean no-op.

Lock-step with the model ``__table_args__`` (load-bearing)
----------------------------------------------------------
Following the 041/042 precedent, the owning models declare identical indexes so the
``create_all`` bootstrap path produces them too:

    TimeEntry.__table_args__            ix_time_entries_work_order_clock_out
    InventoryTransaction.__table_args__ ix_inv_txn_company_lot / ix_inv_txn_company_serial
                                        (both with postgresql_where AND sqlite_where,
                                        per the 076 dialect-parity convention)
    SPCMeasurement.__table_args__       ix_spc_measurements_char_subgroup
    Document.__table_args__             ix_documents_company_part /
                                        ix_documents_company_work_order
    AuditLog.__table_args__             ix_audit_logs_company_timestamp /
                                        ix_audit_logs_company_user_timestamp

Keep this migration and those model declarations in lock-step.

Bootstrap / revision-id-length note
-----------------------------------
Revision id is 23 chars (<= 32) per the create_all -> stamp -> upgrade bootstrap
constraint documented in docs/DEVELOPMENT.md (``alembic_version.version_num`` is
``varchar(32)`` on a freshly bootstrapped DB).

Non-Postgres (the SQLite local create_all / pytest path) is skipped gracefully:
``CREATE INDEX CONCURRENTLY`` is a Postgres feature and SQLite is not a concurrent
multi-writer target; on that path ``create_all`` already emits all eight indexes
from the model ``__table_args__`` declarations above (including the partial ones --
SQLite supports partial indexes, and the models declare ``sqlite_where``).
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "078_golive_perf_indexes"
down_revision = "077_audit_seq_paused_chain"
branch_labels = None
depends_on = None

# (table, index_name, columns, partial_where) -- non-unique btree indexes, kept in
# lock-step with the owning model __table_args__ (see header) so create_all matches.
# partial_where is the raw SQL predicate for a Postgres partial index, or None for a
# full-table index. Predicates MUST stay textually identical to the model
# declarations (postgresql_where/sqlite_where there).
INDEXES = [
    (
        "time_entries",
        "ix_time_entries_work_order_clock_out",
        ["work_order_id", "clock_out"],
        None,
    ),
    (
        "inventory_transactions",
        "ix_inv_txn_company_lot",
        ["company_id", "lot_number"],
        "lot_number IS NOT NULL",
    ),
    (
        "inventory_transactions",
        "ix_inv_txn_company_serial",
        ["company_id", "serial_number"],
        "serial_number IS NOT NULL",
    ),
    (
        "spc_measurements",
        "ix_spc_measurements_char_subgroup",
        ["characteristic_id", "subgroup_number", "sample_number"],
        None,
    ),
    (
        "documents",
        "ix_documents_company_part",
        ["company_id", "part_id"],
        "part_id IS NOT NULL",
    ),
    (
        "documents",
        "ix_documents_company_work_order",
        ["company_id", "work_order_id"],
        "work_order_id IS NOT NULL",
    ),
    (
        "audit_logs",
        "ix_audit_logs_company_timestamp",
        ["company_id", "timestamp"],
        None,
    ),
    (
        "audit_logs",
        "ix_audit_logs_company_user_timestamp",
        ["company_id", "user_id", "timestamp"],
        None,
    ),
]


def _is_postgres(conn) -> bool:
    return conn.dialect.name == "postgresql"


def _index_validity(conn, index_name: str) -> str:
    """Return 'valid' | 'invalid' | 'absent' for a Postgres index (by name).

    Validity-aware on purpose (same rationale as 042). An interrupted ``CREATE INDEX
    CONCURRENTLY`` (deploy killed, statement_timeout, lock cancel mid-build) leaves an
    **INVALID** index (``pg_index.indisvalid = false``) behind. That dead index still
    shows up in both ``inspector.get_indexes`` AND ``pg_indexes`` (neither filters on
    validity), so a plain existence probe would report it present -- and then BOTH an
    existence guard and ``if_not_exists=True`` would skip the rebuild, masking the
    dead index permanently: the planner ignores it (no read speedup -- the whole point
    of these indexes) while it still costs on every write. By reading ``indisvalid``
    we can tell an interrupted build apart from a healthy one and rebuild it (see
    ``_ensure_index``).
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


def _ensure_index(table_name: str, index_name: str, columns, partial_where=None) -> None:
    """Idempotently build a CONCURRENTLY index, self-healing a masked INVALID one.

    Caller must already be inside an ``autocommit_block`` (CONCURRENTLY cannot run in
    a transaction). If a prior interrupted build left an INVALID index of this name,
    drop it CONCURRENTLY first (``if_not_exists`` would otherwise no-op on the dead
    name and never rebuild), then create. A valid index is left untouched.

    Extends 042's helper with an optional ``partial_where`` (raw SQL string) for the
    partial indexes -- passed through as ``postgresql_where``.
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
        kwargs = {}
        if partial_where is not None:
            kwargs["postgresql_where"] = sa.text(partial_where)
        op.create_index(
            index_name,
            table_name,
            columns,
            unique=False,
            postgresql_concurrently=True,
            if_not_exists=True,
            **kwargs,
        )


def upgrade() -> None:
    conn = op.get_bind()

    if not _is_postgres(conn):
        # CREATE INDEX CONCURRENTLY is a Postgres feature. On SQLite (local dev /
        # test create_all path) we skip; create_all already emits all eight indexes
        # from the model __table_args__ declarations (partial ones included, via
        # sqlite_where), and SQLite is not a concurrent multi-writer target so the
        # non-concurrent build there is harmless.
        return

    # Build each index CONCURRENTLY in an autocommit block so we never take ACCESS
    # EXCLUSIVE on these high-write tables. CONCURRENTLY cannot run in a transaction.
    # _ensure_index is idempotent and self-heals an INVALID index from an interrupted
    # prior build. No table rows are read or written -- in particular audit_logs'
    # UPDATE/DELETE-refusing triggers (008/060) never fire and its hash chain is
    # untouched.
    with op.get_context().autocommit_block():
        for table_name, index_name, columns, partial_where in INDEXES:
            _ensure_index(table_name, index_name, columns, partial_where)


def downgrade() -> None:
    conn = op.get_bind()

    if not _is_postgres(conn):
        return

    # Drop CONCURRENTLY too so rollback doesn't take ACCESS EXCLUSIVE on the
    # high-write tables. Must run outside a transaction. ``if_exists=True`` makes
    # this a no-op when absent, and it drops an INVALID leftover index too (DROP
    # does not care about indisvalid).
    with op.get_context().autocommit_block():
        for table_name, index_name, _columns, _partial_where in reversed(INDEXES):
            if _index_validity(conn, index_name) != "absent":
                op.drop_index(
                    index_name,
                    table_name=table_name,
                    postgresql_concurrently=True,
                    if_exists=True,
                )
