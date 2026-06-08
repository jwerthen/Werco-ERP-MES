"""DB-enforced idempotency for work-order completion inventory writes (Batch 6 / INV)

Revision ID: 041_uq_wo_inventory_idempotency
Revises: 040_add_part_backflush_flag
Create Date: 2026-06-07

Context
-------
On work-order completion ``completion_inventory_service.py`` writes a RECEIVE
``InventoryTransaction`` for the finished goods and -- when the finished part opts
into backflush -- one negative ISSUE txn per BOM component. Idempotency is
currently ONLY an application-level check-then-insert:

    FG receipt   -> ``_existing_work_order_receipt``  : SELECT ... WHERE
                    company_id=? AND reference_type='work_order' AND
                    reference_id=<wo> AND transaction_type=RECEIVE
    backflush    -> ``_component_already_issued``     : SELECT ... WHERE
                    company_id=? AND reference_type='work_order' AND
                    reference_id=<wo> AND transaction_type=ISSUE AND part_id=<comp>

with NO database constraint backing it. Under concurrency -- two reconcile-on-read
GETs, or a live completion racing a reconcile GET (the reconcile path holds no WO
row lock) -- both callers can pass the existence check and both insert, so
finished-goods on-hand DOUBLES (and components double-issue). A compliance review
flagged this as a double-receive / double-issue hole.

This migration adds the missing invariant as two PARTIAL UNIQUE indexes that scope
the exact idempotency keys the service uses, so the second insert raises
``IntegrityError`` (the service catches it and no-ops):

    uq_wo_inventory_receipt
        UNIQUE ON inventory_transactions
            (company_id, reference_type, reference_id, transaction_type)
        WHERE reference_type = 'work_order' AND transaction_type = 'RECEIVE'
        -> at most one FG receipt per (company, work_order)

    uq_wo_inventory_issue
        UNIQUE ON inventory_transactions
            (company_id, reference_type, reference_id, transaction_type, part_id)
        WHERE reference_type = 'work_order' AND transaction_type = 'ISSUE'
        -> at most one backflush issue per (company, work_order, component part)

The partial predicate scopes the constraint to work-order-referenced rows ONLY, so
PO/SO receipts, manual adjustments, transfers, scrap, ships, counts, returns -- and
any non-work_order RECEIVE/ISSUE -- are completely unaffected.

Enum stored-value note (load-bearing)
-------------------------------------
``transaction_type`` is a NATIVE Postgres enum (``transactiontype``) built from
``TransactionType(str, enum.Enum)``. SQLAlchemy's default native-enum behavior
binds and stores the enum MEMBER NAME, not its ``str`` value -- i.e. inserting
``TransactionType.RECEIVE`` stores the label ``'RECEIVE'`` (verified: the bound
param is ``'RECEIVE'`` and ``pg_enum`` labels are uppercase ``RECEIVE``/``ISSUE``).
The service's ORM filters compile to ``transaction_type = 'RECEIVE'`` /
``= 'ISSUE'`` accordingly. The partial predicate below therefore uses the UPPERCASE
labels so the index actually applies to the rows the service writes. (Using the
lowercase ``str`` values would silently never match -> a useless index.) The model
``InventoryTransaction.__table_args__`` declares the identical indexes so the
``create_all`` bootstrap path produces them byte-for-byte; keep the two in lock-step.

Pre-flight duplicate guard (defensive, NON-destructive)
-------------------------------------------------------
Batch 6 is new, so in practice there should be NO work-order-referenced inventory
transactions yet and the index build succeeds immediately. But a unique-index build
FAILS hard if pre-existing duplicates exist, so we check first. Inventory
transactions are regulated, traceability-bearing records (AS9100D / CMMC-L2):
silently deduplicating them is NOT acceptable. So instead of deleting anything, if
any duplicate work-order RECEIVE or backflush-ISSUE groups are found this migration
RAISES with a clear, itemized error listing the offending
``(company_id, reference_id[, part_id])`` groups and their row ids, so an operator
resolves them DELIBERATELY before re-running. We never DELETE inventory rows.

Locking / operations note
-------------------------
``inventory_transactions`` is a high-write table, so each index is built with
``CREATE UNIQUE INDEX CONCURRENTLY`` inside an autocommit block to avoid an ACCESS
EXCLUSIVE lock that would block stock movements during deploy. CONCURRENTLY cannot
run inside a transaction, so the duplicate-detection SELECTs run first (read-only,
no lock impact) and the index builds run after in the autocommit block. The
downgrade drops both indexes CONCURRENTLY too so the rollback doesn't block writers.

No deploy-ordering constraint: the indexes only formalize an invariant the app
already enforces best-effort, so they are safe to apply before or after the backend
rollout. Once live, a losing concurrent insert surfaces as ``IntegrityError`` which
the service already treats as an idempotent no-op.

Idempotent (IF NOT EXISTS / inspector guard) and reversible (downgrade drops both
indexes, also CONCURRENTLY). Non-Postgres (the SQLite local create_all/test path)
is skipped gracefully -- partial indexes + CONCURRENTLY are Postgres features and
SQLite is not a concurrent multi-writer target; the model still emits a (full)
unique index there via create_all and the application-level guard still applies.

Revision id is 31 chars (<= 32) per the create_all -> stamp -> upgrade bootstrap
constraint documented in docs/DEVELOPMENT.md (alembic_version.version_num is
varchar(32) on a freshly bootstrapped DB).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector

# revision identifiers, used by Alembic.
revision = "041_uq_wo_inventory_idempotency"
down_revision = "040_add_part_backflush_flag"
branch_labels = None
depends_on = None

TABLE_NAME = "inventory_transactions"

# (index_name, columns, where-predicate). The predicate values are the UPPERCASE
# enum member names that Postgres stores for TransactionType.RECEIVE / .ISSUE --
# see the module docstring. Kept in lock-step with InventoryTransaction.__table_args__.
RECEIPT_INDEX = "uq_wo_inventory_receipt"
RECEIPT_COLUMNS = ["company_id", "reference_type", "reference_id", "transaction_type"]
RECEIPT_WHERE = "reference_type = 'work_order' AND transaction_type = 'RECEIVE'"

ISSUE_INDEX = "uq_wo_inventory_issue"
ISSUE_COLUMNS = ["company_id", "reference_type", "reference_id", "transaction_type", "part_id"]
ISSUE_WHERE = "reference_type = 'work_order' AND transaction_type = 'ISSUE'"


def _is_postgres(conn) -> bool:
    return conn.dialect.name == "postgresql"


def _index_exists(conn, index_name: str) -> bool:
    inspector = Inspector.from_engine(conn)
    try:
        existing = {ix["name"] for ix in inspector.get_indexes(TABLE_NAME)}
    except Exception:
        existing = set()
    if index_name in existing:
        return True
    # Belt-and-suspenders: query pg_indexes directly (covers reflection-cache misses
    # on a concurrently-built index).
    row = conn.execute(
        sa.text("SELECT 1 FROM pg_indexes WHERE indexname = :name"),
        {"name": index_name},
    ).fetchone()
    return row is not None


def _find_receipt_duplicates(conn):
    """Groups of >1 work-order RECEIVE txn sharing (company_id, reference_id)."""
    return conn.execute(
        sa.text(
            """
            SELECT company_id,
                   reference_id,
                   COUNT(*)              AS n,
                   MIN(id)               AS keep_id,
                   array_agg(id ORDER BY id) AS ids
            FROM inventory_transactions
            WHERE reference_type = 'work_order'
              AND transaction_type = 'RECEIVE'
            GROUP BY company_id, reference_id
            HAVING COUNT(*) > 1
            ORDER BY company_id, reference_id
            """
        )
    ).fetchall()


def _find_issue_duplicates(conn):
    """Groups of >1 backflush ISSUE txn sharing (company_id, reference_id, part_id)."""
    return conn.execute(
        sa.text(
            """
            SELECT company_id,
                   reference_id,
                   part_id,
                   COUNT(*)              AS n,
                   MIN(id)               AS keep_id,
                   array_agg(id ORDER BY id) AS ids
            FROM inventory_transactions
            WHERE reference_type = 'work_order'
              AND transaction_type = 'ISSUE'
            GROUP BY company_id, reference_id, part_id
            HAVING COUNT(*) > 1
            ORDER BY company_id, reference_id, part_id
            """
        )
    ).fetchall()


def _assert_no_duplicates(conn) -> None:
    """Fail LOUDLY (never delete) if pre-existing WO RECEIVE/ISSUE duplicates exist.

    Inventory transactions are regulated, traceability-bearing records; silent
    dedup is not acceptable. We list the offending groups (keeping the earliest
    min-id row is the intended manual resolution) so an operator resolves them
    deliberately, then re-runs the migration.
    """
    receipt_dupes = _find_receipt_duplicates(conn)
    issue_dupes = _find_issue_duplicates(conn)
    if not receipt_dupes and not issue_dupes:
        return

    lines = [
        "Cannot build work-order inventory idempotency indexes: pre-existing "
        "duplicate work-order inventory transactions were found. Inventory "
        "transactions are regulated records and are NOT auto-deleted. Resolve "
        "these groups manually (keep the earliest min-id row per group), then "
        "re-run the migration:",
    ]
    for r in receipt_dupes:
        lines.append(
            f"  FG RECEIVE duplicate: company_id={r.company_id} "
            f"reference_id(work_order)={r.reference_id} count={r.n} "
            f"keep_id(min)={r.keep_id} txn_ids={list(r.ids)}"
        )
    for r in issue_dupes:
        lines.append(
            f"  backflush ISSUE duplicate: company_id={r.company_id} "
            f"reference_id(work_order)={r.reference_id} part_id={r.part_id} "
            f"count={r.n} keep_id(min)={r.keep_id} txn_ids={list(r.ids)}"
        )
    message = "\n".join(lines)
    print(f"[041_uq_wo_inventory_idempotency] {message}")
    raise RuntimeError(message)


def upgrade() -> None:
    conn = op.get_bind()

    if not _is_postgres(conn):
        # Partial indexes + CONCURRENTLY are Postgres features. On SQLite (local
        # dev / test create_all path) we skip; create_all already emits a (full)
        # unique index from the model, the application-level guard still applies,
        # and SQLite is not a concurrent multi-writer target.
        return

    # ---- Phase 1: pre-flight duplicate guard (read-only, no lock impact) -------
    # Skipped if both indexes already exist (a prior successful run) -- nothing to
    # validate and the data already satisfies uniqueness.
    if not (_index_exists(conn, RECEIPT_INDEX) and _index_exists(conn, ISSUE_INDEX)):
        _assert_no_duplicates(conn)

    # ---- Phase 2: build the partial unique indexes (autocommit, CONCURRENTLY) --
    with op.get_context().autocommit_block():
        if not _index_exists(conn, RECEIPT_INDEX):
            op.create_index(
                RECEIPT_INDEX,
                TABLE_NAME,
                RECEIPT_COLUMNS,
                unique=True,
                postgresql_concurrently=True,
                postgresql_where=sa.text(RECEIPT_WHERE),
                if_not_exists=True,
            )
        if not _index_exists(conn, ISSUE_INDEX):
            op.create_index(
                ISSUE_INDEX,
                TABLE_NAME,
                ISSUE_COLUMNS,
                unique=True,
                postgresql_concurrently=True,
                postgresql_where=sa.text(ISSUE_WHERE),
                if_not_exists=True,
            )


def downgrade() -> None:
    conn = op.get_bind()

    if not _is_postgres(conn):
        return

    # Drop CONCURRENTLY too so rollback doesn't take ACCESS EXCLUSIVE on the
    # high-write table. Must run outside a transaction.
    with op.get_context().autocommit_block():
        if _index_exists(conn, ISSUE_INDEX):
            op.drop_index(
                ISSUE_INDEX,
                table_name=TABLE_NAME,
                postgresql_concurrently=True,
                if_exists=True,
            )
        if _index_exists(conn, RECEIPT_INDEX):
            op.drop_index(
                RECEIPT_INDEX,
                table_name=TABLE_NAME,
                postgresql_concurrently=True,
                if_exists=True,
            )
