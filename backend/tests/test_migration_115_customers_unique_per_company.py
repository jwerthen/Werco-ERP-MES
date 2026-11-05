"""Coverage for 115_customers_unique_per_company (customer name/code unique per company).

115 swaps the two GLOBAL unique indexes ``create_all`` built from the old
``unique=True, index=True`` columns for tenant-scoped unique constraints, keeping a plain
index on each column:

- ``ix_customers_name`` / ``ix_customers_code``  UNIQUE -> plain
- ``uq_customers_company_name`` / ``uq_customers_company_code``  new, ``(company_id, col)``

The behaviour (a second company may reuse a name and a code) is covered in
``tests/api/test_customers_tenant_isolation.py``; this file covers the MIGRATION in the
layers the suite already uses (...083, ...114):

1. **Script wiring + model lock-step** (unit) -- single head, the 114 -> 115 chain, an id
   that fits ``alembic_version``'s varchar(32), and a model that declares exactly the
   constraints and indexes the migration builds (the stamped-over hazard).
2. **What PostgreSQL receives** (unit) -- ``upgrade()`` / ``downgrade()`` rendered through
   alembic's real ``Operations`` in offline mode, with the catalog probes stubbed, so each
   guard is shown to fire independently and the DDL is asserted by dialect compilation
   rather than by trusting SQLite (CLAUDE.md: tests run on SQLite, production is Postgres).
3. **A real round-trip** (integration/slow) over a disposable SQLite file bootstrapped
   create_all -> stamp(114): the no-op upgrade, a real downgrade to the global shape, a
   real upgrade back, data surviving both, and the downgrade refusing -- before any DDL --
   once two companies share a name.
"""

import importlib.util
import io
import os
import re
import subprocess
import sys
from typing import Dict, List, Optional

import pytest
import sqlalchemy as sa

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSIONS_DIR = os.path.join(BACKEND_DIR, "alembic", "versions")

REVISION = "115_customers_unique_per_company"
DOWN_REVISION = "114_wo_dispatch_rank"
MIGRATION_FILE = "115_customers_unique_per_company.py"

TABLE = "customers"
INDEXES = {"name": "ix_customers_name", "code": "ix_customers_code"}
CONSTRAINTS = {"name": "uq_customers_company_name", "code": "uq_customers_company_code"}


def _script_directory() -> ScriptDirectory:
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))
    return ScriptDirectory.from_config(cfg)


def _load_module():
    path = os.path.join(VERSIONS_DIR, MIGRATION_FILE)
    spec = importlib.util.spec_from_file_location("_migtest_115", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    assert module.TABLE_NAME == TABLE
    assert module.INDEX_NAMES == INDEXES
    assert module.CONSTRAINT_NAMES == CONSTRAINTS


@pytest.mark.unit
def test_model_declares_the_per_company_constraints_and_plain_indexes():
    from app.models.customer import Customer

    table = Customer.__table__
    uniques = {c.name: [col.name for col in c.columns] for c in table.constraints if isinstance(c, sa.UniqueConstraint)}
    assert uniques == {
        "uq_customers_company_name": ["company_id", "name"],
        "uq_customers_company_code": ["company_id", "code"],
    }
    indexes = {ix.name: ([col.name for col in ix.columns], bool(ix.unique)) for ix in table.indexes}
    assert indexes[INDEXES["name"]] == (["name"], False)
    assert indexes[INDEXES["code"]] == (["code"], False)
    # The global shape is gone from the columns themselves, not just overridden.
    assert not table.c.name.unique
    assert not table.c.code.unique


@pytest.mark.unit
@pytest.mark.parametrize("dialect_name", ["postgresql", "sqlite"])
def test_create_all_ddl_compiles_the_per_company_constraints(dialect_name: str):
    from sqlalchemy.dialects import postgresql, sqlite
    from sqlalchemy.schema import CreateIndex, CreateTable

    from app.models.customer import Customer

    dialect = {"postgresql": postgresql.dialect(), "sqlite": sqlite.dialect()}[dialect_name]
    ddl = " ".join(str(CreateTable(Customer.__table__).compile(dialect=dialect)).split())
    assert "CONSTRAINT uq_customers_company_name UNIQUE (company_id, name)" in ddl
    assert "CONSTRAINT uq_customers_company_code UNIQUE (company_id, code)" in ddl
    for column, name in INDEXES.items():
        index = next(ix for ix in Customer.__table__.indexes if ix.name == name)
        assert str(CreateIndex(index).compile(dialect=dialect)) == f"CREATE INDEX {name} ON customers ({column})"


# ---------------------------------------------------------------------------
# 2. What PostgreSQL receives
# ---------------------------------------------------------------------------


def _render(
    function_name: str,
    *,
    dialect: str = "postgresql",
    probed: bool = False,
    constraints: Optional[Dict[str, List[str]]] = None,
    index_unique: Optional[Dict[str, Optional[bool]]] = None,
) -> List[str]:
    """Render ``upgrade()`` / ``downgrade()`` offline through alembic's real ``Operations``.

    ``probed=False`` is genuine ``--sql`` mode. ``probed=True`` stubs the catalog probes so
    the GUARDED path renders: ``constraints`` is what reflection reports, ``index_unique``
    maps an index name to ``True`` / ``False`` / ``None`` (absent).
    """
    module = _load_module()
    buffer = io.StringIO()
    context = MigrationContext.configure(dialect_name=dialect, opts={"as_sql": True, "output_buffer": buffer})
    module.op = Operations(context)
    if probed:
        module._offline = lambda: False
        module._unique_constraints = lambda: dict(constraints or {})
        module._index_is_unique = lambda name: (index_unique or {}).get(name)
        module._refuse_cross_row_duplicates = lambda: None
    with context.begin_transaction():
        getattr(module, function_name)()
    return [" ".join(s.split()) for s in buffer.getvalue().split(";") if s.strip()]


_POST_115 = dict(
    constraints={v: ["company_id", k] for k, v in CONSTRAINTS.items()},
    index_unique={v: False for v in INDEXES.values()},
)
_PRE_115 = dict(constraints={}, index_unique={v: True for v in INDEXES.values()})


@pytest.mark.unit
def test_offline_upgrade_builds_the_canonical_shape_under_a_bounded_lock():
    statements = _render("upgrade")
    assert statements[0] == "BEGIN"
    assert statements[1] == "SET LOCAL lock_timeout = '5s'"
    assert statements[-2] == "SET LOCAL lock_timeout = DEFAULT"
    body = statements[2:-2]
    assert "ALTER TABLE customers ADD CONSTRAINT uq_customers_company_name UNIQUE (company_id, name)" in body
    assert "ALTER TABLE customers ADD CONSTRAINT uq_customers_company_code UNIQUE (company_id, code)" in body
    for column, name in INDEXES.items():
        drop, create = f"DROP INDEX {name}", f"CREATE INDEX {name} ON customers ({column})"
        assert body.index(drop) < body.index(create)
    assert not any("UNIQUE INDEX" in s for s in body)


@pytest.mark.unit
def test_offline_downgrade_restores_the_global_unique_indexes():
    statements = _render("downgrade")
    body = statements[2:-2]
    assert "ALTER TABLE customers DROP CONSTRAINT uq_customers_company_name" in body
    assert "ALTER TABLE customers DROP CONSTRAINT uq_customers_company_code" in body
    for column, name in INDEXES.items():
        assert body.index(f"DROP INDEX {name}") < body.index(f"CREATE UNIQUE INDEX {name} ON customers ({column})")


@pytest.mark.unit
@pytest.mark.parametrize("dialect", ["postgresql", "sqlite"])
def test_the_bootstrapped_schema_is_a_clean_no_op(dialect: str):
    statements = _render("upgrade", dialect=dialect, probed=True, **_POST_115)
    assert [s for s in statements if s not in ("BEGIN", "COMMIT")] == []


@pytest.mark.unit
@pytest.mark.parametrize("dialect", ["postgresql", "sqlite"])
def test_an_already_downgraded_schema_downgrades_as_a_no_op(dialect: str):
    statements = _render("downgrade", dialect=dialect, probed=True, **_PRE_115)
    assert [s for s in statements if s not in ("BEGIN", "COMMIT")] == []


@pytest.mark.unit
def test_each_object_is_guarded_independently():
    # Only the name constraint missing: exactly that ADD, no index work.
    only_name = dict(_POST_115, constraints={CONSTRAINTS["code"]: ["company_id", "code"]})
    body = _render("upgrade", probed=True, **only_name)
    assert [s for s in body if "CONSTRAINT" in s or "INDEX" in s] == [
        "ALTER TABLE customers ADD CONSTRAINT uq_customers_company_name UNIQUE (company_id, name)"
    ]

    # Only the code index still unique: rebuilt plain, nothing else.
    code_unique = dict(_POST_115, index_unique={INDEXES["name"]: False, INDEXES["code"]: True})
    body = _render("upgrade", probed=True, **code_unique)
    assert [s for s in body if "CONSTRAINT" in s or "INDEX" in s] == [
        "DROP INDEX ix_customers_code",
        "CREATE INDEX ix_customers_code ON customers (code)",
    ]

    # The name index missing altogether: created plain, never dropped.
    name_missing = dict(_POST_115, index_unique={INDEXES["name"]: None, INDEXES["code"]: False})
    body = _render("upgrade", probed=True, **name_missing)
    assert [s for s in body if "CONSTRAINT" in s or "INDEX" in s] == [
        "CREATE INDEX ix_customers_name ON customers (name)"
    ]


@pytest.mark.unit
def test_a_legacy_single_column_unique_constraint_is_dropped():
    """The Postgres ``<table>_<col>_key`` shape (027 drops the same three shapes)."""
    legacy = dict(constraints={"customers_name_key": ["name"]}, index_unique={v: True for v in INDEXES.values()})
    body = _render("upgrade", probed=True, **legacy)
    assert "ALTER TABLE customers DROP CONSTRAINT customers_name_key" in body
    # The multi-column constraints are never mistaken for the legacy shape.
    body = _render("upgrade", probed=True, **_POST_115)
    assert not any("DROP CONSTRAINT" in s for s in body)


@pytest.mark.unit
def test_lock_timeout_is_postgres_only():
    # Index-only work, so SQLite renders offline (its batch rebuild needs a live database).
    index_work = dict(_POST_115, index_unique={v: True for v in INDEXES.values()})
    sqlite_statements = _render("upgrade", dialect="sqlite", probed=True, **index_work)
    assert "DROP INDEX ix_customers_name" in sqlite_statements
    assert not any("lock_timeout" in s for s in sqlite_statements)
    postgres_statements = _render("upgrade", dialect="postgresql", probed=True, **index_work)
    assert "SET LOCAL lock_timeout = '5s'" in postgres_statements
    assert postgres_statements[-2:] == ["SET LOCAL lock_timeout = DEFAULT", "COMMIT"]


@pytest.mark.unit
def test_pure_ddl_no_rls_or_role_statements():
    module = _load_module()
    with open(os.path.join(VERSIONS_DIR, MIGRATION_FILE)) as fh:
        source = fh.read()
    code = source[source.index(module.__doc__) + len(module.__doc__) :]
    for forbidden in ("ROW LEVEL SECURITY", "REVOKE", "GRANT", "UPDATE ", "INSERT", "create_table"):
        assert forbidden not in code, forbidden
    for role in ("anon", "authenticated", "service_role"):
        assert not re.search(rf"\b{role}\b", code), role


# ---------------------------------------------------------------------------
# 3. Real round-trip over a create_all-bootstrapped SQLite file
# ---------------------------------------------------------------------------


def _alembic(db_url: str, *args: str, expect_ok: bool = True) -> subprocess.CompletedProcess:
    env = {**os.environ, "DATABASE_URL": db_url}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if expect_ok:
        assert result.returncode == 0, f"alembic {' '.join(args)} failed\n{result.stdout}\n{result.stderr}"
    return result


def _shape(engine) -> tuple:
    inspector = sa.inspect(engine)
    uniques = sorted((u["name"], tuple(u["column_names"])) for u in inspector.get_unique_constraints(TABLE))
    indexes = sorted((ix["name"], tuple(ix["column_names"]), bool(ix["unique"])) for ix in inspector.get_indexes(TABLE))
    columns = [c["name"] for c in inspector.get_columns(TABLE)]
    fks = sorted((tuple(fk["constrained_columns"]), fk["referred_table"]) for fk in inspector.get_foreign_keys(TABLE))
    return uniques, indexes, columns, fks


def _insert(engine, company_id: int, name: str, code: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO customers (name, code, company_id, is_deleted, is_active) "
                "VALUES (:name, :code, :company_id, 0, 1)"
            ),
            {"name": name, "code": code, "company_id": company_id},
        )


@pytest.mark.integration
@pytest.mark.slow
def test_migration_115_round_trip_and_data(tmp_path):
    db_url = f"sqlite:///{tmp_path / 'mig115.db'}"

    import app.models  # noqa: F401  (registers every table on Base.metadata)
    from app.db.database import Base

    engine = sa.create_engine(db_url)
    try:
        Base.metadata.create_all(engine)
        with engine.begin() as conn:
            conn.execute(sa.text("INSERT INTO companies (id, name, slug, is_active) VALUES (1, 'A', 'a', 1)"))
            conn.execute(sa.text("INSERT INTO companies (id, name, slug, is_active) VALUES (2, 'B', 'b', 1)"))
        post_115 = _shape(engine)
        assert (CONSTRAINTS["name"], ("company_id", "name")) in post_115[0]

        _alembic(db_url, "stamp", DOWN_REVISION)

        # 1. Over the create_all schema every guard fires: nothing moves.
        _alembic(db_url, "upgrade", REVISION)
        assert _shape(engine) == post_115

        # 2. A real downgrade to the pre-115 GLOBAL shape.
        _alembic(db_url, "downgrade", "-1")
        uniques, indexes, columns, fks = _shape(engine)
        assert uniques == []
        assert (INDEXES["name"], ("name",), True) in indexes
        assert (INDEXES["code"], ("code",), True) in indexes
        assert (columns, fks) == post_115[2:]

        # The global shape really is global: a second company cannot reuse the name.
        _insert(engine, 1, "Acme", "ACM001")
        with pytest.raises(sa.exc.IntegrityError):
            _insert(engine, 2, "Acme", "B-1")

        # 3. A real upgrade back: exactly the create_all shape, the pre-existing row intact.
        _alembic(db_url, "upgrade", REVISION)
        assert _shape(engine) == post_115
        with engine.connect() as conn:
            assert conn.execute(sa.text("SELECT company_id, name, code FROM customers")).all() == [
                (1, "Acme", "ACM001")
            ]

        # Per company now: B may reuse A's name AND code, A still may not repeat its own.
        _insert(engine, 2, "Acme", "ACM001")
        with pytest.raises(sa.exc.IntegrityError):
            _insert(engine, 1, "Acme", "OTHER")
        with pytest.raises(sa.exc.IntegrityError):
            _insert(engine, 1, "Other", "ACM001")

        # 4. Downgrade REFUSES before any DDL once two companies share a value, and says how
        #    many collide without naming them (tenant data in deploy logs).
        refused = _alembic(db_url, "downgrade", "-1", expect_ok=False)
        assert refused.returncode != 0
        output = refused.stdout + refused.stderr
        assert "Cannot downgrade 115_customers_unique_per_company" in output
        assert "1 name value(s), 1 code value(s)" in output
        assert "Acme" not in output.split("Cannot downgrade", 1)[1].split("\n", 1)[0]
        assert _shape(engine) == post_115

        # 5. Re-runnable at the DDL level, not just alembic's bookkeeping.
        _alembic(db_url, "stamp", DOWN_REVISION)
        _alembic(db_url, "upgrade", REVISION)
        assert _shape(engine) == post_115
    finally:
        engine.dispose()
