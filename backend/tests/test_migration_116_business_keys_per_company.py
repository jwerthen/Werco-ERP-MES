"""Coverage for 116_business_keys_per_company (eight business keys unique per company).

116 swaps the eight GLOBAL unique indexes ``create_all`` built from the old
``unique=True, index=True`` columns for tenant-scoped unique constraints, keeping a plain
index on each column:

- ``ix_<table>_<column>``  UNIQUE -> plain
- ``uq_<table>_company_<column>``  new, ``(company_id, <column>)``

over ``ncrs.ncr_number``, ``cars.car_number``, ``fais.fai_number``,
``customer_complaints.complaint_number``, ``return_material_authorizations.rma_number``,
``equipment.equipment_id``, ``tools.tool_id`` and ``downtime_reason_codes.code``.

This file covers the MIGRATION in the layers ``test_migration_115_*`` uses:

1. **Script wiring + model lock-step** (unit) -- single head, the 115 -> 116 chain, an id
   that fits ``alembic_version``'s varchar(32), and models that declare exactly the
   constraints and indexes the migration builds (the stamped-over hazard).
2. **What PostgreSQL receives** (unit) -- ``upgrade()`` / ``downgrade()`` rendered through
   alembic's real ``Operations`` in offline mode, with the catalog probes stubbed, so each
   guard is shown to fire independently -- per object AND per table -- and the DDL is
   asserted by dialect compilation rather than by trusting SQLite (CLAUDE.md: tests run on
   SQLite, production is Postgres).
3. **A real round-trip** (integration/slow) over a disposable SQLite file bootstrapped
   create_all -> stamp(115): the no-op upgrade, a real downgrade to the global shape, a
   real upgrade back, data surviving both, and the downgrade refusing -- before any DDL --
   once two companies share a value in any of the eight columns.
"""

import importlib
import importlib.util
import io
import os
import re
import subprocess
import sys
from typing import Callable, Dict, List, Optional

import pytest
import sqlalchemy as sa

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSIONS_DIR = os.path.join(BACKEND_DIR, "alembic", "versions")

REVISION = "116_business_keys_per_company"
DOWN_REVISION = "115_customers_unique_per_company"
MIGRATION_FILE = "116_business_keys_per_company.py"

# table -> business-key column, in the migration's processing order.
UNIQUE_KEYS = {
    "ncrs": "ncr_number",
    "cars": "car_number",
    "fais": "fai_number",
    "customer_complaints": "complaint_number",
    "return_material_authorizations": "rma_number",
    "equipment": "equipment_id",
    "tools": "tool_id",
    "downtime_reason_codes": "code",
}
TABLES = list(UNIQUE_KEYS)

# Spelled out, not derived: a change to the migration's naming formula must fail here.
INDEXES = {
    "ncrs": "ix_ncrs_ncr_number",
    "cars": "ix_cars_car_number",
    "fais": "ix_fais_fai_number",
    "customer_complaints": "ix_customer_complaints_complaint_number",
    "return_material_authorizations": "ix_return_material_authorizations_rma_number",
    "equipment": "ix_equipment_equipment_id",
    "tools": "ix_tools_tool_id",
    "downtime_reason_codes": "ix_downtime_reason_codes_code",
}
CONSTRAINTS = {
    "ncrs": "uq_ncrs_company_ncr_number",
    "cars": "uq_cars_company_car_number",
    "fais": "uq_fais_company_fai_number",
    "customer_complaints": "uq_customer_complaints_company_complaint_number",
    "return_material_authorizations": "uq_return_material_authorizations_company_rma_number",
    "equipment": "uq_equipment_company_equipment_id",
    "tools": "uq_tools_company_tool_id",
    "downtime_reason_codes": "uq_downtime_reason_codes_company_code",
}

# table -> (module, class) of the owning model.
MODELS = {
    "ncrs": ("app.models.quality", "NonConformanceReport"),
    "cars": ("app.models.quality", "CorrectiveActionRequest"),
    "fais": ("app.models.quality", "FirstArticleInspection"),
    "customer_complaints": ("app.models.customer_complaint", "CustomerComplaint"),
    "return_material_authorizations": ("app.models.customer_complaint", "ReturnMaterialAuthorization"),
    "equipment": ("app.models.calibration", "Equipment"),
    "tools": ("app.models.tool_management", "Tool"),
    "downtime_reason_codes": ("app.models.downtime", "DowntimeReasonCode"),
}


def _script_directory() -> ScriptDirectory:
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))
    return ScriptDirectory.from_config(cfg)


def _load_module():
    path = os.path.join(VERSIONS_DIR, MIGRATION_FILE)
    spec = importlib.util.spec_from_file_location("_migtest_116", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _model_table(table: str) -> sa.Table:
    module_name, class_name = MODELS[table]
    model = getattr(importlib.import_module(module_name), class_name)
    assert model.__tablename__ == table
    return model.__table__


# ---------------------------------------------------------------------------
# 1. Wiring and model lock-step
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_single_head_and_revision_chain():
    scripts = _script_directory()
    heads = scripts.get_heads()
    assert len(heads) == 1, f"multiple alembic heads: {heads}"
    assert scripts.get_revision(REVISION).down_revision == DOWN_REVISION


@pytest.mark.unit
def test_revision_id_fits_alembic_version_varchar32():
    assert len(REVISION) <= 32


@pytest.mark.unit
def test_module_names_exactly_the_objects_it_owns():
    module = _load_module()
    assert module.revision == REVISION
    assert module.down_revision == DOWN_REVISION
    assert list(module.UNIQUE_KEYS.items()) == list(UNIQUE_KEYS.items())  # order included
    assert module.INDEX_NAMES == INDEXES
    assert module.CONSTRAINT_NAMES == CONSTRAINTS
    # Postgres truncates identifiers past 63 bytes; a truncated name would never match a probe.
    assert all(len(name) <= 63 for name in [*INDEXES.values(), *CONSTRAINTS.values()])


@pytest.mark.unit
@pytest.mark.parametrize("table", TABLES)
def test_model_declares_the_per_company_constraint_and_plain_index(table: str):
    column = UNIQUE_KEYS[table]
    model_table = _model_table(table)
    uniques = {
        c.name: [col.name for col in c.columns] for c in model_table.constraints if isinstance(c, sa.UniqueConstraint)
    }
    assert uniques == {CONSTRAINTS[table]: ["company_id", column]}
    indexes = {ix.name: ([col.name for col in ix.columns], bool(ix.unique)) for ix in model_table.indexes}
    assert indexes[INDEXES[table]] == ([column], False)
    # No other unique index covers the column on its own.
    assert not [name for name, (cols, unique) in indexes.items() if unique and cols == [column]]
    # The global shape is gone from the column itself, not just overridden.
    assert not model_table.c[column].unique
    assert model_table.c[column].index


@pytest.mark.unit
@pytest.mark.parametrize("dialect_name", ["postgresql", "sqlite"])
@pytest.mark.parametrize("table", TABLES)
def test_create_all_ddl_compiles_the_per_company_constraint(table: str, dialect_name: str):
    from sqlalchemy.dialects import postgresql, sqlite
    from sqlalchemy.schema import CreateIndex, CreateTable

    column = UNIQUE_KEYS[table]
    model_table = _model_table(table)
    dialect = {"postgresql": postgresql.dialect(), "sqlite": sqlite.dialect()}[dialect_name]
    ddl = " ".join(str(CreateTable(model_table).compile(dialect=dialect)).split())
    assert f"CONSTRAINT {CONSTRAINTS[table]} UNIQUE (company_id, {column})" in ddl
    index = next(ix for ix in model_table.indexes if ix.name == INDEXES[table])
    assert str(CreateIndex(index).compile(dialect=dialect)) == f"CREATE INDEX {INDEXES[table]} ON {table} ({column})"


# ---------------------------------------------------------------------------
# 2. What PostgreSQL receives
# ---------------------------------------------------------------------------


def _render(
    function_name: str,
    *,
    dialect: str = "postgresql",
    probed: bool = False,
    constraints: Optional[Dict[str, Dict[str, List[str]]]] = None,
    index_unique: Optional[Dict[str, Optional[bool]]] = None,
    index_probe: Optional[Callable[[str, str], Optional[bool]]] = None,
    refuse: Optional[Callable[[], None]] = None,
    buffer: Optional[io.StringIO] = None,
) -> List[str]:
    """Render ``upgrade()`` / ``downgrade()`` offline through alembic's real ``Operations``.

    ``probed=False`` is genuine ``--sql`` mode. ``probed=True`` stubs the catalog probes so
    the GUARDED path renders: ``constraints`` maps a table to what reflection reports for
    it, ``index_unique`` maps an index name to ``True`` / ``False`` / ``None`` (absent), and
    ``index_probe`` replaces that lookup outright (for a probe whose answer changes).
    """
    module = _load_module()
    buffer = buffer if buffer is not None else io.StringIO()
    context = MigrationContext.configure(dialect_name=dialect, opts={"as_sql": True, "output_buffer": buffer})
    module.op = Operations(context)
    if probed:
        module._offline = lambda: False
        module._unique_constraints = lambda table: dict((constraints or {}).get(table, {}))
        module._index_is_unique = index_probe or (lambda table, name: (index_unique or {}).get(name))
        module._refuse_cross_row_duplicates = refuse or (lambda: None)
    with context.begin_transaction():
        getattr(module, function_name)()
    return [" ".join(s.split()) for s in buffer.getvalue().split(";") if s.strip()]


def _ddl(statements: List[str]) -> List[str]:
    return [s for s in statements if "CONSTRAINT" in s or "INDEX" in s]


_POST_116 = dict(
    constraints={t: {CONSTRAINTS[t]: ["company_id", c]} for t, c in UNIQUE_KEYS.items()},
    index_unique={name: False for name in INDEXES.values()},
)
_PRE_116 = dict(constraints={}, index_unique={name: True for name in INDEXES.values()})


def _post_except(table: str, *, constraints=None, index_state="unchanged") -> dict:
    """The post-116 shape with ONE table's reflection replaced."""
    shape = {
        "constraints": {t: dict(v) for t, v in _POST_116["constraints"].items()},
        "index_unique": dict(_POST_116["index_unique"]),
    }
    if constraints is not None:
        shape["constraints"][table] = constraints
    if index_state != "unchanged":
        shape["index_unique"][INDEXES[table]] = index_state
    return shape


@pytest.mark.unit
def test_offline_upgrade_builds_the_canonical_shape_under_one_bounded_lock():
    statements = _render("upgrade")
    assert statements[0] == "BEGIN"
    assert statements[1] == "SET LOCAL lock_timeout = '5s'"
    assert statements[-2:] == ["SET LOCAL lock_timeout = DEFAULT", "COMMIT"]
    assert sum("lock_timeout" in s for s in statements) == 2  # taken once, handed back once
    body = statements[2:-2]
    adds = [s for s in body if "ADD CONSTRAINT" in s]
    assert adds == [
        f"ALTER TABLE {t} ADD CONSTRAINT {CONSTRAINTS[t]} UNIQUE (company_id, {c})" for t, c in UNIQUE_KEYS.items()
    ]
    for table, column in UNIQUE_KEYS.items():
        drop, create = f"DROP INDEX {INDEXES[table]}", f"CREATE INDEX {INDEXES[table]} ON {table} ({column})"
        assert body.index(drop) < body.index(create)
    assert not any("UNIQUE INDEX" in s for s in body)
    assert not any("DROP CONSTRAINT" in s for s in body)


@pytest.mark.unit
def test_offline_downgrade_restores_every_global_unique_index():
    statements = _render("downgrade")
    assert statements[1] == "SET LOCAL lock_timeout = '5s'"
    assert statements[-2:] == ["SET LOCAL lock_timeout = DEFAULT", "COMMIT"]
    body = statements[2:-2]
    for table, column in UNIQUE_KEYS.items():
        assert f"ALTER TABLE {table} DROP CONSTRAINT {CONSTRAINTS[table]}" in body
        unique = f"CREATE UNIQUE INDEX {INDEXES[table]} ON {table} ({column})"
        assert body.index(f"DROP INDEX {INDEXES[table]}") < body.index(unique)
    assert not any("ADD CONSTRAINT" in s for s in body)


@pytest.mark.unit
@pytest.mark.parametrize("dialect", ["postgresql", "sqlite"])
def test_the_bootstrapped_schema_is_a_clean_no_op(dialect: str):
    statements = _render("upgrade", dialect=dialect, probed=True, **_POST_116)
    assert [s for s in statements if s not in ("BEGIN", "COMMIT")] == []


@pytest.mark.unit
@pytest.mark.parametrize("dialect", ["postgresql", "sqlite"])
def test_an_already_downgraded_schema_downgrades_as_a_no_op(dialect: str):
    statements = _render("downgrade", dialect=dialect, probed=True, **_PRE_116)
    assert [s for s in statements if s not in ("BEGIN", "COMMIT")] == []


@pytest.mark.unit
@pytest.mark.parametrize("table", TABLES)
def test_each_object_is_guarded_independently_per_table(table: str):
    column = UNIQUE_KEYS[table]

    # Only this table's constraint missing: exactly that ADD, no index work anywhere.
    body = _render("upgrade", probed=True, **_post_except(table, constraints={}))
    assert _ddl(body) == [f"ALTER TABLE {table} ADD CONSTRAINT {CONSTRAINTS[table]} UNIQUE (company_id, {column})"]

    # Only this table's index still unique: rebuilt plain, nothing else.
    body = _render("upgrade", probed=True, **_post_except(table, index_state=True))
    assert _ddl(body) == [f"DROP INDEX {INDEXES[table]}", f"CREATE INDEX {INDEXES[table]} ON {table} ({column})"]

    # Only this table's index missing altogether: created plain, never dropped.
    body = _render("upgrade", probed=True, **_post_except(table, index_state=None))
    assert _ddl(body) == [f"CREATE INDEX {INDEXES[table]} ON {table} ({column})"]


@pytest.mark.unit
@pytest.mark.parametrize("table", TABLES)
def test_the_downgrade_is_guarded_independently_per_table(table: str):
    column = UNIQUE_KEYS[table]
    pre = {"constraints": {}, "index_unique": dict(_PRE_116["index_unique"])}

    # Only this table still carries its per-company constraint (its index already unique).
    still_keyed = dict(pre, constraints={table: {CONSTRAINTS[table]: ["company_id", column]}})
    body = _render("downgrade", probed=True, **still_keyed)
    assert _ddl(body) == [f"ALTER TABLE {table} DROP CONSTRAINT {CONSTRAINTS[table]}"]

    # Only this table's index still plain: rebuilt UNIQUE, nothing else.
    plain = dict(pre, index_unique={**pre["index_unique"], INDEXES[table]: False})
    body = _render("downgrade", probed=True, **plain)
    assert _ddl(body) == [
        f"DROP INDEX {INDEXES[table]}",
        f"CREATE UNIQUE INDEX {INDEXES[table]} ON {table} ({column})",
    ]

    # Only this table's index missing: created UNIQUE, never dropped.
    missing = dict(pre, index_unique={**pre["index_unique"], INDEXES[table]: None})
    body = _render("downgrade", probed=True, **missing)
    assert _ddl(body) == [f"CREATE UNIQUE INDEX {INDEXES[table]} ON {table} ({column})"]


@pytest.mark.unit
@pytest.mark.parametrize("table", TABLES)
def test_a_legacy_single_column_unique_constraint_is_dropped(table: str):
    """The Postgres ``<table>_<col>_key`` shape (027 drops the same three shapes)."""
    column = UNIQUE_KEYS[table]
    legacy_name = f"{table}_{column}_key"
    body = _render("upgrade", probed=True, **_post_except(table, constraints={legacy_name: [column]}, index_state=True))
    assert _ddl(body) == [
        f"ALTER TABLE {table} DROP CONSTRAINT {legacy_name}",
        f"ALTER TABLE {table} ADD CONSTRAINT {CONSTRAINTS[table]} UNIQUE (company_id, {column})",
        f"DROP INDEX {INDEXES[table]}",
        f"CREATE INDEX {INDEXES[table]} ON {table} ({column})",
    ]


@pytest.mark.unit
@pytest.mark.parametrize("table", TABLES)
def test_a_constraint_sharing_the_ix_name_is_dropped_and_the_index_reprobed(table: str):
    """On Postgres that constraint OWNS the ``ix_`` index: dropping it drops the index, so
    the index must be re-probed after the batch and created plain, never dropped twice."""
    column = UNIQUE_KEYS[table]
    dropped = {"done": False}
    post = _post_except(table, constraints={INDEXES[table]: [column]})

    def probe(probed_table: str, name: str) -> Optional[bool]:
        if name != INDEXES[table]:
            return post["index_unique"][name]
        # First probe (planning) sees the constraint's unique index; the re-probe does not.
        if dropped["done"]:
            return None
        dropped["done"] = True
        return True

    body = _render("upgrade", probed=True, constraints=post["constraints"], index_probe=probe)
    assert _ddl(body) == [
        f"ALTER TABLE {table} DROP CONSTRAINT {INDEXES[table]}",
        f"ALTER TABLE {table} ADD CONSTRAINT {CONSTRAINTS[table]} UNIQUE (company_id, {column})",
        f"CREATE INDEX {INDEXES[table]} ON {table} ({column})",
    ]


@pytest.mark.unit
def test_only_the_business_key_column_counts_as_legacy():
    # The multi-column constraints are never mistaken for the legacy shape...
    assert not any("DROP CONSTRAINT" in s for s in _render("upgrade", probed=True, **_POST_116))
    # ...nor is a single-column unique on some OTHER column of the table.
    other = _post_except("tools", constraints={CONSTRAINTS["tools"]: ["company_id", "tool_id"], "tools_x_key": ["x"]})
    assert not any("DROP CONSTRAINT" in s for s in _render("upgrade", probed=True, **other))
    # ...nor another table's business-key column name.
    crossed = _post_except("tools", constraints={CONSTRAINTS["tools"]: ["company_id", "tool_id"], "k": ["code"]})
    assert not any("DROP CONSTRAINT" in s for s in _render("upgrade", probed=True, **crossed))


@pytest.mark.unit
def test_lock_timeout_is_postgres_only():
    # Index-only work, so SQLite renders offline (its batch rebuild needs a live database).
    index_work = dict(_POST_116, index_unique={name: True for name in INDEXES.values()})
    sqlite_statements = _render("upgrade", dialect="sqlite", probed=True, **index_work)
    assert "DROP INDEX ix_ncrs_ncr_number" in sqlite_statements
    assert not any("lock_timeout" in s for s in sqlite_statements)
    postgres_statements = _render("upgrade", dialect="postgresql", probed=True, **index_work)
    assert postgres_statements[1] == "SET LOCAL lock_timeout = '5s'"
    assert postgres_statements.count("SET LOCAL lock_timeout = '5s'") == 1
    assert postgres_statements[-2:] == ["SET LOCAL lock_timeout = DEFAULT", "COMMIT"]


@pytest.mark.unit
def test_the_downgrade_refusal_runs_before_any_ddl():
    buffer = io.StringIO()

    def refuse() -> None:
        raise RuntimeError("Cannot downgrade 116_business_keys_per_company: 1 ncrs.ncr_number value(s) ...")

    with pytest.raises(RuntimeError, match="Cannot downgrade 116"):
        _render("downgrade", probed=True, refuse=refuse, buffer=buffer, **_POST_116)
    assert [s.strip() for s in buffer.getvalue().split(";") if s.strip()] == ["BEGIN"]


@pytest.mark.unit
def test_pure_ddl_no_rls_or_role_statements():
    module = _load_module()
    with open(os.path.join(VERSIONS_DIR, MIGRATION_FILE)) as fh:
        source = fh.read()
    code = source[source.index(module.__doc__) + len(module.__doc__) :]
    for forbidden in ("ROW LEVEL SECURITY", "REVOKE", "GRANT", "UPDATE ", "INSERT", "create_table", "audit_log"):
        assert forbidden not in code, forbidden
    for role in ("anon", "authenticated", "service_role"):
        assert not re.search(rf"\b{role}\b", code), role


# ---------------------------------------------------------------------------
# 3. Real round-trip over a create_all-bootstrapped SQLite file
# ---------------------------------------------------------------------------

# table -> the NOT NULL, default-less columns besides the key and company_id (looked up from
# the create_all schema), with a minimal valid value. ``fais.part_id`` points at a real part.
_REQUIRED = {
    "ncrs": {"source": "IN_PROCESS", "title": "t", "description": "d", "is_deleted": 0},
    "cars": {"title": "t", "problem_description": "d"},
    "fais": {"part_id": None},  # filled per company below
    "customer_complaints": {"customer_name": "c", "title": "t", "description": "d"},
    "return_material_authorizations": {"customer_name": "c", "quantity": 1, "reason": "r"},
    "equipment": {"name": "n"},
    "tools": {"name": "n"},
    "downtime_reason_codes": {"name": "n", "category": "MECHANICAL"},
}
_PART_FOR_COMPANY = {1: 101, 2: 102}

# A distinctive value per table: the refusal must never echo one.
SHARED = {table: f"TENANT-VALUE-{i}" for i, table in enumerate(TABLES)}


def _alembic(db_url: str, *args: str, expect_ok: bool = True) -> subprocess.CompletedProcess:
    env = {**os.environ, "DATABASE_URL": db_url}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if expect_ok:
        assert result.returncode == 0, f"alembic {' '.join(args)} failed\n{result.stdout}\n{result.stderr}"
    return result


def _shape(engine, table: str) -> tuple:
    inspector = sa.inspect(engine)
    uniques = sorted((u["name"], tuple(u["column_names"])) for u in inspector.get_unique_constraints(table))
    indexes = sorted((ix["name"], tuple(ix["column_names"]), bool(ix["unique"])) for ix in inspector.get_indexes(table))
    columns = [c["name"] for c in inspector.get_columns(table)]
    fks = sorted((tuple(fk["constrained_columns"]), fk["referred_table"]) for fk in inspector.get_foreign_keys(table))
    return uniques, indexes, columns, fks


def _shapes(engine) -> Dict[str, tuple]:
    return {table: _shape(engine, table) for table in TABLES}


def _insert(engine, table: str, company_id: int, value: str) -> None:
    row = {**_REQUIRED[table], "company_id": company_id, UNIQUE_KEYS[table]: value}
    if table == "fais":
        row["part_id"] = _PART_FOR_COMPANY[company_id]
    columns = ", ".join(row)
    params = ", ".join(f":{name}" for name in row)
    with engine.begin() as conn:
        conn.execute(sa.text(f"INSERT INTO {table} ({columns}) VALUES ({params})"), row)


@pytest.mark.unit
def test_round_trip_inserts_cover_every_required_column():
    """``_REQUIRED`` must name every NOT NULL, default-less column, so the round-trip's
    inserts fail only on the unique key under test, never on a missing column."""
    import app.models  # noqa: F401  (registers every table on Base.metadata)
    from app.db.database import Base

    for table in TABLES:
        model_table = Base.metadata.tables[table]
        required = {
            c.name
            for c in model_table.columns
            if not c.nullable and c.server_default is None and not (c.primary_key and c.autoincrement in (True, "auto"))
        }
        provided = {"company_id", UNIQUE_KEYS[table], *_REQUIRED[table]}
        assert required - provided == set(), table
        assert provided <= set(model_table.columns.keys()), table


@pytest.mark.integration
@pytest.mark.slow
def test_migration_116_round_trip_and_data(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'mig116.db'}"

    import app.models  # noqa: F401  (registers every table on Base.metadata)
    from app.db.database import Base

    engine = sa.create_engine(db_url)
    try:
        Base.metadata.create_all(engine)
        with engine.begin() as conn:
            conn.execute(sa.text("INSERT INTO companies (id, name, slug, is_active) VALUES (1, 'A', 'a', 1)"))
            conn.execute(sa.text("INSERT INTO companies (id, name, slug, is_active) VALUES (2, 'B', 'b', 1)"))
            for company_id, part_id in _PART_FOR_COMPANY.items():
                conn.execute(
                    sa.text(
                        "INSERT INTO parts (id, part_number, name, part_type, company_id) "
                        "VALUES (:id, :number, 'P', 'MANUFACTURED', :company_id)"
                    ),
                    {"id": part_id, "number": f"P-{company_id}", "company_id": company_id},
                )
        post_116 = _shapes(engine)
        for table, column in UNIQUE_KEYS.items():
            assert (CONSTRAINTS[table], ("company_id", column)) in post_116[table][0]
            assert (INDEXES[table], (column,), False) in post_116[table][1]

        _alembic(db_url, "stamp", DOWN_REVISION)

        # 1. Over the create_all schema every guard fires: nothing moves.
        _alembic(db_url, "upgrade", REVISION)
        assert _shapes(engine) == post_116

        # 2. A real downgrade to the pre-116 GLOBAL shape, table by table.
        _alembic(db_url, "downgrade", "-1")
        downgraded = _shapes(engine)
        for table, column in UNIQUE_KEYS.items():
            uniques, indexes, columns, fks = downgraded[table]
            assert uniques == [], table
            assert (INDEXES[table], (column,), True) in indexes, table
            assert (columns, fks) == post_116[table][2:], table

        # The global shape really is global: a second company cannot reuse the value.
        for table in TABLES:
            _insert(engine, table, 1, SHARED[table])
            with pytest.raises(sa.exc.IntegrityError):
                _insert(engine, table, 2, SHARED[table])

        # 3. A real upgrade back: exactly the create_all shape, every pre-existing row intact.
        _alembic(db_url, "upgrade", REVISION)
        assert _shapes(engine) == post_116
        with engine.connect() as conn:
            for table, column in UNIQUE_KEYS.items():
                rows = conn.execute(sa.text(f"SELECT company_id, {column} FROM {table}")).all()
                assert rows == [(1, SHARED[table])], table

        # Per company now: B may reuse A's value, A still may not repeat its own.
        for table in TABLES:
            _insert(engine, table, 2, SHARED[table])
            with pytest.raises(sa.exc.IntegrityError):
                _insert(engine, table, 1, SHARED[table])

        # 4. Downgrade REFUSES before any DDL once two companies share a value, naming every
        #    colliding table.column with a count and never a value (tenant data in deploy logs).
        refused = _alembic(db_url, "downgrade", "-1", expect_ok=False)
        assert refused.returncode != 0
        output = refused.stdout + refused.stderr
        assert "Cannot downgrade 116_business_keys_per_company" in output
        message = output.split("Cannot downgrade", 1)[1].split("\n", 1)[0]
        expected_detail = ", ".join(f"1 {table}.{column} value(s)" for table, column in UNIQUE_KEYS.items())
        assert expected_detail in message
        for value in SHARED.values():
            assert value not in message
        assert _shapes(engine) == post_116

        # 5. Re-runnable at the DDL level, not just alembic's bookkeeping.
        _alembic(db_url, "stamp", DOWN_REVISION)
        _alembic(db_url, "upgrade", REVISION)
        assert _shapes(engine) == post_116
    finally:
        engine.dispose()
