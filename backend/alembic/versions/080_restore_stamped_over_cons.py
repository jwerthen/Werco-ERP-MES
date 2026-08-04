"""Restore the FK and CHECK constraints the create_all+stamp bootstrap skipped
(2026-07-31 prod pg_constraint audit): migration 003's lineage foreign keys and
value-range CHECK constraints that existed only in migration DDL, never in the
models -- the constraint half of the drift class 079 fixed for indexes.

Revision ID: 080_restore_stamped_over_con
Revises: 079_restore_stamped_over_idx
Create Date: 2026-07-31

The bootstrap-stamp drift story (same class as 079)
---------------------------------------------------
Prod was bootstrapped via ``Base.metadata.create_all()`` + ``alembic stamp``
(docs/DEVELOPMENT.md -> Database Migrations), so every constraint declared ONLY
inside stamped-past migration 003 -- and never mirrored on a model -- was
silently skipped. A read-only pre-flight against the live prod DB (2026-07-31)
confirmed:

  * ZERO of 003's 22 ``fk_*`` named foreign keys exist (8 of those columns DO
    carry an equivalent auto-named FK from model-level ``ForeignKey()``
    declarations that create_all emitted -- see the equivalence probe below).
  * ZERO of 003's ``chk_*`` CHECK constraints exist.
  * Every restored FK column has ZERO orphan rows and every restored CHECK
    predicate has ZERO violating rows, so everything below can be added fully
    enforced and VALIDATEd with no data remediation.

This migration restores the curated set below AND (the load-bearing half) every
restored constraint is mirrored on its owning model -- ``ForeignKey(...)`` on
the column / ``CheckConstraint(...)`` in ``__table_args__`` -- so ``create_all``
reproduces them and a future stamp can never skip them again. Migration
literals and model declarations are kept in exact lock-step (names, columns,
predicate text -- the 042/078/079 convention); a drift-guard test enforces it.

DELIBERATE EXCLUSIONS -- read before "completing" this set
----------------------------------------------------------
Four of 003's constraints are NOT restored. Three are load-bearing absences;
restoring any of them would break shipped behavior:

1. ``chk_inventory_items_quantity_non_negative``
   (``inventory_items.quantity_on_hand >= 0``) -- **EXCLUDED BY DESIGN, NOT
   DRIFT.** The material-consumption shortage posture (CLAUDE.md invariant 6,
   docs/MATERIAL_CONSUMPTION_PLAN.md) deliberately lets a short completion
   drive a lot NEGATIVE: record-and-alert (shortage event + notification),
   never a rolled-back write. This CHECK would make short completions fail at
   the DB. Its absence is the documented, chosen behavior
   (docs/DEVELOPMENT.md flags it load-bearing). DO NOT restore it in a future
   "complete the set" pass without a product decision reversing that posture.
   (``chk_inventory_items_allocated_non_negative`` on ``quantity_allocated``
   IS restored: verified 2026-07-31 that the consumption engine mutates only
   ``quantity_on_hand`` -- ``quantity_allocated`` is written exactly once per
   row, as 0 at item creation (receiving.py, completion_inventory_service.py),
   and no code path decrements it. ``quantity_available`` -- on_hand minus
   allocated -- CAN go negative, but no constraint targets it.)

2. ``chk_po_receipts_quantity_received_positive``
   (``po_receipts.quantity_received > 0``) -- **EXCLUDED: shipped code
   legitimately writes 0.** The receipt VOID flow (PR #149,
   receiving.py::void_receipt) reconciles a voided receipt down to
   ``quantity_received = 0`` on the soft-deleted row (the correction path is
   schema-bounded gt=0; 0 is reserved for void). Prod's zero-violation probe
   only means nothing has been voided yet. Restoring 003's predicate would
   make every receipt void fail with a DB IntegrityError. If a future pass
   wants DB coverage here, re-derive it void-aware (e.g.
   ``quantity_received > 0 OR is_deleted``) as its own reviewed migration.
   (The sibling ``quantity_accepted >= 0`` / ``quantity_rejected >= 0``
   CHECKs are unaffected -- void writes 0 to quantity_accepted, which
   passes -- and ARE restored.)

3. ``chk_bom_items_quantity_positive`` (``bom_items.quantity > 0``) --
   **EXCLUDED: non-positive BOM quantities are a designed, representable
   state.** The backflush readiness/refusal layer
   (completion_inventory_service.py) carries dedicated BLOCKING diagnostic
   codes ``zero_bom_quantity`` and ``negative_bom_quantity``: a BOM line with
   quantity <= 0 is meant to EXIST, be surfaced by
   ``GET /parts/{id}/backflush-readiness``, refuse the backflush opt-in, and
   be corrected by a human -- the same surface-don't-make-unrepresentable
   posture as the BOM-line UoM mismatches. The bulk BOM import schema
   (schemas/bom_import.py) puts no bound on quantity, and the pinned tests
   (tests/api/test_backflush_exposure.py) seed quantity 0 / negative rows
   directly. Restoring this CHECK would make the import path fail at the DB
   on rows the diagnostics exist to catch, and turn the diagnostic machinery
   into dead code. The API create/update path already bounds quantity gt=0
   (schemas/bom.py), which is where the real gate lives.
   (``chk_bom_items_scrap_factor_range`` IS restored: no code path writes
   scrap_factor outside [0, 1] -- the API bounds ge=0, the importer never
   sets it -- and the resolver's ``scrap_factor <= -1`` branch is defensive,
   not a designed representable state.)

4. ``chk_work_centers_efficiency_range``
   (``work_centers.efficiency`` between 0 and 200) -- **OBSOLETE: the column
   never existed.** Git history shows ``work_centers`` has carried
   ``efficiency_factor`` (1.0-scale, 1.0 = 100%) since the initial commit;
   a bare ``efficiency`` column appears nowhere. 003's own
   ``required_columns=['efficiency']`` guard means this CHECK was skipped
   even on DBs that really ran 003 -- it has never existed anywhere, so this
   is not drift. Not re-derived here either: the natural successor
   (``efficiency_factor >= 0 AND efficiency_factor <= 2``) targets a column
   the 2026-07-31 pre-flight could not probe (the probe failed on the missing
   ``efficiency`` name), and the Pydantic schema puts no bounds on
   ``efficiency_factor``, so out-of-range prod data cannot be ruled out from
   here. Re-derive only after a prod probe of ``efficiency_factor`` bounds.

Restored set
------------
22 foreign keys (all ``ON DELETE SET NULL``, no ON UPDATE action -- exactly
003's ``safe_create_fk`` semantics; every column is nullable) and 19 CHECK
constraints (23 minus the four exclusions above). See the FOREIGN_KEYS and
CHECKS constants.

Online-safe pattern: ADD NOT VALID, then VALIDATE, never in one transaction
---------------------------------------------------------------------------
Each constraint is restored in two phases:

  Phase 1: ``ALTER TABLE ... ADD CONSTRAINT ... NOT VALID`` -- takes a brief
           ACCESS EXCLUSIVE lock but does NOT scan the table, so it completes
           in milliseconds regardless of table size. New/updated rows are
           enforced from this moment.
  Phase 2: ``ALTER TABLE ... VALIDATE CONSTRAINT ...`` -- scans existing rows
           under SHARE UPDATE EXCLUSIVE only, so concurrent writers proceed
           during the scan.

The entire run executes inside ``op.get_context().autocommit_block()``: unlike
079's CONCURRENTLY builds, plain ALTERs don't *require* autocommit, but it is
the lock-separation guarantee -- under Alembic's default transactional DDL,
ADD and VALIDATE would otherwise share one transaction, and the ACCESS
EXCLUSIVE lock taken by ADD would then be HELD THROUGH the VALIDATE scan
(locks release at commit, not statement end), blocking all writers for the
full scan after all. Autocommit makes each statement its own transaction: the
strong lock drops the instant the ADD commits, and each VALIDATE holds only
the weak lock. All Phase-1 ADDs run first (the cheap locking phase, done in
one quick burst), then all Phase-2 VALIDATEs (the scanning phase).

Zero rows touched: this migration executes only ALTER TABLE ADD/VALIDATE/DROP
CONSTRAINT plus read-only ``pg_constraint`` probes. It reads and writes no
table rows anywhere -- in particular nothing on ``audit_logs`` (no audit_logs
constraint is in the set; the 008/060 triggers and the hash chain are
untouched, as is every other table's data).

Idempotency / self-heal
-----------------------
Both directions probe ``pg_constraint`` (conname + conrelid). A constraint
that already exists AND is validated is skipped; one that exists with
``convalidated = false`` (an interrupted prior run: Phase 1 committed, Phase 2
never ran -- NOT VALID persists, it does not expire) gets VALIDATE re-run
rather than being skipped, so a re-run converges to fully-validated. For the
FKs there is additionally an EQUIVALENCE probe: 8 of the 22 columns carry a
model-level ``ForeignKey()`` that a create_all bootstrap emits under an
auto-generated name (e.g. ``cycle_counts_assigned_to_fkey``) -- where any FK
already exists on the same column referencing the same table, under ANY name,
we skip (never double-constrain a column) and VALIDATE it if unvalidated.

Downgrade semantics
-------------------
Drops exactly the 003-named constraints (reversed order, IF EXISTS-guarded,
constraints only -- never data, never the auto-named model FKs create_all
owns). On a stamped-bootstrap DB (prod) this returns the audited pre-080
state. On a fully-migrated DB (e.g. dev) it also drops 003's originals --
re-upgrading restores them identically. Round-trip safe in both environments.

Non-Postgres (the SQLite local create_all / pytest path) early-returns in both
directions: SQLite cannot ALTER TABLE ADD CONSTRAINT at all, and on that path
``create_all`` already emits every constraint from the model mirrors this
migration is locked to (SQLite enforces the CHECKs; FKs stay unenforced there
per its default PRAGMA, which the test suite relies on).

Revision id is 28 chars (<= 32) per the create_all -> stamp -> upgrade
bootstrap constraint (``alembic_version.version_num`` is varchar(32)).
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "080_restore_stamped_over_con"
down_revision = "079_restore_stamped_over_idx"
branch_labels = None
depends_on = None

# (constraint_name, table, column, referenced_table) -- all single-column FKs
# referencing <referenced_table>.id with ON DELETE SET NULL (003's
# safe_create_fk default; no call site overrode it, and 003 set no ON UPDATE
# action). Names are 003's, verbatim. Kept in lock-step with the model
# ForeignKey declarations (the 14 previously-plain columns now carry
# name= + ondelete="SET NULL"; the 8 columns that already had auto-named model
# FKs are covered by the equivalence probe and deliberately left as-is).
FOREIGN_KEYS = [
    ("fk_users_created_by", "users", "created_by", "users"),
    ("fk_parts_created_by", "parts", "created_by", "users"),
    ("fk_parts_primary_supplier", "parts", "primary_supplier_id", "vendors"),
    ("fk_work_orders_created_by", "work_orders", "created_by", "users"),
    ("fk_work_orders_released_by", "work_orders", "released_by", "users"),
    ("fk_work_orders_current_operation", "work_orders", "current_operation_id", "work_order_operations"),
    ("fk_work_order_operations_started_by", "work_order_operations", "started_by", "users"),
    ("fk_work_order_operations_completed_by", "work_order_operations", "completed_by", "users"),
    ("fk_time_entries_approved_by", "time_entries", "approved_by", "users"),
    ("fk_boms_created_by", "boms", "created_by", "users"),
    ("fk_boms_approved_by", "boms", "approved_by", "users"),
    ("fk_inventory_items_supplier", "inventory_items", "supplier_id", "vendors"),
    ("fk_routing_operations_vendor", "routing_operations", "vendor_id", "vendors"),
    ("fk_routings_created_by", "routings", "created_by", "users"),
    ("fk_routings_approved_by", "routings", "approved_by", "users"),
    ("fk_mrp_runs_created_by", "mrp_runs", "created_by", "users"),
    ("fk_mrp_actions_processed_by", "mrp_actions", "processed_by", "users"),
    ("fk_cycle_counts_assigned_to", "cycle_counts", "assigned_to", "users"),
    ("fk_cycle_counts_completed_by", "cycle_counts", "completed_by", "users"),
    ("fk_cycle_counts_created_by", "cycle_counts", "created_by", "users"),
    ("fk_cycle_count_items_counted_by", "cycle_count_items", "counted_by", "users"),
    ("fk_documents_released_by", "documents", "released_by", "users"),
]

# (constraint_name, table, predicate) -- names and predicate text are 003's,
# verbatim, and are kept byte-identical to the model CheckConstraint mirrors.
# chk_inventory_items_quantity_non_negative, chk_po_receipts_quantity_received_
# positive, chk_bom_items_quantity_positive, and chk_work_centers_efficiency_
# range are DELIBERATELY absent -- see the header's DELIBERATE EXCLUSIONS
# before adding any of them.
CHECKS = [
    ("chk_work_orders_quantity_ordered_positive", "work_orders", "quantity_ordered > 0"),
    ("chk_work_orders_quantity_complete_non_negative", "work_orders", "quantity_complete >= 0"),
    ("chk_work_orders_quantity_scrapped_non_negative", "work_orders", "quantity_scrapped >= 0"),
    ("chk_work_orders_priority_range", "work_orders", "priority >= 1 AND priority <= 10"),
    ("chk_po_lines_quantity_ordered_positive", "purchase_order_lines", "quantity_ordered > 0"),
    ("chk_po_lines_quantity_received_non_negative", "purchase_order_lines", "quantity_received >= 0"),
    ("chk_po_lines_unit_price_non_negative", "purchase_order_lines", "unit_price >= 0"),
    ("chk_po_receipts_quantity_accepted_non_negative", "po_receipts", "quantity_accepted >= 0"),
    ("chk_po_receipts_quantity_rejected_non_negative", "po_receipts", "quantity_rejected >= 0"),
    ("chk_inventory_items_allocated_non_negative", "inventory_items", "quantity_allocated >= 0"),
    ("chk_bom_items_scrap_factor_range", "bom_items", "scrap_factor >= 0 AND scrap_factor <= 1"),
    ("chk_quote_lines_quantity_positive", "quote_lines", "quantity > 0"),
    ("chk_quote_lines_unit_price_non_negative", "quote_lines", "unit_price >= 0"),
    ("chk_work_order_ops_setup_time_non_negative", "work_order_operations", "setup_time_hours >= 0"),
    ("chk_work_order_ops_run_time_non_negative", "work_order_operations", "run_time_hours >= 0"),
    ("chk_routing_ops_setup_hours_non_negative", "routing_operations", "setup_hours >= 0"),
    ("chk_routing_ops_run_hours_non_negative", "routing_operations", "run_hours_per_unit >= 0"),
    ("chk_parts_standard_cost_non_negative", "parts", "standard_cost >= 0"),
    ("chk_work_centers_hourly_rate_non_negative", "work_centers", "hourly_rate >= 0"),
]


def _is_postgres(conn) -> bool:
    return conn.dialect.name == "postgresql"


def _table_exists(conn, table: str) -> bool:
    return (
        conn.execute(
            sa.text("SELECT to_regclass('public.' || :t) IS NOT NULL"), {"t": table}
        ).scalar()
        is True
    )


def _constraint_state(conn, table: str, name: str) -> str:
    """Return 'absent' | 'valid' | 'not_validated' for a named constraint on a table.

    Validity-aware on purpose: an interrupted prior run leaves the Phase-1
    ``ADD ... NOT VALID`` committed with ``convalidated = false``. A plain
    existence probe would skip it forever, permanently masking a constraint
    that never scans its existing rows; by reading ``convalidated`` we can
    re-run VALIDATE instead (the FK/CHECK twin of 079's ``indisvalid`` probe).
    """
    row = conn.execute(
        sa.text(
            "SELECT c.convalidated FROM pg_constraint c "
            "WHERE c.conname = :name AND c.conrelid = to_regclass('public.' || :t)"
        ),
        {"name": name, "t": table},
    ).fetchone()
    if row is None:
        return "absent"
    return "valid" if row[0] else "not_validated"


def _equivalent_fk(conn, table: str, column: str, ref_table: str):
    """Find an existing FK on (table.column) -> ref_table under ANY name.

    8 of the 22 target columns already carry a model-level ``ForeignKey()``
    that a create_all bootstrap emits under an auto-generated name (e.g.
    ``cycle_counts_assigned_to_fkey``). Adding 003's named twin next to it
    would double-constrain the column (Postgres allows duplicate FKs -- both
    enforced, both maintained on every write). This probe treats any
    single-column FK on the same column referencing the same table as "already
    restored" regardless of name. Returns (conname, convalidated) or None.
    """
    row = conn.execute(
        sa.text(
            "SELECT c.conname, c.convalidated "
            "FROM pg_constraint c "
            "JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = c.conkey[1] "
            "WHERE c.contype = 'f' "
            "  AND c.conrelid = to_regclass('public.' || :t) "
            "  AND c.confrelid = to_regclass('public.' || :ref) "
            "  AND array_length(c.conkey, 1) = 1 "
            "  AND a.attname = :col"
        ),
        {"t": table, "ref": ref_table, "col": column},
    ).fetchone()
    return None if row is None else (row[0], bool(row[1]))


def _validate(table: str, name: str) -> None:
    op.execute(sa.text(f'ALTER TABLE {table} VALIDATE CONSTRAINT "{name}"'))


def upgrade() -> None:
    conn = op.get_bind()

    if not _is_postgres(conn):
        # SQLite (local dev / pytest create_all path) cannot ALTER TABLE ADD
        # CONSTRAINT; create_all already emits every constraint from the model
        # mirrors this migration is locked to.
        return

    # names to VALIDATE in Phase 2: (table, actual_constraint_name)
    to_validate: list[tuple[str, str]] = []

    with op.get_context().autocommit_block():
        # ------------------------------------------------------------------
        # Phase 1: ADD ... NOT VALID -- brief ACCESS EXCLUSIVE, no scan, each
        # statement autocommitted so the strong lock drops immediately (the
        # lock-separation argument in the header).
        # ------------------------------------------------------------------
        for name, table, column, ref_table in FOREIGN_KEYS:
            if not _table_exists(conn, table) or not _table_exists(conn, ref_table):
                print(f"Skipping FK {name}: table missing")
                continue
            state = _constraint_state(conn, table, name)
            if state == "valid":
                continue
            if state == "not_validated":
                to_validate.append((table, name))
                continue
            equivalent = _equivalent_fk(conn, table, column, ref_table)
            if equivalent is not None:
                eq_name, eq_validated = equivalent
                print(f"Skipping FK {name}: equivalent FK {eq_name} already on {table}.{column}")
                if not eq_validated:
                    to_validate.append((table, eq_name))
                continue
            op.execute(
                sa.text(
                    f'ALTER TABLE {table} ADD CONSTRAINT "{name}" '
                    f"FOREIGN KEY ({column}) REFERENCES {ref_table} (id) "
                    f"ON DELETE SET NULL NOT VALID"
                )
            )
            to_validate.append((table, name))

        for name, table, predicate in CHECKS:
            if not _table_exists(conn, table):
                print(f"Skipping CHECK {name}: table missing")
                continue
            state = _constraint_state(conn, table, name)
            if state == "valid":
                continue
            if state == "not_validated":
                to_validate.append((table, name))
                continue
            op.execute(
                sa.text(f'ALTER TABLE {table} ADD CONSTRAINT "{name}" CHECK ({predicate}) NOT VALID')
            )
            to_validate.append((table, name))

        # ------------------------------------------------------------------
        # Phase 2: VALIDATE -- SHARE UPDATE EXCLUSIVE scans, writers proceed;
        # each in its own autocommitted transaction.
        # ------------------------------------------------------------------
        for table, name in to_validate:
            _validate(table, name)


def downgrade() -> None:
    conn = op.get_bind()

    if not _is_postgres(conn):
        return

    # Constraints only -- never data. Drops exactly the 003 names (an
    # equivalence-skipped auto-named model FK is create_all's, not ours, and is
    # left alone). Reversed order, IF EXISTS-guarded; DROP CONSTRAINT takes a
    # brief ACCESS EXCLUSIVE with no scan, autocommitted per statement.
    with op.get_context().autocommit_block():
        for name, table, _predicate in reversed(CHECKS):
            if _table_exists(conn, table):
                op.execute(sa.text(f'ALTER TABLE {table} DROP CONSTRAINT IF EXISTS "{name}"'))
        for name, table, _column, _ref_table in reversed(FOREIGN_KEYS):
            if _table_exists(conn, table):
                op.execute(sa.text(f'ALTER TABLE {table} DROP CONSTRAINT IF EXISTS "{name}"'))
