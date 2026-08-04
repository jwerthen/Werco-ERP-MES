"""Coverage for 080_restore_stamped_over_con (file 080_restore_stamped_over_cons.py).

080 is the CONSTRAINT half of the drift class 079 fixed for indexes. Prod was
bootstrapped via ``Base.metadata.create_all()`` + ``alembic stamp``, so migration 003's
22 named ``fk_*`` lineage foreign keys and its ``chk_*`` value-range CHECKs -- declared
only in migration DDL, never on a model -- were silently skipped. 080 restores 22 FKs
and 19 CHECKs and, the load-bearing half, mirrors every one onto the owning model
(``ForeignKey(..., name=...)`` on the column, ``CheckConstraint(...)`` in
``__table_args__``) so ``create_all`` reproduces them and a future stamp can never skip
them again -- the 042/078/079 lock-step convention.

What is load-bearing here:

1. **The drift guard.** The migration's frozen ``FOREIGN_KEYS`` / ``CHECKS`` literals,
   this test's frozen copies, and what the models actually declare on
   ``Base.metadata`` must all agree -- constraint name, table, column, referenced
   table, ``ON DELETE SET NULL``, and for CHECKs the exact predicate text. Fourteen of
   the 22 FKs are mirrored under 003's own name; the other eight columns already
   carried an unnamed model-level ``ForeignKey()`` that ``create_all`` emits
   auto-named, and the migration's equivalence probe deliberately skips those rather
   than double-constraining the column. Which FK is in which bucket is frozen below.

2. **The four exclusions are deliberate, not oversights.** 003 declared four more
   constraints that 080 must NEVER restore -- each would break shipped behavior. They
   are asserted absent from the migration literals AND from the models, with the
   breakage spelled out at each assertion. This is the test that stops a future
   "complete the set" pass.

3. **Postgres-only, online-safe, and data-free.** Both directions early-return on a
   non-Postgres dialect (functionally verified, not just grepped); every ``op.execute``
   is an ``ALTER TABLE``, every ``conn.execute`` a read-only ``SELECT``; downgrade
   drops exactly the names in the two frozen lists and nothing else.

4. **The FK cycle and the second join path.** ``work_orders.current_operation_id`` ->
   ``work_order_operations.id`` closes a cycle with ``work_order_operations.work_order_id``,
   so it must render as a post-CREATE ``ALTER TABLE`` under ``use_alter=True`` -- and it
   gives those two tables a SECOND foreign-key path, which breaks any onclause-less
   ``join()`` between them (``AmbiguousForeignKeysError``). The pinned relationships and
   the shared dispatch/kiosk queue seam are both asserted to still resolve.
"""

import ast
import importlib.util
import io
import os
import subprocess
import sys
import tokenize

import pytest
import sqlalchemy as sa

from alembic.config import Config
from alembic.script import ScriptDirectory

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSIONS_DIR = os.path.join(BACKEND_DIR, "alembic", "versions")

REVISION = "080_restore_stamped_over_con"
MIGRATION_FILE = "080_restore_stamped_over_cons.py"
DOWN_REVISION = "079_restore_stamped_over_idx"

# ---------------------------------------------------------------------------
# Frozen copies of the migration's literals.
# ---------------------------------------------------------------------------
# (constraint_name, table, column, referenced_table, mirror) where `mirror` records HOW
# the model declares it:
#   "named"      -- the model column carries ForeignKey(..., name=<003's name>,
#                   ondelete="SET NULL"), so create_all emits 003's exact constraint.
#   "equivalent" -- the column already carried a plain, UNNAMED model ForeignKey before
#                   080. create_all emits it auto-named (e.g. cycle_counts_assigned_to_fkey)
#                   and the migration's _equivalent_fk probe skips 003's named twin rather
#                   than double-constraining the column (Postgres would keep and maintain
#                   BOTH). These columns are deliberately left as-is; the bucket is frozen
#                   so a future edit that renames one has to come through this test.
EXPECTED_FOREIGN_KEYS = [
    ("fk_users_created_by", "users", "created_by", "users", "named"),
    ("fk_parts_created_by", "parts", "created_by", "users", "named"),
    ("fk_parts_primary_supplier", "parts", "primary_supplier_id", "vendors", "named"),
    ("fk_work_orders_created_by", "work_orders", "created_by", "users", "named"),
    ("fk_work_orders_released_by", "work_orders", "released_by", "users", "named"),
    (
        "fk_work_orders_current_operation",
        "work_orders",
        "current_operation_id",
        "work_order_operations",
        "named",
    ),
    ("fk_work_order_operations_started_by", "work_order_operations", "started_by", "users", "named"),
    ("fk_work_order_operations_completed_by", "work_order_operations", "completed_by", "users", "named"),
    ("fk_time_entries_approved_by", "time_entries", "approved_by", "users", "named"),
    ("fk_boms_created_by", "boms", "created_by", "users", "named"),
    ("fk_boms_approved_by", "boms", "approved_by", "users", "named"),
    ("fk_inventory_items_supplier", "inventory_items", "supplier_id", "vendors", "named"),
    ("fk_routing_operations_vendor", "routing_operations", "vendor_id", "vendors", "named"),
    ("fk_routings_created_by", "routings", "created_by", "users", "equivalent"),
    ("fk_routings_approved_by", "routings", "approved_by", "users", "equivalent"),
    ("fk_mrp_runs_created_by", "mrp_runs", "created_by", "users", "equivalent"),
    ("fk_mrp_actions_processed_by", "mrp_actions", "processed_by", "users", "named"),
    ("fk_cycle_counts_assigned_to", "cycle_counts", "assigned_to", "users", "equivalent"),
    ("fk_cycle_counts_completed_by", "cycle_counts", "completed_by", "users", "equivalent"),
    ("fk_cycle_counts_created_by", "cycle_counts", "created_by", "users", "equivalent"),
    ("fk_cycle_count_items_counted_by", "cycle_count_items", "counted_by", "users", "equivalent"),
    ("fk_documents_released_by", "documents", "released_by", "users", "equivalent"),
]

# (constraint_name, table, predicate) -- predicate text is 003's, verbatim, and must stay
# byte-identical (modulo whitespace) between the migration literal and the model mirror.
EXPECTED_CHECKS = [
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

# The four 003 constraints 080 must NEVER restore. Each entry carries the table it would
# land on so the model-side assertion can look in the right place; the "what breaks"
# reasoning lives at the assertions in test_the_four_exclusions_stay_excluded.
DELIBERATE_EXCLUSIONS = (
    ("chk_inventory_items_quantity_non_negative", "inventory_items"),
    ("chk_po_receipts_quantity_received_positive", "po_receipts"),
    ("chk_bom_items_quantity_positive", "bom_items"),
    ("chk_work_centers_efficiency_range", "work_centers"),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _script_directory() -> ScriptDirectory:
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))
    return ScriptDirectory.from_config(cfg)


def _load_module():
    path = os.path.join(VERSIONS_DIR, MIGRATION_FILE)
    spec = importlib.util.spec_from_file_location("_migtest_080", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source() -> str:
    with open(os.path.join(VERSIONS_DIR, MIGRATION_FILE)) as fh:
        return fh.read()


def _code() -> str:
    """Executable source with the module docstring AND every ``#`` comment stripped.

    080's docstring and its inline comments discuss ALTER/VALIDATE/UPDATE locking at
    length ("SHARE UPDATE EXCLUSIVE", "never data") and name every constraint, so a
    naive substring scan over the raw file matches prose, not code. Comments are removed
    with ``tokenize`` rather than a regex so a ``#`` inside a string literal survives.
    """
    source = _source()
    tree = ast.parse(source)
    body = source
    if tree.body and isinstance(tree.body[0], ast.Expr) and isinstance(tree.body[0].value, ast.Constant):
        # Slice from the line AFTER the docstring's closing quotes, so the remainder is
        # still tokenizable (slicing on the docstring TEXT would leave a dangling `"""`).
        body = "".join(source.splitlines(keepends=True)[tree.body[0].end_lineno :])
    out = []
    for token in tokenize.generate_tokens(io.StringIO(body).readline):
        if token.type == tokenize.COMMENT:
            continue
        out.append(token)
    return tokenize.untokenize(out)


def _function_code(name: str) -> str:
    """Comment-free source of a single top-level ``def`` in the migration."""
    code = _code()
    start = code.index(f"def {name}(")
    rest = code[start:]
    tail = rest[1:]
    return rest[: 1 + tail.index("\ndef ")] if "\ndef " in tail else rest


def _metadata():
    import app.models  # noqa: F401  # register every model on Base.metadata
    from app.db.database import Base

    return Base.metadata


def _model_check_constraints(table_name: str) -> dict:
    """{constraint_name: CheckConstraint} declared on one model's table."""
    metadata = _metadata()
    assert table_name in metadata.tables, f"{table_name} missing from Base.metadata"
    return {
        constraint.name: constraint
        for constraint in metadata.tables[table_name].constraints
        if isinstance(constraint, sa.CheckConstraint) and constraint.name
    }


def _normalize(predicate: str) -> str:
    """Collapse runs of whitespace so formatting is free but semantics are not.

    ``"priority >= 1 AND priority <= 10"`` still differs from ``"priority >= 0 AND ..."``
    or ``"priority > 1 AND ..."``, so a real predicate edit fails; only re-wrapping and
    re-indentation pass.
    """
    return " ".join(str(predicate).split())


class _FakeDialect:
    def __init__(self, name):
        self.name = name


class _FakeConn:
    """A connection that fails loudly if the migration talks to it on SQLite."""

    def __init__(self, dialect_name):
        self.dialect = _FakeDialect(dialect_name)

    def execute(self, *args, **kwargs):  # pragma: no cover - must never be reached
        raise AssertionError("migration queried the DB on a non-Postgres dialect")


class _RecordingOp:
    """Stand-in for the alembic ``op`` proxy that records every statement."""

    def __init__(self, conn):
        self._conn = conn
        self.executed = []

    def get_bind(self):
        return self._conn

    def execute(self, statement):  # pragma: no cover - must never be reached
        self.executed.append(str(statement))

    def get_context(self):  # pragma: no cover - must never be reached
        raise AssertionError("migration opened an autocommit_block on a non-Postgres dialect")


# ---------------------------------------------------------------------------
# 1. Script wiring
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_single_head_and_revision_chain():
    """One head, and 080 sits on 079 inside the head's ancestry.

    Deliberately does NOT assert that 080 IS the head (the 076-test lesson: pinning
    head fails on every later migration for reasons unrelated to this revision).
    """
    scripts = _script_directory()
    heads = scripts.get_heads()
    assert len(heads) == 1, f"multiple alembic heads: {heads}"

    revision = scripts.get_revision(REVISION)
    assert revision.down_revision == DOWN_REVISION

    chain = {rev.revision for rev in scripts.iterate_revisions(heads[0], "base")}
    assert {REVISION, DOWN_REVISION} <= chain


@pytest.mark.unit
def test_revision_id_fits_alembic_version_varchar32():
    """alembic_version.version_num is varchar(32) on a freshly bootstrapped DB.

    The bootstrap path this migration exists to serve is create_all -> stamp -> upgrade,
    and the stamp writes this id, so an over-long id fails at the worst moment.
    """
    assert len(REVISION) <= 32


@pytest.mark.unit
def test_module_loads_from_the_expected_filename_and_exposes_its_api():
    assert os.path.exists(os.path.join(VERSIONS_DIR, MIGRATION_FILE))
    module = _load_module()
    assert module.revision == REVISION
    assert module.down_revision == DOWN_REVISION
    assert module.branch_labels is None
    assert module.depends_on is None
    for attr in ("upgrade", "downgrade", "_is_postgres", "_table_exists", "_constraint_state", "_equivalent_fk"):
        assert callable(getattr(module, attr)), f"{attr} missing from the migration"


# ---------------------------------------------------------------------------
# 2. The drift guard: test literals == migration literals == Base.metadata
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_migration_foreign_key_list_is_lock_step_with_this_test():
    """The migration's frozen FOREIGN_KEYS must match this test's frozen copy.

    The duplication is intentional (a migration must stay frozen against future edits);
    this is the check that keeps the two from drifting.
    """
    module = _load_module()
    expected = [(name, table, column, ref) for name, table, column, ref, _mirror in EXPECTED_FOREIGN_KEYS]
    assert [
        tuple(entry) for entry in module.FOREIGN_KEYS
    ] == expected, f"migration FOREIGN_KEYS drifted from the expected 22: {module.FOREIGN_KEYS!r}"
    assert len(module.FOREIGN_KEYS) == 22
    names = [entry[0] for entry in module.FOREIGN_KEYS]
    assert len(set(names)) == 22, "duplicate FK constraint name in the migration literal"


@pytest.mark.unit
def test_migration_check_list_is_lock_step_with_this_test():
    module = _load_module()
    assert [
        tuple(entry) for entry in module.CHECKS
    ] == EXPECTED_CHECKS, f"migration CHECKS drifted from the expected 19: {module.CHECKS!r}"
    assert len(module.CHECKS) == 19
    names = [entry[0] for entry in module.CHECKS]
    assert len(set(names)) == 19, "duplicate CHECK constraint name in the migration literal"


@pytest.mark.unit
def test_every_restored_foreign_key_is_mirrored_on_its_model():
    """All 22 FKs exist in Base.metadata so create_all reproduces them.

    Fourteen carry 003's own name plus ``ON DELETE SET NULL`` (matching 003's
    ``safe_create_fk`` semantics exactly, and with no ON UPDATE action). The other eight
    are the equivalence bucket: an UNNAMED model ForeignKey on the same column to the
    same table, which create_all emits auto-named and the migration skips rather than
    double-constraining. Both buckets are asserted positively AND negatively, so moving
    an FK between them fails here.
    """
    metadata = _metadata()

    for name, table_name, column, ref_table, mirror in EXPECTED_FOREIGN_KEYS:
        assert table_name in metadata.tables, f"{table_name} missing from Base.metadata"
        table = metadata.tables[table_name]
        assert column in table.c, f"{table_name}.{column} vanished from the model"

        on_column = [fk for fk in table.foreign_keys if fk.parent.name == column and fk.column.table.name == ref_table]
        assert on_column, f"{table_name}.{column} declares no model ForeignKey to {ref_table} (080 mirror missing)"
        assert (
            len(on_column) == 1
        ), f"{table_name}.{column} carries {len(on_column)} FKs to {ref_table} — never double-constrain"
        fk = on_column[0]
        assert fk.column.name == "id", f"{name} must reference {ref_table}.id"

        if mirror == "named":
            assert (
                fk.constraint.name == name
            ), f"{table_name}.{column} must mirror 003's name {name!r}, got {fk.constraint.name!r}"
            assert fk.ondelete == "SET NULL", f"{name} must be ON DELETE SET NULL (003's safe_create_fk default)"
            assert fk.onupdate is None, f"{name} must set no ON UPDATE action (003 set none)"
        else:
            # Equivalence bucket: create_all owns the name. Asserting it stays UNNAMED is
            # what makes the migration's _equivalent_fk skip the right thing — naming it
            # here without updating the migration would leave 003's twin unbuilt on a
            # migrated DB and built on a bootstrapped one, i.e. new drift.
            assert fk.constraint.name is None, (
                f"{table_name}.{column} is in the equivalence bucket and must stay unnamed on the model, "
                f"got {fk.constraint.name!r}"
            )

    named = [entry for entry in EXPECTED_FOREIGN_KEYS if entry[4] == "named"]
    equivalent = [entry for entry in EXPECTED_FOREIGN_KEYS if entry[4] == "equivalent"]
    assert len(named) == 14
    assert len(equivalent) == 8, "the migration header documents exactly 8 equivalence-probe columns"


@pytest.mark.unit
def test_every_restored_check_is_mirrored_on_its_model_with_the_same_predicate():
    """All 19 CHECKs exist in Base.metadata with byte-identical predicate text.

    This is the half of the lock-step the migration itself cannot cover: on SQLite the
    migration is a no-op, so the model declarations are the only thing standing between
    a bootstrapped DB and a migrated one — the exact drift class 080 fixes.
    """
    module = _load_module()
    migration_predicates = {name: predicate for name, _table, predicate in module.CHECKS}

    for name, table_name, predicate in EXPECTED_CHECKS:
        declared = _model_check_constraints(table_name)
        assert name in declared, f"{name} not declared on the {table_name} model"
        model_predicate = _normalize(declared[name].sqltext)
        assert model_predicate == _normalize(
            predicate
        ), f"{name} predicate drift: model {model_predicate!r} != expected {predicate!r}"
        assert model_predicate == _normalize(
            migration_predicates[name]
        ), f"{name} predicate drift: model {model_predicate!r} != migration {migration_predicates[name]!r}"
        assert declared[name].table.name == table_name


# ---------------------------------------------------------------------------
# 3. THE EXCLUSIONS ARE DELIBERATE
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_four_exclusions_stay_excluded():
    """003 declared four more constraints. Restoring any of them breaks shipped behavior.

    This is the guard against a future "complete the set" pass. Each is asserted absent
    from the migration literals AND from the models — both halves, because the lock-step
    convention means either one alone would re-create the drift.
    """
    module = _load_module()
    migration_names = {name for name, _table, _column, _ref in module.FOREIGN_KEYS}
    migration_names |= {name for name, _table, _predicate in module.CHECKS}

    for name, table_name in DELIBERATE_EXCLUSIONS:
        assert name not in migration_names, f"{name} was DELIBERATELY not restored by 080"
        assert name not in _model_check_constraints(
            table_name
        ), f"{name} must not be mirrored on the {table_name} model"

    declared_inventory = _model_check_constraints("inventory_items")
    declared_receipts = _model_check_constraints("po_receipts")
    declared_bom = _model_check_constraints("bom_items")
    declared_wc = _model_check_constraints("work_centers")

    # 1. chk_inventory_items_quantity_non_negative (quantity_on_hand >= 0).
    #    WHAT BREAKS: the material-consumption shortage posture (CLAUDE.md invariant 6)
    #    deliberately lets a SHORT COMPLETION drive a lot negative — record-and-alert,
    #    never a rolled-back write. This CHECK would make short completions fail at the
    #    DB with an IntegrityError instead of emitting a shortage event. Reversing it is
    #    a product decision, not a migration cleanup.
    assert not any(
        "quantity_on_hand" in _normalize(c.sqltext) for c in declared_inventory.values()
    ), "no CHECK may constrain inventory_items.quantity_on_hand — short completions must stay able to go negative"
    #    (quantity_allocated IS constrained: it is written once, as 0 at item creation,
    #    and no code path decrements it.)
    assert "chk_inventory_items_allocated_non_negative" in declared_inventory

    # 2. chk_po_receipts_quantity_received_positive (quantity_received > 0).
    #    WHAT BREAKS: void_receipt (PR #149) reconciles a voided receipt down to
    #    quantity_received = 0 on the soft-deleted row — 0 is RESERVED for void. This
    #    CHECK would make every receipt void fail with an IntegrityError. A future
    #    version must be void-aware (e.g. "quantity_received > 0 OR is_deleted").
    assert not any(
        "quantity_received" in _normalize(c.sqltext) for c in declared_receipts.values()
    ), "no CHECK may constrain po_receipts.quantity_received — receipt void writes 0 there"
    #    (The accepted/rejected siblings are unaffected by void and ARE restored.)
    assert "chk_po_receipts_quantity_accepted_non_negative" in declared_receipts
    assert "chk_po_receipts_quantity_rejected_non_negative" in declared_receipts

    # 3. chk_bom_items_quantity_positive (quantity > 0).
    #    WHAT BREAKS: a BOM line with quantity <= 0 is a DESIGNED, representable state.
    #    The backflush readiness layer carries blocking diagnostics zero_bom_quantity /
    #    negative_bom_quantity whose entire job is to surface such a line, refuse the
    #    backflush opt-in, and have a human correct it. This CHECK would make the bulk
    #    import fail at the DB on exactly the rows those diagnostics exist to catch, and
    #    turn the diagnostic machinery into dead code.
    assert not any(
        _normalize(c.sqltext).startswith("quantity ") for c in declared_bom.values()
    ), "no CHECK may constrain bom_items.quantity — non-positive BOM lines must stay representable"
    #    (scrap_factor IS constrained; nothing writes it outside [0, 1].)
    assert "chk_bom_items_scrap_factor_range" in declared_bom

    # 4. chk_work_centers_efficiency_range (efficiency between 0 and 200).
    #    WHAT BREAKS: nothing — it targeted a column that has NEVER existed. work_centers
    #    has always carried efficiency_factor (1.0-scale). 003's own required_columns
    #    guard skipped it even on DBs that really ran 003, so this is not drift. The
    #    natural successor on efficiency_factor is NOT re-derived here either: the prod
    #    pre-flight could not probe that column's bounds and the schema puts none on it.
    assert (
        "efficiency" not in _metadata().tables["work_centers"].c
    ), "work_centers grew a bare `efficiency` column — 003's CHECK would need re-deriving, see migration 080"
    assert "efficiency_factor" in _metadata().tables["work_centers"].c
    assert not any(
        "efficiency" in _normalize(c.sqltext) for c in declared_wc.values()
    ), "no efficiency CHECK may be added without a prod probe of efficiency_factor bounds first"


# ---------------------------------------------------------------------------
# 4. Postgres-only, online-safe, data-free
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_non_postgres_is_a_functional_early_return(direction, monkeypatch):
    """SQLite (local create_all / pytest) must be a true no-op in BOTH directions.

    Functional, not a grep: the migration runs against a stub ``op`` whose connection
    reports the sqlite dialect and whose ``execute`` / ``get_context`` raise. Deleting
    either early return fails here immediately.
    """
    module = _load_module()
    fake_op = _RecordingOp(_FakeConn("sqlite"))
    monkeypatch.setattr(module, "op", fake_op)

    getattr(module, direction)()

    assert fake_op.executed == [], f"{direction}() emitted SQL on a non-Postgres dialect: {fake_op.executed}"


@pytest.mark.unit
def test_every_op_execute_is_an_alter_table_and_every_read_is_a_select():
    """080 legitimately uses op.execute (raw ALTER is the point), so bound it by AST.

    Every ``op.execute`` argument must be an ``ALTER TABLE ...``; every ``conn.execute``
    a read-only ``SELECT``. That is a stronger statement than "no INSERT literal": an
    op.execute that grew a data statement fails here regardless of how it is spelled.
    """
    tree = ast.parse(_source())
    op_statements = []
    conn_statements = []

    def _literal_prefix(node):
        """First literal chunk of a str/f-string argument, or None."""
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr) and node.values:
            head = node.values[0]
            if isinstance(head, ast.Constant) and isinstance(head.value, str):
                return head.value
        return None

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        if node.func.attr != "execute" or not isinstance(node.func.value, ast.Name):
            continue
        assert node.args, "execute() called with no statement"
        arg = node.args[0]
        # Every call site wraps its SQL in sa.text(...); unwrap it.
        if isinstance(arg, ast.Call):
            assert arg.args, "sa.text() called with no SQL"
            arg = arg.args[0]
        prefix = _literal_prefix(arg)
        assert prefix is not None, "execute() must be called with a literal/f-string SQL statement"
        if node.func.value.id == "op":
            op_statements.append(prefix)
        elif node.func.value.id == "conn":
            conn_statements.append(prefix)

    assert op_statements, "expected the migration to issue ALTER statements via op.execute"
    for statement in op_statements:
        assert statement.lstrip().upper().startswith("ALTER TABLE"), f"op.execute must only ALTER: {statement!r}"

    assert conn_statements, "expected the pg_constraint probes to read via conn.execute"
    for statement in conn_statements:
        assert statement.lstrip().upper().startswith("SELECT"), f"conn.execute must only read: {statement!r}"


@pytest.mark.unit
def test_migration_performs_no_data_statement():
    """audit_logs carries the 008/060 tamper-evidence triggers and inventory_transactions
    is the regulated ledger — 080 must touch zero rows, in either direction.

    Comment-stripped so the header's "SHARE UPDATE EXCLUSIVE" prose can't mask a real
    UPDATE, and no-op-table-creating so the RLS new-table convention does not apply.
    """
    code = _code().upper()
    for statement in ("INSERT INTO", "DELETE FROM", "UPDATE SET", "TRUNCATE", "OP.BULK_INSERT", "SESSION("):
        assert statement not in code, f"080 must not run a data statement ({statement!r})"
    assert "OP.CREATE_TABLE" not in code, "080 creates no table, so the ENABLE ROW LEVEL SECURITY convention is n/a"
    assert "AUDIT_LOG" not in code, "no audit_logs constraint is in the set; the hash chain stays untouched"


@pytest.mark.unit
def test_upgrade_adds_not_valid_then_validates_inside_one_autocommit_block():
    """The online-safe two-phase pattern: ADD ... NOT VALID (no scan) then VALIDATE.

    Autocommit is the lock-separation guarantee — under transactional DDL the ACCESS
    EXCLUSIVE taken by ADD would be HELD THROUGH the VALIDATE scan. Losing either half
    turns this into a full-table write lock on work_orders in prod.
    """
    upgrade = _function_code("upgrade")
    assert "autocommit_block()" in upgrade
    assert "NOT VALID" in upgrade
    assert "VALIDATE CONSTRAINT" in _code(), "Phase 2 must issue VALIDATE"
    # Phase ordering: every ADD is appended to to_validate, and the VALIDATE sweep runs
    # after both ADD loops (cheap locking phase first, scanning phase second).
    assert upgrade.index("ADD CONSTRAINT") < upgrade.index("for table, name in to_validate")


@pytest.mark.unit
def test_upgrade_self_heals_an_interrupted_not_valid_leftover():
    """An interrupted prior run leaves ADD committed with convalidated = false.

    A plain existence probe would skip it forever, permanently masking a constraint that
    never scans its existing rows (the FK/CHECK twin of 079's indisvalid self-heal).
    """
    code = _code()
    assert "convalidated" in code
    assert "pg_constraint" in code
    state = _function_code("_constraint_state")
    assert '"not_validated"' in state and '"absent"' in state and '"valid"' in state
    upgrade = _function_code("upgrade")
    assert upgrade.count('if state == "not_validated":') == 2, "both the FK and the CHECK loop must re-VALIDATE"


@pytest.mark.unit
def test_downgrade_drops_exactly_the_003_names_and_nothing_else():
    """Constraints only, both frozen lists, no data, no hard-coded name.

    The strongest available source-level statement (079's precedent): downgrade names no
    constraint literally, so the ONLY things it can drop are the entries in FOREIGN_KEYS
    and CHECKS — which the lock-step tests above have already pinned.
    """
    downgrade = _function_code("downgrade")

    assert "DROP CONSTRAINT IF EXISTS" in downgrade, "drops must be IF EXISTS-guarded (round-trip safety)"
    assert "reversed(CHECKS)" in downgrade, "CHECKS must be dropped in reverse order"
    assert "reversed(FOREIGN_KEYS)" in downgrade, "FOREIGN_KEYS must be dropped in reverse order"
    assert "autocommit_block()" in downgrade

    for forbidden in ("DROP TABLE", "DROP COLUMN", "DROP INDEX", "DROP SCHEMA", "CASCADE"):
        assert forbidden not in downgrade.upper(), f"downgrade must drop constraints only ({forbidden!r} found)"

    # No constraint name may appear as a literal: every dropped name comes from the two
    # frozen lists, so downgrade cannot reach anything the upgrade did not add — and in
    # particular cannot reach the four deliberate exclusions.
    for name, _table, _column, _ref, _mirror in EXPECTED_FOREIGN_KEYS:
        assert name not in downgrade, f"downgrade hard-codes {name}; it must iterate FOREIGN_KEYS"
    for name, _table, _predicate in EXPECTED_CHECKS:
        assert name not in downgrade, f"downgrade hard-codes {name}; it must iterate CHECKS"
    for name, _table in DELIBERATE_EXCLUSIONS:
        assert name not in _code(), f"{name} must not appear anywhere in 080's executable body"


# ---------------------------------------------------------------------------
# 5. create_all parity: the bootstrap really emits what the migration builds
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_cyclic_fk_renders_as_a_post_create_alter_on_postgres():
    """``work_orders.current_operation_id`` -> ``work_order_operations.id`` closes an FK
    cycle with ``work_order_operations.work_order_id``.

    ``use_alter=True`` names WHICH edge breaks the cycle. Verified by removing it: nothing
    raises and nothing warns — SQLAlchemy silently picks its own edges and defers ALL
    FOURTEEN foreign keys on both tables to post-CREATE ALTERs, so the bootstrap DDL stops
    resembling the migrated schema and every one of them lands unnamed. With ``use_alter``
    pinned to this column exactly ONE post-CREATE ALTER is emitted, carrying 003's name and
    ``ON DELETE SET NULL``, so a bootstrapped DB and a migrated DB end up identical. The
    count is the assertion that catches the silent version.
    """
    metadata = _metadata()
    statements = []
    engine = sa.create_mock_engine(
        "postgresql://", lambda sql, *a, **kw: statements.append(str(sql.compile(dialect=engine.dialect)))
    )
    metadata.create_all(engine, checkfirst=False)

    alters = [statement.strip() for statement in statements if "ALTER TABLE" in statement]
    assert len(alters) == 1, f"expected exactly one post-CREATE ALTER (the cyclic FK), got {alters}"
    alter = alters[0]
    assert "ADD CONSTRAINT fk_work_orders_current_operation" in alter
    assert "FOREIGN KEY(current_operation_id) REFERENCES work_order_operations (id)" in alter
    assert "ON DELETE SET NULL" in alter
    assert "ON UPDATE" not in alter, "003 set no ON UPDATE action"


@pytest.mark.integration
def test_create_all_bootstrap_emits_every_check_and_named_fk(tmp_path):
    """SQLite create_all must build all 19 CHECKs and all 14 named FKs.

    On SQLite the migration is a no-op, so these model mirrors are the ONLY thing making
    a bootstrapped DB match a migrated one. The raw ``sqlite_master`` DDL is inspected so
    the predicate text itself is asserted, not just the constraint's existence.
    """
    metadata = _metadata()
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'mig080_parity.db'}")
    try:
        metadata.create_all(engine)
        with engine.connect() as conn:
            ddl = {
                name: sql
                for name, sql in conn.execute(
                    sa.text("SELECT name, sql FROM sqlite_master WHERE type = 'table' AND sql IS NOT NULL")
                ).fetchall()
            }

        for name, table_name, predicate in EXPECTED_CHECKS:
            assert table_name in ddl, f"create_all did not build {table_name}"
            table_ddl = " ".join(ddl[table_name].split())
            assert (
                f"CONSTRAINT {name} CHECK ({predicate})" in table_ddl
            ), f"create_all did not emit {name} with predicate {predicate!r} on {table_name}"

        for name, table_name, column, ref_table, mirror in EXPECTED_FOREIGN_KEYS:
            if mirror != "named":
                continue
            table_ddl = " ".join(ddl[table_name].split())
            assert (
                f"CONSTRAINT {name} FOREIGN KEY({column}) REFERENCES {ref_table} (id) ON DELETE SET NULL" in table_ddl
            ), f"create_all did not emit {name} on {table_name}.{column}"

        for name, table_name in DELIBERATE_EXCLUSIONS:
            if table_name in ddl:
                assert name not in ddl[table_name], f"{name} is a deliberate exclusion but create_all emitted it"
    finally:
        engine.dispose()


@pytest.mark.integration
def test_sqlite_enforces_the_restored_checks_but_not_the_excluded_ones(tmp_path):
    """The behavioural end of the exclusion argument, on a real DB.

    A restored CHECK must reject its bad row; an EXCLUDED one must let its row through —
    because a short completion (negative on-hand), a receipt void (0 received) and a
    zero-quantity BOM line are all states shipped code deliberately writes. Restoring any
    of the four would flip one of these inserts to an IntegrityError.
    """
    metadata = _metadata()
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'mig080_enforce.db'}")
    try:
        metadata.create_all(engine)

        restored_violations = [
            # (label, table, columns, values) — each violates exactly one restored CHECK.
            (
                "chk_work_orders_priority_range",
                "INSERT INTO work_orders (work_order_number, work_order_type, quantity_ordered, priority, "
                "version, is_deleted, company_id, part_id) VALUES ('WO-CHK-1', 'production', 1, 0, 1, 0, 1, 1)",
            ),
            (
                "chk_work_orders_quantity_ordered_positive",
                "INSERT INTO work_orders (work_order_number, work_order_type, quantity_ordered, priority, "
                "version, is_deleted, company_id, part_id) VALUES ('WO-CHK-2', 'production', 0, 5, 1, 0, 1, 1)",
            ),
            (
                "chk_quote_lines_quantity_positive",
                "INSERT INTO quote_lines (quote_id, line_number, description, quantity, unit_price, company_id) "
                "VALUES (1, 1, 'chk', 0, 10, 1)",
            ),
            (
                "chk_work_centers_hourly_rate_non_negative",
                "INSERT INTO work_centers (code, name, work_center_type, hourly_rate, company_id) "
                "VALUES ('WC-CHK', 'chk', 'machining', -5, 1)",
            ),
            (
                "chk_bom_items_scrap_factor_range",
                "INSERT INTO bom_items (bom_id, component_part_id, item_number, quantity, item_type, line_type, "
                "scrap_factor, company_id) VALUES (1, 1, 1, 1, 'component', 'make', 5, 1)",
            ),
        ]
        with engine.begin() as conn:
            for label, statement in restored_violations:
                with pytest.raises(sa.exc.IntegrityError) as excinfo:
                    with conn.begin_nested():
                        conn.execute(sa.text(statement))
                assert label in str(excinfo.value), f"expected {label} to reject the row, got {excinfo.value}"

        # The exclusions: shipped code writes exactly these rows.
        allowed = [
            # Short completion drives a lot negative — record-and-alert, not a rollback.
            "INSERT INTO inventory_items (part_id, location, quantity_on_hand, quantity_allocated, company_id) "
            "VALUES (1, 'RAW', -25, 0, 1)",
            # Receipt void reconciles quantity_received down to 0 on the soft-deleted row.
            "INSERT INTO po_receipts (receipt_number, po_line_id, quantity_received, quantity_accepted, "
            "quantity_rejected, lot_number, received_by, is_deleted, company_id) "
            "VALUES ('RC-CHK', 1, 0, 0, 0, 'LOT-CHK', 1, 1, 1)",
            # A zero-quantity BOM line must EXIST so the blocking diagnostic can surface it.
            "INSERT INTO bom_items (bom_id, component_part_id, item_number, quantity, item_type, line_type, "
            "company_id) VALUES (1, 1, 2, 0, 'component', 'make', 1)",
        ]
        with engine.begin() as conn:
            for statement in allowed:
                conn.execute(sa.text(statement))
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# 6. The second FK path: pinned relationships and pinned query joins
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_two_second_fk_paths_are_pinned_on_their_relationships():
    """``foreign_keys=`` must name the join column on both newly-ambiguous pairs.

    080 gives work_orders <-> work_order_operations a second FK path
    (current_operation_id alongside work_order_id) and time_entries -> users a second one
    (approved_by alongside user_id). Without the pins ``configure_mappers()`` raises and
    the whole app fails to import; with them, the pairs below must resolve to the
    ORIGINAL columns — pinning the wrong one would silently re-point every relationship.
    """
    from sqlalchemy.orm import configure_mappers

    from app.models.time_entry import TimeEntry
    from app.models.user import User
    from app.models.work_order import WorkOrder, WorkOrderOperation

    configure_mappers()

    def _pairs(model, attribute):
        relationship = sa.inspect(model).relationships[attribute]
        return {(local.name, remote.name) for local, remote in relationship.local_remote_pairs}

    assert _pairs(WorkOrder, "operations") == {("id", "work_order_id")}
    assert _pairs(WorkOrderOperation, "work_order") == {("work_order_id", "id")}
    assert _pairs(User, "time_entries") == {("id", "user_id")}
    # Both ends of the time_entries pair: approved_by must never become the join column.
    assert _pairs(TimeEntry, "user") == {("user_id", "id")}


@pytest.mark.unit
def test_ambiguous_implicit_joins_are_pinned_at_every_call_site():
    """An onclause-less ``join()`` between the newly two-FK-path tables now RAISES.

    ``Query.join(Entity)`` with no onclause resolves via Core foreign-key inference
    between the two TABLES, not via the ORM relationship, so the ``foreign_keys=`` pins
    above do NOT rescue it. Left unfixed this is a production 500 on the shop floor, not
    a test-only problem. Both halves are asserted: the ambiguity is real (so this test
    can't pass vacuously), and the shared dispatch/kiosk queue seam compiles anyway.
    """
    from sqlalchemy.orm import Session

    from app.models.time_entry import TimeEntry
    from app.models.user import User
    from app.models.work_order import WorkOrder, WorkOrderOperation
    from app.services.dispatch_service import queued_operations_query

    engine = sa.create_engine("sqlite://")
    try:
        session = Session(engine)

        # The ambiguity is real — this is what every unpinned call site would hit.
        # Both newly two-path pairs are covered: work_order_operations <-> work_orders
        # (current_operation_id) and time_entries -> users (approved_by).
        with pytest.raises(sa.exc.AmbiguousForeignKeysError):
            str(session.query(WorkOrderOperation).join(WorkOrder))
        with pytest.raises(sa.exc.AmbiguousForeignKeysError):
            str(session.query(TimeEntry).join(User))

        # queued_operations_query is the shared Dispatch Board / kiosk operator queue /
        # wallboard seam (CLAUDE.md flags it a don't-touch seam). It must still compile.
        compiled = str(queued_operations_query(session, company_id=1))
        assert "work_order_operations.work_order_id = work_orders.id" in compiled.replace("\n", " ")
    finally:
        engine.dispose()


@pytest.mark.unit
def test_no_unpinned_join_to_work_orders_survives_in_the_backend():
    """AST sweep: no ``.join(WorkOrder)`` / ``.outerjoin(WorkOrder)`` without an onclause.

    Seven such call sites existed when 080 landed (dispatch_service x2, work_centers,
    shop_floor, scheduling x2, scheduling_service). A new one added later compiles fine
    in isolation and only fails when the endpoint is actually hit — an HTTP 500 no unit
    test of that endpoint's helper would catch — so guard it statically here.

    Scoped to the ``work_orders`` <-> ``work_order_operations`` pair that 080 made
    cyclic. It is deliberately a shade broader than "provably ambiguous": AST cannot see
    the query's LEFT entity, and every other join to these tables in the codebase already
    passes an explicit onclause, so requiring one is both consistent and cheap.
    ``.join(User)`` is NOT swept — users is joined from a dozen unambiguous tables — but
    the same hazard exists for ``time_entries -> users``, which the previous test pins.
    """
    offenders = []
    for root, _dirs, files in os.walk(os.path.join(BACKEND_DIR, "app")):
        for filename in files:
            if not filename.endswith(".py"):
                continue
            path = os.path.join(root, filename)
            with open(path) as handle:
                try:
                    tree = ast.parse(handle.read())
                except SyntaxError:  # pragma: no cover
                    continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr not in ("join", "outerjoin"):
                    continue
                if len(node.args) != 1 or node.keywords:
                    continue
                target = node.args[0]
                if isinstance(target, ast.Name) and target.id in ("WorkOrder", "WorkOrderOperation"):
                    offenders.append(
                        f"{os.path.relpath(path, BACKEND_DIR)}:{node.lineno} .{node.func.attr}({target.id})"
                    )

    assert offenders == [], (
        "onclause-less joins to a table with two FK paths raise AmbiguousForeignKeysError at "
        f"query-compile time (HTTP 500). Pin the onclause: {offenders}"
    )


# ---------------------------------------------------------------------------
# 7. Real alembic round-trip on SQLite: a byte-identical no-op
# ---------------------------------------------------------------------------


def _alembic(db_url: str, *args: str):
    env = {**os.environ, "DATABASE_URL": db_url}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert (
        result.returncode == 0
    ), f"alembic {' '.join(args)} failed rc={result.returncode}\n{result.stdout}\n{result.stderr}"
    return result


def _table_ddl_snapshot(engine) -> dict:
    """{table_name: raw CREATE TABLE DDL} — a byte-level constraint snapshot.

    ``alembic_version`` is excluded: the stamp creates it, so it is bookkeeping about the
    round trip rather than part of the schema the round trip must leave alone.
    """
    with engine.connect() as conn:
        rows = conn.execute(
            sa.text(
                "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND sql IS NOT NULL "
                "AND name != 'alembic_version' ORDER BY name"
            )
        ).fetchall()
    return {name: ddl for name, ddl in rows}


@pytest.mark.integration
@pytest.mark.slow
def test_sqlite_upgrade_downgrade_round_trip_is_a_clean_no_op(tmp_path):
    """create_all -> stamp 079 -> upgrade 080 -> downgrade -> upgrade, byte-identical.

    The bootstrap path this migration exists to fix, replayed end-to-end: on SQLite both
    directions early-return, create_all already built every constraint, and the round
    trip must not add, drop, or reshape ANY table DDL. (Upgrades to REVISION, not
    ``head``, per the 076-test lesson — today they are the same revision.)
    """
    db_path = tmp_path / "mig080.db"
    db_url = f"sqlite:///{db_path}"

    metadata = _metadata()
    engine = sa.create_engine(db_url)
    try:
        metadata.create_all(engine)
        bootstrapped = _table_ddl_snapshot(engine)
        for _name, table_name, _predicate in EXPECTED_CHECKS:
            assert table_name in bootstrapped, f"create_all must build {table_name}"

        _alembic(db_url, "stamp", DOWN_REVISION)
        _alembic(db_url, "upgrade", REVISION)
        assert _table_ddl_snapshot(engine) == bootstrapped, "080 upgrade must be a no-op on SQLite"

        _alembic(db_url, "downgrade", "-1")
        assert _table_ddl_snapshot(engine) == bootstrapped, "080 downgrade must be a no-op on SQLite"

        _alembic(db_url, "upgrade", REVISION)
        assert _table_ddl_snapshot(engine) == bootstrapped, "re-upgrade must stay a no-op"
    finally:
        engine.dispose()
