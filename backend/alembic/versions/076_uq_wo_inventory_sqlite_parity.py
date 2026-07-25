"""uq_wo_inventory_*: restore the partial predicate on SQLite (dialect parity, no-op on Postgres)

Revision ID: 076_uq_wo_inventory_sqlite_parity
Revises: 075_inventory_txn_allocation_ref
Create Date: 2026-07-25

What this fixes
---------------
``uq_wo_inventory_receipt`` and ``uq_wo_inventory_issue`` (migration
``041_uq_wo_inventory_idempotency``, mirrored in
``app/models/inventory.py::InventoryTransaction.__table_args__``) declared ONLY
``postgresql_where``. That is correct on Postgres -- they are PARTIAL unique indexes
scoped to ``reference_type = 'work_order'``. On SQLite, which is what local dev and
the ENTIRE pytest suite run on, a ``postgresql_where`` is simply ignored, so both
declarations degraded into FULL unique indexes covering EVERY reference_type::

    Postgres (correct)   UNIQUE (company_id, reference_type, reference_id,
                                 transaction_type[, part_id])
                         WHERE reference_type = 'work_order'
                           AND transaction_type = 'RECEIVE' | 'ISSUE'

    SQLite (wrong)       UNIQUE (company_id, reference_type, reference_id,
                                 transaction_type[, part_id])
                         -- no predicate: applies to every row in the ledger

So the test/dev environment enforced a constraint production does NOT. That is a lie
in the harness independent of any feature: it invents failures for movements
Postgres accepts, and it can mask a real bug by having an accidental index catch
what the application layer was supposed to catch. Concretely, on SQLite only:

* Two compensating ``ADJUST`` rows against ONE ``po_receipt`` -- a correct-then-void,
  or a second correction of the same receipt -- collided on
  ``(company_id, 'po_receipt', receipt.id, ADJUST)``. Legal on Postgres.
* Any repeated movement under one reference: several ``SHIP`` rows for one shipment,
  several ``RECEIVE`` rows under one ``purchase_order``, and the incoming
  material-consumption path (``reference_type = 'work_order_operation'``, which is
  legitimately many-rows-per-operation) were all rejected.
* Work-order-referenced rows whose ``transaction_type`` is neither RECEIVE nor ISSUE
  (an ADJUST or SCRAP against a WO) were squeezed into ``uq_wo_inventory_receipt``.

The divergence was pinned as an executable fact in
``tests/test_migration_075_inventory_txn_allocation_ref.py`` when 075 landed; this
revision is the reviewed change that fixes it, as that test's note anticipated.

What does NOT change -- the guarantee these indexes exist for
-------------------------------------------------------------
This is a strict RELAXATION, and it relaxes ONLY rows the guards were never meant to
cover. For the rows they DO exist for -- ``reference_type = 'work_order'`` with
``transaction_type`` RECEIVE or ISSUE -- coverage is BIT-IDENTICAL before and after,
on both dialects: the predicate pins ``reference_type`` and ``transaction_type``, so
what remains uniquely constrained is exactly ``(company_id, reference_id)`` for the
FG receipt and ``(company_id, reference_id, part_id)`` for the backflush issue --
precisely the idempotency keys ``_existing_work_order_receipt`` and
``_component_already_issued`` use. A concurrent double-complete still cannot
double-receive finished goods or double-issue a backflushed component, on Postgres
(unchanged) and on SQLite (unchanged for these rows). Nothing about the backflush
idempotency invariant is weakened.

Postgres is a NO-OP -- deliberately
-----------------------------------
``upgrade`` and ``downgrade`` both return early unless the dialect is SQLite. The
Postgres indexes are already exactly right; dropping and rebuilding a UNIQUE index on
``inventory_transactions`` -- the hottest, append-only ledger table in the system --
for no behavioral change would take a real lock and create a window with NO
idempotency guard on the live double-receive race, for zero benefit. That risk is not
acceptable. This revision therefore has NO production DDL and NO production lock
impact whatsoever: on Postgres it advances ``alembic_version`` and nothing else.

Predicate literal -- verified per dialect
-----------------------------------------
The 041 predicates use the UPPERCASE enum MEMBER NAMES (``'RECEIVE'``/``'ISSUE'``),
not the lowercase ``str`` values, because SQLAlchemy's ``Enum`` binds ``enum.name``.
Before copying the literal across, this was verified to be true on SQLite too rather
than assumed from the Postgres case::

    Enum(TransactionType).dialect_impl(d).bind_processor(d)
        postgresql : TransactionType.RECEIVE -> 'RECEIVE'   (native enum label)
        sqlite     : TransactionType.RECEIVE -> 'RECEIVE'   (VARCHAR(8))

    ORM filter compiled with literal_binds, both dialects:
        WHERE inventory_transactions.transaction_type = 'RECEIVE'

    An ORM-inserted row on SQLite stores: typeof() = 'text', value = 'RECEIVE'

Both dialects bind the SAME literal, so ONE predicate string is correct for both and
no per-dialect predicate is needed. Had they differed, the ``sqlite_where`` would
have had to be written separately -- the reason this was checked rather than
copy-pasted. The literals here are duplicated from the model on purpose: a migration
must stay frozen against future model edits (the 041 precedent). The migration test
asserts the two stay in lock-step.

Idempotent
----------
Every step is reflection-guarded. ``sqlalchemy.inspect(...).get_indexes()`` reports a
SQLite partial index's predicate as ``dialect_options['sqlite_where']`` (verified: it
yields exactly the WHERE text, e.g. ``reference_type = 'work_order' AND
transaction_type = 'RECEIVE'``), so this revision can tell a correct partial index
from a degraded full one and rebuild ONLY what is actually wrong:

* index missing entirely            -> create it, partial
* index present WITHOUT a predicate -> drop + recreate partial   (the fix)
* index present WITH the predicate  -> leave it alone            (no-op)

That last case is the normal one for a freshly bootstrapped DB: ``create_all`` now
builds these partial from the model, so ``create_all -> stamp -> upgrade`` finds them
already correct and does nothing. Re-running is likewise a no-op.

Reversible
----------
``downgrade`` genuinely restores the prior SQLite shape -- it drops both partial
indexes and recreates them as FULL unique indexes (no predicate), which is what a
pre-076 SQLite DB had. It is not a ``pass`` stub.

Because the full shape is STRICTER, the rollback can legitimately fail on data
written while 076 was applied (say a second consumption row for one operation --
accepted under the correct partial index, rejected by the old over-broad one). We do
NOT silently delete those rows to make the rollback fit: ``inventory_transactions``
is a regulated, traceability-bearing AS9100D/CMMC record. Instead the downgrade
pre-checks, and if it finds groups that the restored full index would reject it
RAISES with an itemized list of the offending ``(company_id, reference_type,
reference_id, transaction_type[, part_id])`` groups and their row ids, so an operator
resolves them deliberately. That is the same non-destructive posture 041 took with
its own pre-flight duplicate guard. On the expected rollback path -- no such rows --
the downgrade just runs.

Ordering / operations
---------------------
No deploy-ordering constraint and no coordination with any app rollout: on Postgres
this revision emits no DDL at all, and on SQLite it only relaxes a local-only index.
The ``audit_log`` hash chain is not touched, no table is created (so the
ENABLE-ROW-LEVEL-SECURITY convention does not apply), and no data statement runs --
no row is inserted, updated, or deleted.

Revision id vs filename
-----------------------
The revision id is ``076_uq_wo_inv_sqlite_parity`` (27 chars), abbreviated from the
filename: ``alembic_version.version_num`` is varchar(32) on a freshly bootstrapped
create_all -> stamp -> upgrade DB (docs/DEVELOPMENT.md), and the full
``076_uq_wo_inventory_sqlite_parity`` is 33. Descriptive filename + shortened
revision id follows the 051/052/053/054 precedent.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "076_uq_wo_inv_sqlite_parity"
down_revision = "075_inventory_txn_allocation_ref"
branch_labels = None
depends_on = None

TABLE_NAME = "inventory_transactions"

RECEIPT_INDEX = "uq_wo_inventory_receipt"
RECEIPT_COLUMNS = ["company_id", "reference_type", "reference_id", "transaction_type"]
RECEIPT_WHERE = "reference_type = 'work_order' AND transaction_type = 'RECEIVE'"

ISSUE_INDEX = "uq_wo_inventory_issue"
ISSUE_COLUMNS = ["company_id", "reference_type", "reference_id", "transaction_type", "part_id"]
ISSUE_WHERE = "reference_type = 'work_order' AND transaction_type = 'ISSUE'"

# (name, columns, predicate) for the two indexes this revision owns.
TARGET_INDEXES = (
    (RECEIPT_INDEX, RECEIPT_COLUMNS, RECEIPT_WHERE),
    (ISSUE_INDEX, ISSUE_COLUMNS, ISSUE_WHERE),
)


def _inspector():
    return sa.inspect(op.get_bind())


def _is_postgres(conn) -> bool:
    return conn.dialect.name == "postgresql"


def _is_sqlite(conn) -> bool:
    return conn.dialect.name == "sqlite"


def _has_table(table_name: str) -> bool:
    return _inspector().has_table(table_name)


def _reflect_index(table_name: str, index_name: str):
    """The reflected index dict, or None when it does not exist."""
    if not _has_table(table_name):
        return None
    for index in _inspector().get_indexes(table_name):
        if index["name"] == index_name:
            return index
    return None


def _index_predicate(index) -> str:
    """The reflected SQLite partial predicate, or '' for a full (predicate-less) index."""
    if index is None:
        return ""
    where = (index.get("dialect_options") or {}).get("sqlite_where")
    return "" if where is None else str(where).strip()


def _normalized(predicate: str) -> str:
    """Whitespace/case-insensitive compare so a reflected predicate matches ours."""
    return " ".join(predicate.split()).lower()


def _create_partial(index_name: str, columns, predicate: str) -> None:
    op.create_index(
        index_name,
        TABLE_NAME,
        columns,
        unique=True,
        sqlite_where=sa.text(predicate),
        postgresql_where=sa.text(predicate),
    )


def _create_full(index_name: str, columns) -> None:
    """The pre-076 SQLite shape: unique, no predicate."""
    op.create_index(index_name, TABLE_NAME, columns, unique=True)


def _duplicate_groups(conn, columns):
    """Groups that a FULL unique index over ``columns`` would reject.

    Used ONLY by the downgrade, whose restored shape is stricter than the correct
    partial one. Read-only: we report, we never delete a ledger row.
    """
    column_list = ", ".join(columns)
    rows = conn.execute(sa.text(f"""
            SELECT {column_list}, COUNT(*) AS n, GROUP_CONCAT(id) AS ids
            FROM {TABLE_NAME}
            GROUP BY {column_list}
            HAVING COUNT(*) > 1
            ORDER BY {column_list}
            """)).fetchall()  # nosec B608 - column_list is a module constant, never caller input
    # SQLite treats NULLs as DISTINCT in a unique index, so a group keyed on any NULL
    # can never actually collide -- GROUP BY folds NULLs together but the index does
    # not. Drop those or the downgrade would refuse a rollback it could complete.
    return [row for row in rows if all(value is not None for value in tuple(row)[: len(columns)])]


def _assert_downgrade_is_possible(conn) -> None:
    """Fail LOUDLY (never delete) if the restored full indexes would reject live rows."""
    offenders = []
    for index_name, columns, _predicate in TARGET_INDEXES:
        for row in _duplicate_groups(conn, columns):
            values = dict(zip(columns, tuple(row)[: len(columns)]))
            offenders.append(
                f"  {index_name}: "
                + ", ".join(f"{k}={v!r}" for k, v in values.items())
                + f" count={row.n} ids={row.ids}"
            )
    if not offenders:
        return

    message = "\n".join(
        [
            "Cannot restore the pre-076 (full, predicate-less) SQLite unique indexes: "
            "ledger rows exist that the over-broad shape would reject. These rows are "
            "legitimate under the correct partial indexes and under production "
            "Postgres. Inventory transactions are regulated records and are NOT "
            "auto-deleted -- resolve these groups deliberately, then re-run the "
            "downgrade:",
            *offenders,
        ]
    )
    print(f"[076_uq_wo_inventory_sqlite_parity] {message}")
    raise RuntimeError(message)


def upgrade() -> None:
    conn = op.get_bind()

    # Postgres already has the correct PARTIAL indexes (041 built them CONCURRENTLY).
    # Rebuilding a unique index on the hottest ledger table for no behavioral change
    # would take a real lock and briefly drop the double-receive guard. No-op.
    if _is_postgres(conn) or not _is_sqlite(conn):
        return

    if not _has_table(TABLE_NAME):
        return

    for index_name, columns, predicate in TARGET_INDEXES:
        existing = _reflect_index(TABLE_NAME, index_name)
        if existing is not None and _normalized(_index_predicate(existing)) == _normalized(predicate):
            # Already correct (the create_all bootstrap path, or a re-run). No-op.
            continue
        if existing is not None:
            # The degraded FULL unique index -- drop it and rebuild it partial. Going
            # full -> partial only RELAXES, so the rebuild can never fail on data.
            op.drop_index(index_name, table_name=TABLE_NAME)
        _create_partial(index_name, columns, predicate)


def downgrade() -> None:
    conn = op.get_bind()

    if _is_postgres(conn) or not _is_sqlite(conn):
        return

    if not _has_table(TABLE_NAME):
        return

    # The restored shape is STRICTER, so it can legitimately reject rows written while
    # 076 was applied. Report them; never delete them.
    _assert_downgrade_is_possible(conn)

    for index_name, columns, _predicate in TARGET_INDEXES:
        if _reflect_index(TABLE_NAME, index_name) is not None:
            op.drop_index(index_name, table_name=TABLE_NAME)
        _create_full(index_name, columns)
