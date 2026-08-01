"""Restore the indexes the create_all+stamp bootstrap silently skipped
(2026-07-31 prod pg_indexes audit): 42 read-path indexes from migrations
001/003/020/026/027 that existed only in migration DDL, never in the models.

Revision ID: 079_restore_stamped_over_idx
Revises: 078_golive_perf_indexes
Create Date: 2026-07-31

The bootstrap-stamp drift story (why prod is missing these)
-----------------------------------------------------------
Prod was bootstrapped the documented way (docs/DEVELOPMENT.md -> Database
Migrations): ``Base.metadata.create_all()`` on an empty Postgres, then ``alembic
stamp`` at a baseline, then incremental upgrades. ``create_all`` only emits what
the MODELS declare -- so every index that was declared ONLY inside a migration at
or below the stamp point (and never mirrored in a model's ``__table_args__`` /
``Column(index=True)``) was silently skipped: the migration that would have
created it was stamped over, and nothing else ever built it.

A read-only pg_indexes audit of the live prod DB (2026-07-31, alembic_version =
078_golive_perf_indexes) confirmed the drift class precisely:

  * All eight 078 indexes exist and are VALID -- the model-mirrored deploy
    pattern (042/078) works.
  * Of migration 001's 35 indexes, only ``ix_work_orders_status`` and
    ``ix_work_orders_due_date`` exist -- exactly the two that happen to be
    mirrored as ``Column(index=True)`` on the WorkOrder model.
  * Also confirmed absent: 003's six "new ones not in migration 001"
    (``ix_time_entries_user_clock_in``, ``ix_inventory_transactions_part_created``,
    ``ix_po_receipts_status_received``, ``ix_audit_logs_resource_timestamp``,
    ``ix_ncrs_status_source``, ``ix_mrp_requirements_run_part``), 020's
    ``ix_documents_vendor_id``, 026's three tenancy composites
    (``ix_work_orders_company_status``, ``ix_parts_company_active``,
    ``ix_users_company_active``) and 027's ``ix_work_orders_company_due_date``.

This migration restores the curated set (below) AND -- the load-bearing half --
every restored index is now mirrored in the owning model's ``__table_args__``, so
``create_all`` reproduces them and a future stamp can never skip them again. The
migration INDEXES literals, the model declarations, and the column order are kept
in exact lock-step (the 042/078 convention); a drift-guard test enforces it.

What each index serves (grouped as 001 grouped them; sources noted)
-------------------------------------------------------------------
Work Orders (001; 026/027 tenancy composites):
    ix_work_orders_status_due_date   (status, due_date)      dispatch/late-WO lists
    ix_work_orders_created_at        (created_at)            recent-WO lists/reports
    ix_work_orders_customer_name     (customer_name)         customer job lookups
    ix_work_orders_actual_end        (actual_end)            completion-window reports
    ix_work_orders_company_status    (company_id, status)    tenant-scoped status lists
                                                             (026 "common query patterns")
    ix_work_orders_company_due_date  (company_id, due_date)  the late-work-orders query
                                                             (027's stated purpose)
Work Order Operations (001):
    ix_woo_work_center_status        (work_center_id, status) kiosk/dispatch queue per WC
    ix_woo_status                    (status)                 cross-WC status scans
    ix_woo_scheduled_start           (scheduled_start)        schedule-ordered reads
Time Entries (001; 003):
    ix_time_entries_user_clock_out   (user_id, clock_out)    open-entry / labor-by-user
    ix_time_entries_wc_clock_in      (work_center_id, clock_in) labor-by-work-center
    ix_time_entries_type_clock_in    (entry_type, clock_in)  downtime/setup analytics
    ix_time_entries_user_clock_in    (user_id, clock_in)     per-user timeline reads (003)
Inventory Items (001):
    ix_inventory_items_part_active   (part_id, is_active)    on-hand rollups per part
    ix_inventory_items_status        (status)                quarantine/hold filters
    ix_inventory_items_warehouse     (warehouse)             per-warehouse views
Inventory Transactions (001; 003):
    ix_inv_txn_part_type_created     (part_id, transaction_type, created_at)
                                     per-part per-movement-type history
    ix_inventory_transactions_part_created (part_id, created_at)
                                     part ledger history: exports.py (part filter +
                                     created_at range + ORDER BY created_at DESC),
                                     inventory.py ledger, prediction_service usage
                                     window (part_id = ? AND created_at >= cutoff)
NCRs (001):
    ix_ncrs_status                   (status)                open-NCR lists/gates
    ix_ncrs_status_created           (status, created_at)    status lists newest-first
    ix_ncrs_source                   (source)                source analytics
    ix_ncrs_disposition              (disposition)           disposition rollups
CARs (001):
    ix_cars_status                   (status)                open-CAR lists
    ix_cars_due_date                 (due_date)              overdue-CAR reads
Equipment (001):
    ix_equipment_next_cal_date       (next_calibration_date) calibration-due queue
    ix_equipment_status_active       (status, is_active)     active-gauge filters
Purchase Orders (001):
    ix_purchase_orders_status        (status)                open-PO lists
    ix_purchase_orders_vendor_status (vendor_id, status)     per-vendor open POs
    ix_purchase_orders_required_date (required_date)         arrivals / due-in reads
PO Receipts (001; 003):
    ix_po_receipts_status            (status)                receipt status filters
    ix_po_receipts_inspection_status (inspection_status)     inspection rollups
    ix_po_receipts_received_at       (received_at)           receiving history windows
    ix_po_receipts_status_received   (status, received_at)   THE inspection queue:
                                     receiving.py ~524-531 (WHERE status =
                                     PENDING_INSPECTION [AND received_at >= cutoff]
                                     ORDER BY received_at) and the status-filtered
                                     history view ~1437-1444 (same shape, DESC)
FAIs (001):
    ix_fais_status                   (status)                open-FAI lists
Cycle Counts (001):
    ix_cycle_counts_status_scheduled (status, scheduled_date) count schedule queue
Quotes (001):
    ix_quotes_status                 (status)                quote pipeline lists
    ix_quotes_updated_at             (updated_at)            recently-touched quotes
Audit Logs (003):
    ix_audit_logs_resource_timestamp (resource_type, resource_id, timestamp)
                                     per-record audit-history reads ("everything
                                     that happened to WO 123, in order")
MRP (003):
    ix_mrp_requirements_run_part     (mrp_run_id, part_id)   per-run requirement loads
Documents (020):
    ix_documents_vendor_id           (vendor_id)             vendor document lists (the
                                     020 vendor<->documents association)
Parts / Users (026 tenancy composites):
    ix_parts_company_active          (company_id, is_active) tenant-scoped active parts
    ix_users_company_active          (company_id, is_active) tenant-scoped active users

Curation decisions (what is deliberately NOT restored, and why)
---------------------------------------------------------------
SKIPPED -- already present in prod (model ``Column(index=True)``):
    ix_work_orders_status, ix_work_orders_due_date (001). Restoring them would
    just no-op, and their model declarations already prevent recurrence.
SKIPPED -- fully redundant with a confirmed-present index:
    ix_inv_txn_created_at (001, single-column created_at). Prod already has
    ``ix_inventory_transactions_created_at`` on the same single column (the
    model's ``created_at`` is ``index=True``). Two names, one shape -- restoring
    it would double-index the highest-write column in the ledger for nothing.
SKIPPED -- no serving query, prefix-covered:
    ix_ncrs_status_source (003, (status, source)). No query in the app filters
    status AND source together (the NCR list endpoint filters status and part_id
    only; source appears alone in report_builder). Its (status) prefix is fully
    covered by the restored ix_ncrs_status, and source-only reads are covered by
    the restored ix_ncrs_source. Genuinely redundant with the restored set.
KEPT after the same scrutiny:
    ix_inventory_transactions_part_created (003, (part_id, created_at)): NOT
    redundant with 001's 3-col (part_id, transaction_type, created_at) -- with
    transaction_type in the middle, that index cannot serve a created_at range /
    ORDER BY after only a part_id equality probe, and the hot part-history reads
    (exports, ledger, prediction cutoff window) filter exactly (part_id,
    created_at) with NO transaction_type. The confirmed-present single-column
    part_id index would leave a heap filter + sort on a table that grows with
    every stock movement. Both composites restored; they serve different shapes.
    ix_po_receipts_status_received: exact-shape match for the inspection queue
    (see table above) -- not derivable from the two restored singles without a
    bitmap-AND plus sort.
    ix_mrp_requirements_run_part: mrp_requirements has NO index on mrp_run_id at
    all (only the 026 company_id index), so every per-run requirements load
    (the MRPRun.requirements relationship IN-probe) scans; the leading column
    fixes that and the trailing part_id matches the natural (run, part) grain.

audit_logs note (why touching this table is safe)
-------------------------------------------------
``audit_logs`` carries the 008/060 DB-level triggers that refuse UPDATE and
DELETE -- the tamper-evidence layer. ``CREATE INDEX`` is DDL on the table's
access paths: it fires no row-level trigger, reads/writes no row, and rewrites no
data, so the triggers are unaffected and the hash-chain columns
(``sequence_number``, ``previous_hash``, ``integrity_hash``) are untouched. This
migration performs ZERO data statements anywhere; its only raw SQL is the
read-only ``pg_index.indisvalid`` probe.

Locking / operations note
-------------------------
Every build uses ``CREATE INDEX CONCURRENTLY`` inside an autocommit block (the
078 pattern), so no ACCESS EXCLUSIVE lock is ever taken -- writers are never
blocked, including on the high-write tables (time_entries,
inventory_transactions, audit_logs, work_order_operations). The cost is
duration: 42 CONCURRENTLY builds each scan their table twice, so this migration
will take noticeably longer than a normal deploy step. That is time, not risk:
it can run before or after the backend rollout in any order (pure read-path
metadata; no behavior change), and an interruption is self-healing (below).

Downgrade semantics: the downgrade drops all 42 CONCURRENTLY -- i.e. it returns
a stamped-bootstrap DB (prod) to its audited pre-079 state. On a fully-MIGRATED
DB (one that really ran 001/003/020/026/027, e.g. a dev DB), upgrade is a clean
no-op for anything already present, but downgrade WILL drop those older
migrations' indexes; re-upgrading restores them identically. Round-trip safe in
both environments.

Idempotency / self-heal (same guard as 042/078)
-----------------------------------------------
An interrupted ``CREATE INDEX CONCURRENTLY`` leaves an INVALID index that both
``inspector.get_indexes`` and ``if_not_exists=True`` would treat as present,
permanently masking the dead index. ``_ensure_index`` probes
``pg_index.indisvalid`` and drops+rebuilds an INVALID leftover; a valid index --
including one a real 001/003/020/026/027 run already built -- is left untouched,
so a re-run (and a run against a properly-migrated DB) is a clean no-op.

Lock-step with the model ``__table_args__`` (load-bearing)
----------------------------------------------------------
Every index below is now declared identically on its owning model so the
``create_all`` bootstrap path produces it too -- closing the drift class this
migration exists to fix:

    WorkOrder / WorkOrderOperation      app/models/work_order.py
    TimeEntry                           app/models/time_entry.py
    InventoryItem / InventoryTransaction / CycleCount
                                        app/models/inventory.py
    NonConformanceReport / CorrectiveActionRequest / FirstArticleInspection
                                        app/models/quality.py
    Equipment                           app/models/calibration.py
    PurchaseOrder / POReceipt           app/models/purchasing.py
    Quote                               app/models/quote.py
    AuditLog                            app/models/audit_log.py
    MRPRequirement                      app/models/mrp.py
    Document                            app/models/document.py
    Part                                app/models/part.py
    User                                app/models/user.py

Keep this migration and those declarations in lock-step (names, column order).

Non-Postgres (the SQLite local create_all / pytest path) early-returns in both
directions: CONCURRENTLY is a Postgres feature, and on that path ``create_all``
already emits every index from the model declarations above.

Revision id is 28 chars (<= 32) per the create_all -> stamp -> upgrade bootstrap
constraint (``alembic_version.version_num`` is varchar(32)).
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "079_restore_stamped_over_idx"
down_revision = "078_golive_perf_indexes"
branch_labels = None
depends_on = None

# (table, index_name, columns) -- non-unique full-table btree indexes, kept in
# lock-step with the owning model __table_args__ (see header) so create_all
# matches. Sources: migrations 001 (performance indexes), 003 (constraints +
# follow-up indexes), 020 (documents.vendor_id), 026/027 (tenancy composites).
INDEXES = [
    # Work Orders (001; 026; 027)
    ("work_orders", "ix_work_orders_status_due_date", ["status", "due_date"]),
    ("work_orders", "ix_work_orders_created_at", ["created_at"]),
    ("work_orders", "ix_work_orders_customer_name", ["customer_name"]),
    ("work_orders", "ix_work_orders_actual_end", ["actual_end"]),
    ("work_orders", "ix_work_orders_company_status", ["company_id", "status"]),
    ("work_orders", "ix_work_orders_company_due_date", ["company_id", "due_date"]),
    # Work Order Operations (001)
    ("work_order_operations", "ix_woo_work_center_status", ["work_center_id", "status"]),
    ("work_order_operations", "ix_woo_status", ["status"]),
    ("work_order_operations", "ix_woo_scheduled_start", ["scheduled_start"]),
    # Time Entries (001; 003)
    ("time_entries", "ix_time_entries_user_clock_out", ["user_id", "clock_out"]),
    ("time_entries", "ix_time_entries_wc_clock_in", ["work_center_id", "clock_in"]),
    ("time_entries", "ix_time_entries_type_clock_in", ["entry_type", "clock_in"]),
    ("time_entries", "ix_time_entries_user_clock_in", ["user_id", "clock_in"]),
    # Inventory Items (001)
    ("inventory_items", "ix_inventory_items_part_active", ["part_id", "is_active"]),
    ("inventory_items", "ix_inventory_items_status", ["status"]),
    ("inventory_items", "ix_inventory_items_warehouse", ["warehouse"]),
    # Inventory Transactions (001; 003)
    ("inventory_transactions", "ix_inv_txn_part_type_created", ["part_id", "transaction_type", "created_at"]),
    ("inventory_transactions", "ix_inventory_transactions_part_created", ["part_id", "created_at"]),
    # NCRs (001)
    ("ncrs", "ix_ncrs_status", ["status"]),
    ("ncrs", "ix_ncrs_status_created", ["status", "created_at"]),
    ("ncrs", "ix_ncrs_source", ["source"]),
    ("ncrs", "ix_ncrs_disposition", ["disposition"]),
    # CARs (001)
    ("cars", "ix_cars_status", ["status"]),
    ("cars", "ix_cars_due_date", ["due_date"]),
    # Equipment (001)
    ("equipment", "ix_equipment_next_cal_date", ["next_calibration_date"]),
    ("equipment", "ix_equipment_status_active", ["status", "is_active"]),
    # Purchase Orders (001)
    ("purchase_orders", "ix_purchase_orders_status", ["status"]),
    ("purchase_orders", "ix_purchase_orders_vendor_status", ["vendor_id", "status"]),
    ("purchase_orders", "ix_purchase_orders_required_date", ["required_date"]),
    # PO Receipts (001; 003)
    ("po_receipts", "ix_po_receipts_status", ["status"]),
    ("po_receipts", "ix_po_receipts_inspection_status", ["inspection_status"]),
    ("po_receipts", "ix_po_receipts_received_at", ["received_at"]),
    ("po_receipts", "ix_po_receipts_status_received", ["status", "received_at"]),
    # FAIs (001)
    ("fais", "ix_fais_status", ["status"]),
    # Cycle Counts (001)
    ("cycle_counts", "ix_cycle_counts_status_scheduled", ["status", "scheduled_date"]),
    # Quotes (001)
    ("quotes", "ix_quotes_status", ["status"]),
    ("quotes", "ix_quotes_updated_at", ["updated_at"]),
    # Audit Logs (003) -- index DDL only; triggers/hash chain untouched (see header)
    ("audit_logs", "ix_audit_logs_resource_timestamp", ["resource_type", "resource_id", "timestamp"]),
    # MRP (003)
    ("mrp_requirements", "ix_mrp_requirements_run_part", ["mrp_run_id", "part_id"]),
    # Documents (020)
    ("documents", "ix_documents_vendor_id", ["vendor_id"]),
    # Parts / Users (026 tenancy composites)
    ("parts", "ix_parts_company_active", ["company_id", "is_active"]),
    ("users", "ix_users_company_active", ["company_id", "is_active"]),
]


def _is_postgres(conn) -> bool:
    return conn.dialect.name == "postgresql"


def _index_validity(conn, index_name: str) -> str:
    """Return 'valid' | 'invalid' | 'absent' for a Postgres index (by name).

    Validity-aware on purpose (same rationale as 042/078). An interrupted
    ``CREATE INDEX CONCURRENTLY`` (deploy killed, statement_timeout, lock cancel
    mid-build) leaves an **INVALID** index (``pg_index.indisvalid = false``)
    behind. That dead index still shows up in both ``inspector.get_indexes`` AND
    ``pg_indexes`` (neither filters on validity), so a plain existence probe
    would report it present -- and then BOTH an existence guard and
    ``if_not_exists=True`` would skip the rebuild, masking the dead index
    permanently: the planner ignores it (no read speedup -- the whole point of
    these indexes) while it still costs on every write. By reading
    ``indisvalid`` we can tell an interrupted build apart from a healthy one and
    rebuild it (see ``_ensure_index``).
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

    Caller must already be inside an ``autocommit_block`` (CONCURRENTLY cannot
    run in a transaction). If a prior interrupted build left an INVALID index of
    this name, drop it CONCURRENTLY first (``if_not_exists`` would otherwise
    no-op on the dead name and never rebuild), then create. A VALID index --
    whether built by a previous 079 run or by a real 001/003/020/026/027 run on
    a properly-migrated DB -- is left untouched.
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
        # test create_all path) we skip; create_all already emits every index
        # from the model __table_args__ declarations this migration is mirrored
        # against, and SQLite is not a concurrent multi-writer target.
        return

    # Build each index CONCURRENTLY in an autocommit block so we never take
    # ACCESS EXCLUSIVE on live tables. CONCURRENTLY cannot run in a transaction.
    # _ensure_index is idempotent and self-heals an INVALID index from an
    # interrupted prior build. No table rows are read or written -- in
    # particular audit_logs' UPDATE/DELETE-refusing triggers (008/060) never
    # fire and its hash chain is untouched.
    with op.get_context().autocommit_block():
        for table_name, index_name, columns in INDEXES:
            _ensure_index(table_name, index_name, columns)


def downgrade() -> None:
    conn = op.get_bind()

    if not _is_postgres(conn):
        return

    # Drop CONCURRENTLY too so rollback doesn't take ACCESS EXCLUSIVE on the
    # live tables. Must run outside a transaction. ``if_exists=True`` makes this
    # a no-op when absent, and it drops an INVALID leftover index too (DROP does
    # not care about indisvalid). On a stamped-bootstrap DB (prod) this returns
    # the DB to its audited pre-079 state; on a fully-migrated DB it also drops
    # the older migrations' copies -- re-upgrading restores them identically.
    with op.get_context().autocommit_block():
        for table_name, index_name, _columns in reversed(INDEXES):
            if _index_validity(conn, index_name) != "absent":
                op.drop_index(
                    index_name,
                    table_name=table_name,
                    postgresql_concurrently=True,
                    if_exists=True,
                )
