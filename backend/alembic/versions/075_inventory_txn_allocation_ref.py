"""inventory_transactions: allocation_id genealogy FK + (company, reference) composite index

Revision ID: 075_inventory_txn_allocation_ref
Revises: 074_wo_material_allocations
Create Date: 2026-07-25

Context
-------
PR 1 (schema only) of the material-consumption feature, ledger half. Deliberately
split out of ``074_wo_material_allocations`` so this change to the HOTTEST table
in the system -- ``inventory_transactions``, the append-only stock ledger -- can be
reviewed, deployed, and rolled back independently of 074's pure CREATE TABLE. 074
touches nothing that exists; this one does.

Two additive changes, lock-step with
``app/models/inventory.py::InventoryTransaction``:

1. ``allocation_id`` -- nullable Integer FK -> ``work_order_material_allocations.id``,
   indexed (``ix_inventory_transactions_allocation_id``). The DURABLE GENEALOGY
   KEY: which tie caused this movement. It survives a re-tie, because an untied
   allocation is CANCELLED rather than deleted (status is the tombstone -- see the
   model docstring), so an auditor can always walk a ledger row back to the exact
   allocation that produced it even after the work order has been re-tied to a
   different lot or a nest has been re-imported. ``reference_id`` alone cannot do
   this: it points at the work order / operation, which is reused across ties.
2. ``ix_inventory_txn_company_reference`` -- a NON-UNIQUE composite index on
   ``(company_id, reference_type, reference_id)``.

Why the composite index (and why NON-unique)
--------------------------------------------
The two existing partial UNIQUE indexes on this table --
``uq_wo_inventory_receipt`` and ``uq_wo_inventory_issue`` (migration 041) -- both
carry the predicate ``reference_type = 'work_order' AND ...``. They are the
DB-level idempotency guards behind ``completion_inventory_service.py``: they are
what makes a concurrent double-complete unable to double-receive finished goods or
double-issue a backflushed component.

**They are NOT touched by this revision, in any way.** Their columns, predicates,
uniqueness, and names are left exactly as 041 built them. The new
material-consumption path sits deliberately OUTSIDE those predicates by writing
``reference_type = 'work_order_operation'`` instead of ``'work_order'``, so it can
never collide with the backflush guards -- and, symmetrically, its own repeated
consumption rows (many runs, many rows, legitimately) are never squeezed into a
uniqueness rule designed for one-shot receipts. That is a design decision, not an
oversight: consumption is intentionally many-per-reference, so the new index is
NON-unique. It exists purely to make the genealogy / history reads ("every ledger
row for this operation", "every movement under this reference") index-backed;
today those reads would fall back to a sequential scan of the ledger because no
existing index covers ``reference_type = 'work_order_operation'``.

KNOWN DIALECT DIVERGENCE the service PR must plan for
-----------------------------------------------------
The 041 indexes declare ONLY ``postgresql_where``. On Postgres they are therefore
PARTIAL and the consumption path (``reference_type = 'work_order_operation'``)
falls outside them, so many consumption rows may share one operation reference --
which is the whole point. On SQLite -- dev and pytest -- those same declarations
degrade to FULL unique indexes covering EVERY reference_type, so a second ISSUE
row with the same ``(company_id, reference_type, reference_id, transaction_type,
part_id)`` is REJECTED locally even though production Postgres accepts it.

This revision deliberately does NOT "fix" that by adding ``sqlite_where`` to the
041 indexes. They are the load-bearing backflush idempotency guards; loosening
them on SQLite would change the behavior existing backflush tests run against, and
that belongs in its own reviewed change rather than smuggled into a migration for a
different feature. (Contrast the NEW indexes in 074, which declare BOTH predicates
precisely so they do not have this problem.)

    SUPERSEDED: that reviewed change is ``076_uq_wo_inv_sqlite_parity``
    (file ``076_uq_wo_inventory_sqlite_parity.py``), which adds ``sqlite_where`` to
    both 041 index declarations and rebuilds them partial on existing SQLite DBs
    (no-op on Postgres). As of 076 the divergence described above no longer exists
    and the consumption path is outside the predicates on BOTH dialects. The test
    that pinned it was updated in place to assert the corrected behavior and renamed
    to ``tests/test_migration_075_inventory_txn_allocation_ref.py::
    test_consumption_reference_is_outside_the_041_predicates_on_both_dialects``.
    Nothing executable in THIS revision changed -- 075 never touched those indexes
    and still does not; only this note was added.

NO backfill -- correct-forward only
-----------------------------------
``allocation_id`` is nullable and purely additive. Every pre-existing ledger row
predates the feature and genuinely has no allocation, and so does every movement
that is not allocation-driven going forward: PO receipts, manual adjustments,
finished-goods receipts, and the existing BOM backflush issues. NULL is the
truthful value for all of them. Inventing one would fabricate material genealogy
in an AS9100D traceability record -- the same posture 073 took with delivery
provenance, and the opposite of 072's ``notified_at``, where the backfill was
load-bearing against a go-live storm. Nothing reads ``allocation_id`` yet (PR 1 is
schema + model only), so there is nothing to storm and nothing to stamp. No
historical work order gains an allocation.

The tamper-evident ``audit_log`` table is NOT touched and NOT backfilled.

No new table, so no RLS statement
---------------------------------
``inventory_transactions`` is pre-existing, so the "``ENABLE ROW LEVEL SECURITY``
on every new table" convention (docs/SUPABASE_SECURITY.md) does not apply: 059
already swept every public table lacking RLS, and ALTERing a table does not change
its RLS state, so the Security Advisor's ``rls_disabled_in_public`` check cannot
re-flag it. (074 needed an explicit statement because it CREATEs a table.)

Idempotent and reversible
-------------------------
Bootstrap is ``create_all() -> stamp -> upgrade`` (docs/DEVELOPMENT.md), not a bare
``upgrade head`` on an empty DB. The ``add_column`` is guarded by
``_has_table``/``_has_column`` and both ``create_index`` calls by ``_has_index``
(precedent 058/061/071/072), so a create_all-bootstrapped DB -- where the model
already built the column and both indexes -- and any re-run are clean no-ops.

``downgrade`` really drops both indexes and the column (guarded, reverse of the
add order); it is not a ``pass`` stub. It never touches ``uq_wo_inventory_receipt``
/ ``uq_wo_inventory_issue``.

Dialect note (SQLite): adding an FK COLUMN requires ``op.batch_alter_table`` --
alembic cannot ALTER-add an FK constraint on SQLite (precedent 058/072:292-296).
Postgres keeps a plain inline-FK ALTER. The drop takes the batch path on SQLite
too, because SQLite 3.35+ refuses to plain-drop a column that participates in an
FK or an index. The batch path recreates ``inventory_transactions``, which is safe
here: no other table has an inbound FK to it (verified), and batch mode
reconstructs the two 041 partial unique indexes along with everything else --
which the migration test asserts explicitly, by name and predicate, on both sides
of the round-trip.

Locking / operations note
-------------------------
This is the one place in PR 1 that touches a large table, so the ordering matters:

* The ``ADD COLUMN`` is nullable with NO default -> metadata-only on PostgreSQL
  11+ (catalog change, brief ACCESS EXCLUSIVE lock to take the DDL, NO table
  rewrite and no row scan), so it is effectively instant no matter how large the
  ledger has grown. Adding the FK does take a brief ShareRowExclusive lock on
  ``work_order_material_allocations``, which 074 just created empty -- no scan.
* The TWO index builds are the real cost. A non-CONCURRENT ``CREATE INDEX`` holds
  a SHARE lock on ``inventory_transactions`` for the duration of the build, which
  BLOCKS WRITES to the ledger -- and the ledger is written by receiving, issuing,
  adjusting, and every work-order completion. On Werco's single-plant ledger
  (tens-of-thousands to low hundreds-of-thousands of rows) that is a seconds-long
  window, which is acceptable inside a normal deploy. **If this ledger is ever
  materially larger, or this must run hot, build both indexes out-of-band FIRST
  with ``CREATE INDEX CONCURRENTLY`` (using these exact names) and let the guarded
  ``create_index`` calls find them and no-op.** That escape hatch is the reason
  every index here is guarded by name. CONCURRENTLY cannot be used inside the
  migration itself because alembic runs it in a transaction.

Deploy ordering: run AFTER 074 (the FK target must exist) and BEFORE any app
deploy that writes ``allocation_id``. Since PR 1 ships no code that writes it,
this revision can run arbitrarily early; old code neither writes nor selects the
column. There is no window where the reverse ordering is safe.

create_all parity
-----------------
This revision and ``Base.metadata.create_all()`` converge: same column name, type
(Integer), nullability (nullable), absent server default, same FK target, and the
same two index names/columns/uniqueness. Only ordinal position differs (create_all
emits the column where it is declared, after ``reference_number``; ``ADD COLUMN``
appends it), which autogenerate does not compare.

Revision id ``075_inventory_txn_allocation_ref`` is 31 chars (<= 32) per the
create_all -> stamp -> upgrade bootstrap constraint (``alembic_version.version_num``
is varchar(32) on a freshly bootstrapped DB); see docs/DEVELOPMENT.md.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "075_inventory_txn_allocation_ref"
down_revision = "074_wo_material_allocations"
branch_labels = None
depends_on = None

TXN_TABLE = "inventory_transactions"
ALLOCATIONS_TABLE = "work_order_material_allocations"

ALLOCATION_COLUMN = "allocation_id"
ALLOCATION_INDEX = "ix_inventory_transactions_allocation_id"
ALLOCATION_FK = "fk_inventory_transactions_allocation_id"

# Non-unique composite backing the genealogy/history reads for reference types the
# 041 partial unique indexes do not cover (notably 'work_order_operation').
REFERENCE_INDEX = "ix_inventory_txn_company_reference"
REFERENCE_COLUMNS = ["company_id", "reference_type", "reference_id"]

# The 041 idempotency indexes. Listed ONLY so it is unmistakable in the source that
# this revision leaves them alone -- they are never created, dropped, or altered
# here. Their predicates are load-bearing for backflush idempotency.
DO_NOT_TOUCH_INDEXES = ("uq_wo_inventory_receipt", "uq_wo_inventory_issue")


def _inspector():
    return sa.inspect(op.get_bind())


# No ``_is_postgres`` helper here: unlike 074 (which emits Postgres-only RLS and
# CREATE TYPE), every statement in this revision is dialect-neutral DDL. The only
# branch is SQLite-vs-rest for the FK column, taken inline. A postgres guard would
# be dead code -- same reasoning 073 used for omitting ``_has_index``.
def _has_table(table_name: str) -> bool:
    return _inspector().has_table(table_name)


def _has_column(table_name: str, column_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(col["name"] == column_name for col in _inspector().get_columns(table_name))


def _has_index(table_name: str, index_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(ix["name"] == index_name for ix in _inspector().get_indexes(table_name))


def upgrade() -> None:
    if not _has_table(TXN_TABLE):
        return

    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"

    # 1) allocation_id: nullable FK -> work_order_material_allocations.id.
    #    Nullable + no default => metadata-only on PG 11+. NO backfill: NULL is the
    #    truthful value for every pre-existing and every non-allocation-driven row.
    #    SQLite cannot ALTER-add an FK constraint, so it takes the batch path
    #    (precedent 058/072); Postgres keeps a plain inline-FK ALTER.
    if not _has_column(TXN_TABLE, ALLOCATION_COLUMN):
        if is_sqlite:
            with op.batch_alter_table(TXN_TABLE) as batch_op:
                batch_op.add_column(sa.Column(ALLOCATION_COLUMN, sa.Integer(), nullable=True))
                batch_op.create_foreign_key(ALLOCATION_FK, ALLOCATIONS_TABLE, [ALLOCATION_COLUMN], ["id"])
        else:
            op.add_column(
                TXN_TABLE,
                sa.Column(
                    ALLOCATION_COLUMN,
                    sa.Integer(),
                    sa.ForeignKey(f"{ALLOCATIONS_TABLE}.id"),
                    nullable=True,
                ),
            )

    # 2) The two index builds -- the only statements here with a real lock cost on a
    #    large ledger. Guarded BY NAME so an out-of-band CREATE INDEX CONCURRENTLY
    #    (same names) makes these a no-op; see the locking note in the docstring.
    if not _has_index(TXN_TABLE, ALLOCATION_INDEX):
        op.create_index(ALLOCATION_INDEX, TXN_TABLE, [ALLOCATION_COLUMN], unique=False)
    if not _has_index(TXN_TABLE, REFERENCE_INDEX):
        op.create_index(REFERENCE_INDEX, TXN_TABLE, REFERENCE_COLUMNS, unique=False)

    # uq_wo_inventory_receipt / uq_wo_inventory_issue (DO_NOT_TOUCH_INDEXES) are
    # NEVER created, dropped, or altered here -- see the module docstring.


def downgrade() -> None:
    if not _has_table(TXN_TABLE):
        return

    is_sqlite = op.get_bind().dialect.name == "sqlite"

    # Reverse of the add order. The 041 partial unique indexes are left untouched
    # (batch mode on SQLite recreates them verbatim; the migration test asserts it).
    if _has_index(TXN_TABLE, REFERENCE_INDEX):
        op.drop_index(REFERENCE_INDEX, table_name=TXN_TABLE)
    if _has_index(TXN_TABLE, ALLOCATION_INDEX):
        op.drop_index(ALLOCATION_INDEX, table_name=TXN_TABLE)

    # SQLite 3.35+ refuses to plain-drop a column that participates in an FK, so the
    # drop takes the batch (table-recreate) path there. Safe: nothing has an inbound
    # FK to inventory_transactions.
    if _has_column(TXN_TABLE, ALLOCATION_COLUMN):
        if is_sqlite:
            with op.batch_alter_table(TXN_TABLE) as batch_op:
                batch_op.drop_column(ALLOCATION_COLUMN)
        else:
            op.drop_column(TXN_TABLE, ALLOCATION_COLUMN)
