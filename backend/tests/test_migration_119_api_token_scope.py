"""Coverage for 119_api_token_scope (read-only and AI-external API tokens).

119 is purely additive over the EXISTING ``api_tokens`` table -- two columns and one CHECK,
no data:

- ``api_tokens.scope``        ``VARCHAR(16) NOT NULL DEFAULT 'full'``
- ``api_tokens.ai_external``  ``BOOLEAN NOT NULL DEFAULT 'false'``
- ``ck_api_tokens_scope``     ``CHECK (scope IN ('full', 'read_only'))``

The behavior (mint, the API-layer gates, the MCP door) is covered by
``tests/test_api_token_read_only.py`` and ``tests/test_mcp_read_only.py``; this file
covers the MIGRATION, in the layers the suite already uses (088, 117):

1. **Script wiring** (unit) -- one head, the 118 -> 119 chain, an id that fits
   ``alembic_version``'s varchar(32).
2. **What the functions EMIT** (unit) -- ``upgrade()`` / ``downgrade()`` run against a
   recording stand-in for ``op`` with every guard stubbed, both ways and independently.
2b. **The SQL PostgreSQL receives** (unit) -- offline (``as_sql``) rendering.
3. **Source invariants** (unit) -- pure DDL: no backfill, no table created (so no RLS
   statement), no ``audit_log`` contact, a real downgrade.
4. **Model / migration lock-step** (unit) -- the model declares the same two columns and
   the byte-identical CHECK, DIALECT-COMPILED for both engines.
5. **A real round-trip** (integration/slow) over a disposable create_all-bootstrapped
   SQLite file, plus the data claim: a token minted BEFORE 119 comes out ``full`` /
   ``false`` -- exactly how it always behaved -- and the CHECK refuses a third scope on the
   schema the migration built.

THE THINGS A FUTURE READER WILL TRY TO "FIX"
--------------------------------------------
* **No backfill.** The server defaults ARE the backfill: every pre-119 token is full and
  not AI-external, which is what it already was. An ``UPDATE`` would buy nothing and
  would make the ADD COLUMN a rewrite.
* **088 is not edited** to mention the columns. It is an applied migration; its test
  (``LATER_COLUMNS``) is what changed.
"""

import importlib.util
import io
import os
import re
import subprocess
import sys
from contextlib import contextmanager
from types import SimpleNamespace
from typing import List

import pytest
import sqlalchemy as sa

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSIONS_DIR = os.path.join(BACKEND_DIR, "alembic", "versions")

REVISION = "119_api_token_scope"
MIGRATION_FILE = "119_api_token_scope.py"
DOWN_REVISION = "118_dashboard_layouts"

TABLE = "api_tokens"
SCOPE = "scope"
AI_EXTERNAL = "ai_external"
CHECK_NAME = "ck_api_tokens_scope"
CHECK_CONDITION = "scope IN ('full', 'read_only')"

LOCK_TIMEOUT = "SET LOCAL lock_timeout = '5s'"
RESET_LOCK_TIMEOUT = "SET LOCAL lock_timeout = DEFAULT"


def _script_directory() -> ScriptDirectory:
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))
    return ScriptDirectory.from_config(cfg)


def _load_module():
    path = os.path.join(VERSIONS_DIR, MIGRATION_FILE)
    spec = importlib.util.spec_from_file_location("_migtest_119", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source() -> str:
    with open(os.path.join(VERSIONS_DIR, MIGRATION_FILE)) as fh:
        return fh.read()


def _body() -> str:
    """Source with the module docstring stripped (the prose names what the code avoids)."""
    module = _load_module()
    docstring = module.__doc__ or ""
    source = _source()
    return source[source.index(docstring) + len(docstring) :] if docstring else source


# ---------------------------------------------------------------------------
# Recording the DDL the migration actually emits (088 / 117's idiom)
# ---------------------------------------------------------------------------


class _RecordedDDL:
    """A stand-in for alembic's ``op`` that records calls instead of executing them."""

    def __init__(self, dialect_name: str):
        self.dialect_name = dialect_name
        self.calls: list = []

    def get_bind(self):  # pragma: no cover - the guards are stubbed; reaching this is a bug
        raise AssertionError("the recorder stubs every guard; nothing may inspect a database")

    def get_context(self):
        return SimpleNamespace(as_sql=False, dialect=SimpleNamespace(name=self.dialect_name))

    def add_column(self, table_name, column, **kwargs):
        self.calls.append(("add_column", table_name, column))

    def drop_column(self, table_name, column_name, **kwargs):
        self.calls.append(("drop_column", table_name, column_name))

    def create_check_constraint(self, name, table_name, condition, **kwargs):
        self.calls.append(("create_check_constraint", table_name, (name, condition)))

    def drop_constraint(self, name, table_name, type_=None, **kwargs):
        self.calls.append(("drop_constraint", table_name, (name, type_)))

    @contextmanager
    def batch_alter_table(self, table_name, **kwargs):
        recorder = self

        class _Batch:
            def add_column(self, column, **kw):
                recorder.calls.append(("batch_add_column", table_name, column))

            def drop_column(self, column_name, **kw):
                recorder.calls.append(("batch_drop_column", table_name, column_name))

            def create_check_constraint(self, name, condition, **kw):
                recorder.calls.append(("batch_create_check_constraint", table_name, (name, condition)))

            def drop_constraint(self, name, type_=None, **kw):
                recorder.calls.append(("batch_drop_constraint", table_name, (name, type_)))

        yield _Batch()

    def execute(self, statement):
        self.calls.append(("execute", str(statement)))

    @property
    def sequence(self) -> list:
        return [(call[0], call[1]) for call in self.calls]

    def subjects(self, kind: str) -> list:
        """The column name / constraint name of every call of ``kind``."""
        out = []
        for call in self.calls:
            if call[0] != kind:
                continue
            subject = call[2]
            out.append(
                subject.name if isinstance(subject, sa.Column) else subject if isinstance(subject, str) else subject[0]
            )
        return out


def _record(function_name: str, *, dialect: str, scope: bool, ai_external: bool, check: bool) -> _RecordedDDL:
    """Run ``function_name`` with each object's existence stubbed independently."""
    module = _load_module()
    recorder = _RecordedDDL(dialect)
    module.op = recorder
    present = {SCOPE: scope, AI_EXTERNAL: ai_external}
    module._has_table = lambda _table: True
    module._has_column = lambda _table, column: present[column]
    module._has_check_constraint = lambda _table, _name: check
    getattr(module, function_name)()
    return recorder


def _render(function_name: str, *, dialect: str) -> List[str]:
    """``upgrade()`` / ``downgrade()`` as the SQL the dialect receives, via offline mode."""
    module = _load_module()
    buffer = io.StringIO()
    context = MigrationContext.configure(dialect_name=dialect, opts={"as_sql": True, "output_buffer": buffer})
    module.op = Operations(context)

    def _must_not_inspect(*_args, **_kwargs):  # pragma: no cover - failure path
        raise AssertionError("offline mode must not inspect a database")

    module._has_column = _must_not_inspect
    module._has_table = _must_not_inspect
    module._has_check_constraint = _must_not_inspect
    with context.begin_transaction():
        getattr(module, function_name)()
    return [" ".join(statement.split()) for statement in buffer.getvalue().split(";") if statement.strip()]


def _model_table():
    from app.models.api_token import ApiToken

    return ApiToken.__table__


def _model_check():
    [check] = [
        constraint
        for constraint in _model_table().constraints
        if isinstance(constraint, sa.CheckConstraint) and constraint.name == CHECK_NAME
    ]
    return check


# ---------------------------------------------------------------------------
# 1. Script wiring
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
    assert (module.SCOPE_COLUMN, module.AI_EXTERNAL_COLUMN) == (SCOPE, AI_EXTERNAL)
    assert (module.CHECK_NAME, module.CHECK_CONDITION) == (CHECK_NAME, CHECK_CONDITION)
    assert (module.LOCK_TIMEOUT_SQL, module.RESET_LOCK_TIMEOUT_SQL) == (LOCK_TIMEOUT, RESET_LOCK_TIMEOUT)


# ---------------------------------------------------------------------------
# 2. What upgrade() / downgrade() emit
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_postgres_virgin_upgrade_adds_both_columns_then_the_check_under_a_local_lock_timeout():
    recorder = _record("upgrade", dialect="postgresql", scope=False, ai_external=False, check=False)
    assert recorder.sequence == [
        ("execute", LOCK_TIMEOUT),
        ("add_column", TABLE),
        ("add_column", TABLE),
        ("create_check_constraint", TABLE),
        ("execute", RESET_LOCK_TIMEOUT),
    ]
    assert recorder.subjects("add_column") == [SCOPE, AI_EXTERNAL]
    assert recorder.calls[3][2] == (CHECK_NAME, CHECK_CONDITION)


@pytest.mark.unit
def test_sqlite_virgin_upgrade_runs_in_batch_mode_and_sets_no_lock_timeout():
    """SQLite has no ALTER TABLE ... ADD CONSTRAINT, and lock_timeout is a PostgreSQL setting."""
    recorder = _record("upgrade", dialect="sqlite", scope=False, ai_external=False, check=False)
    assert recorder.sequence == [
        ("batch_add_column", TABLE),
        ("batch_add_column", TABLE),
        ("batch_create_check_constraint", TABLE),
    ]
    assert recorder.subjects("batch_add_column") == [SCOPE, AI_EXTERNAL]


@pytest.mark.unit
@pytest.mark.parametrize("dialect", ["postgresql", "sqlite"])
def test_the_bootstrapped_database_is_a_clean_no_op(dialect: str):
    """create_all built all three from the model: nothing -- not even the lock_timeout."""
    assert _record("upgrade", dialect=dialect, scope=True, ai_external=True, check=True).calls == []


@pytest.mark.unit
@pytest.mark.parametrize("dialect", ["postgresql", "sqlite"])
@pytest.mark.parametrize(
    "scope,ai_external,check",
    [(True, False, False), (False, True, False), (True, True, False), (False, False, True), (True, False, True)],
)
def test_each_object_is_guarded_independently(dialect: str, scope: bool, ai_external: bool, check: bool):
    """A partially applied schema gets exactly the missing objects -- never a duplicate."""
    recorder = _record("upgrade", dialect=dialect, scope=scope, ai_external=ai_external, check=check)
    prefix = "batch_" if dialect == "sqlite" else ""
    expected_columns = [name for name, present in ((SCOPE, scope), (AI_EXTERNAL, ai_external)) if not present]
    assert recorder.subjects(f"{prefix}add_column") == expected_columns
    assert recorder.subjects(f"{prefix}create_check_constraint") == ([] if check else [CHECK_NAME])


@pytest.mark.unit
def test_postgres_downgrade_drops_the_check_before_the_columns():
    recorder = _record("downgrade", dialect="postgresql", scope=True, ai_external=True, check=True)
    assert recorder.sequence == [
        ("execute", LOCK_TIMEOUT),
        ("drop_constraint", TABLE),
        ("drop_column", TABLE),
        ("drop_column", TABLE),
        ("execute", RESET_LOCK_TIMEOUT),
    ]
    assert recorder.calls[1][2] == (CHECK_NAME, "check")
    assert recorder.subjects("drop_column") == [AI_EXTERNAL, SCOPE]


@pytest.mark.unit
def test_sqlite_downgrade_drops_the_check_before_the_column_it_names_in_batch_mode():
    recorder = _record("downgrade", dialect="sqlite", scope=True, ai_external=True, check=True)
    assert recorder.calls == [
        ("batch_drop_constraint", TABLE, (CHECK_NAME, "check")),
        ("batch_drop_column", TABLE, AI_EXTERNAL),
        ("batch_drop_column", TABLE, SCOPE),
    ]


@pytest.mark.unit
@pytest.mark.parametrize("dialect", ["postgresql", "sqlite"])
def test_downgrade_over_an_already_downgraded_database_is_a_no_op(dialect: str):
    assert _record("downgrade", dialect=dialect, scope=False, ai_external=False, check=False).calls == []


@pytest.mark.unit
def test_the_columns_are_not_null_with_plain_string_server_defaults():
    recorder = _record("upgrade", dialect="postgresql", scope=False, ai_external=False, check=False)
    columns = {call[2].name: call[2] for call in recorder.calls if call[0] == "add_column"}
    scope = columns[SCOPE]
    assert isinstance(scope.type, sa.String) and scope.type.length == 16
    assert scope.nullable is False
    assert scope.server_default.arg == "full"
    ai_external = columns[AI_EXTERNAL]
    assert isinstance(ai_external.type, sa.Boolean)
    assert ai_external.nullable is False
    assert isinstance(ai_external.server_default.arg, str), "plain string like `revoked`, never sa.text('false')"
    assert ai_external.server_default.arg == "false"


# ---------------------------------------------------------------------------
# 2b. The SQL PostgreSQL actually receives (offline rendering)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_postgres_upgrade_sql():
    """NOT NULL with a constant DEFAULT: metadata-only ADD COLUMN on PostgreSQL 11+."""
    assert _render("upgrade", dialect="postgresql") == [
        "BEGIN",
        LOCK_TIMEOUT,
        f"ALTER TABLE {TABLE} ADD COLUMN {SCOPE} VARCHAR(16) DEFAULT 'full' NOT NULL",
        f"ALTER TABLE {TABLE} ADD COLUMN {AI_EXTERNAL} BOOLEAN DEFAULT 'false' NOT NULL",
        f"ALTER TABLE {TABLE} ADD CONSTRAINT {CHECK_NAME} CHECK ({CHECK_CONDITION})",
        RESET_LOCK_TIMEOUT,
        "COMMIT",
    ]


@pytest.mark.unit
def test_postgres_downgrade_sql():
    assert _render("downgrade", dialect="postgresql") == [
        "BEGIN",
        LOCK_TIMEOUT,
        f"ALTER TABLE {TABLE} DROP CONSTRAINT {CHECK_NAME}",
        f"ALTER TABLE {TABLE} DROP COLUMN {AI_EXTERNAL}",
        f"ALTER TABLE {TABLE} DROP COLUMN {SCOPE}",
        RESET_LOCK_TIMEOUT,
        "COMMIT",
    ]


# ---------------------------------------------------------------------------
# 3. Source invariants
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pure_ddl_no_backfill_and_no_data_statement():
    body = _body()
    for forbidden in ("op.bulk_insert", "conn.execute", "sa.text(", "UPDATE ", "INSERT ", "DELETE ", "SELECT "):
        assert forbidden not in body, f"119 must be pure DDL; found {forbidden!r}"
    assert body.count("op.execute(LOCK_TIMEOUT_SQL)") == body.count("op.execute(RESET_LOCK_TIMEOUT_SQL)") == 2
    assert body.count("op.execute(") == 4
    assert "audit_log" not in body


@pytest.mark.unit
def test_no_table_is_created_so_no_rls_statement_is_needed():
    body = _body()
    assert "op.create_table" not in body
    assert "ROW LEVEL SECURITY" not in body
    assert "REVOKE" not in body and "GRANT" not in body
    for role in ("anon", "authenticated", "service_role"):
        assert not re.search(rf"\b{role}\b", body), role


@pytest.mark.unit
def test_the_lossy_downgrade_is_disclosed():
    docstring = _load_module().__doc__ or ""
    assert "LOSSY" in docstring
    assert "scope <> 'full'" in docstring, "the docstring says how to find the read-only tokens to revoke first"


@pytest.mark.unit
def test_088_is_not_edited():
    """088 is applied: its text still promises no scope column. 119 adds it; 088 stays."""
    with open(os.path.join(VERSIONS_DIR, "088_api_tokens.py")) as fh:
        source_088 = fh.read()
    assert '"scope"' not in source_088
    assert '"ai_external"' not in source_088


# ---------------------------------------------------------------------------
# 4. Model / migration lock-step, DIALECT-COMPILED
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_check_condition_is_byte_identical_on_both_paths():
    from app.models.api_token import API_TOKEN_SCOPE_CHECK_CONDITION, API_TOKEN_SCOPE_CHECK_NAME, ApiTokenScope

    module = _load_module()
    assert module.CHECK_CONDITION == API_TOKEN_SCOPE_CHECK_CONDITION == str(_model_check().sqltext)
    assert module.CHECK_NAME == API_TOKEN_SCOPE_CHECK_NAME == _model_check().name
    # The CHECK admits exactly the enum's values -- no more, no fewer.
    admitted = set(re.findall(r"'([a-z_]+)'", CHECK_CONDITION))
    assert admitted == {member.value for member in ApiTokenScope} == {"full", "read_only"}


@pytest.mark.unit
def test_the_migration_and_the_model_agree_on_both_columns():
    from sqlalchemy.dialects import postgresql, sqlite

    recorder = _record("upgrade", dialect="postgresql", scope=False, ai_external=False, check=False)
    migrated = {call[2].name: call[2] for call in recorder.calls if call[0] == "add_column"}
    table = _model_table()
    for name in (SCOPE, AI_EXTERNAL):
        model = table.columns[name]
        for dialect in (postgresql.dialect(), sqlite.dialect()):
            assert migrated[name].type.compile(dialect=dialect) == model.type.compile(dialect=dialect)
            assert str(sa.schema.CreateColumn(migrated[name]).compile(dialect=dialect)) == str(
                sa.schema.CreateColumn(model).compile(dialect=dialect)
            ), f"{name} renders differently on {dialect.name}"
        assert migrated[name].nullable is model.nullable is False
        assert migrated[name].server_default.arg == model.server_default.arg
        assert not model.index and not model.foreign_keys


@pytest.mark.unit
@pytest.mark.parametrize("dialect_name", ["postgresql", "sqlite"])
def test_the_model_check_compiles_on_both_engines(dialect_name: str):
    from sqlalchemy.dialects import postgresql, sqlite

    dialect = {"postgresql": postgresql.dialect(), "sqlite": sqlite.dialect()}[dialect_name]
    rendered = " ".join(str(sa.schema.CreateTable(_model_table()).compile(dialect=dialect)).split())
    assert f"CONSTRAINT {CHECK_NAME} CHECK ({CHECK_CONDITION})" in rendered
    assert "scope VARCHAR(16) DEFAULT 'full' NOT NULL" in rendered
    assert "ai_external BOOLEAN DEFAULT 'false' NOT NULL" in rendered


@pytest.mark.unit
def test_scope_and_ai_external_are_mint_only_never_on_an_update_schema():
    """Fixed at mint: no update verb exists, and no request schema but the create carries them."""
    import importlib
    import pkgutil

    from pydantic import BaseModel

    import app.schemas as schemas_pkg

    carriers = []
    for info in pkgutil.iter_modules(schemas_pkg.__path__):
        module = importlib.import_module(f"app.schemas.{info.name}")
        for name, obj in vars(module).items():
            if not (isinstance(obj, type) and issubclass(obj, BaseModel)) or obj.__module__ != module.__name__:
                continue
            if not name.startswith("ApiToken") or not name.endswith(("Create", "Update", "Request", "Revoke")):
                continue
            if {SCOPE, AI_EXTERNAL} & set(getattr(obj, "model_fields", {})):
                carriers.append(name)
    assert carriers == ["ApiTokenCreate"]


# ---------------------------------------------------------------------------
# 5. Real round-trip over a create_all-bootstrapped SQLite file
# ---------------------------------------------------------------------------


def _alembic(db_url: str, *args: str) -> None:
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


def _shape(engine) -> tuple:
    """Every column, index, CHECK and FK on api_tokens -- what a batch rebuild must reproduce."""
    inspector = sa.inspect(engine)
    columns = sorted(
        (c["name"], str(c["type"]), bool(c["nullable"]), str(c.get("default"))) for c in inspector.get_columns(TABLE)
    )
    indexes = sorted((ix["name"], tuple(ix["column_names"]), bool(ix["unique"])) for ix in inspector.get_indexes(TABLE))
    checks = sorted((c["name"], " ".join(c["sqltext"].split())) for c in inspector.get_check_constraints(TABLE))
    fks = sorted((tuple(fk["constrained_columns"]), fk["referred_table"]) for fk in inspector.get_foreign_keys(TABLE))
    return columns, indexes, checks, fks


def _column_names(engine) -> set:
    return {c["name"] for c in sa.inspect(engine).get_columns(TABLE)}


@pytest.mark.integration
@pytest.mark.slow
def test_migration_119_round_trip_and_a_pre_119_token_reads_full(tmp_path):
    db_path = tmp_path / "mig119.db"
    db_url = f"sqlite:///{db_path}"

    import app.models  # noqa: F401  (registers every table on Base.metadata)
    from app.db.database import Base

    engine = sa.create_engine(db_url)
    try:
        Base.metadata.create_all(engine)
        bootstrapped = _shape(engine)
        assert {SCOPE, AI_EXTERNAL} <= _column_names(engine)
        assert (CHECK_NAME, CHECK_CONDITION) in bootstrapped[2]

        _alembic(db_url, "stamp", DOWN_REVISION)
        # 1. Over the bootstrapped schema: every guard fires -> clean no-op.
        _alembic(db_url, "upgrade", REVISION)
        assert _shape(engine) == bootstrapped

        # 2. Downgrade: the CHECK and both columns go; everything else is carried across.
        _alembic(db_url, "downgrade", "-1")
        assert not ({SCOPE, AI_EXTERNAL} & _column_names(engine))
        assert not sa.inspect(engine).get_check_constraints(TABLE)

        # A token minted BEFORE 119 (raw INSERT naming only 088's columns; SQLite does not
        # enforce the FKs without PRAGMA foreign_keys, so no parent rows are needed).
        with engine.begin() as conn:
            conn.execute(
                sa.text(
                    "INSERT INTO api_tokens (id, user_id, label, jti, revoked, created_by, created_at, company_id) "
                    "VALUES (7, 1, 'pre-119 bot', 'jti-pre-119', 0, 1, '2026-11-01 00:00:00', 1)"
                )
            )

        # 3. Re-upgrade: the batch rebuild brings back the exact bootstrapped shape...
        _alembic(db_url, "upgrade", REVISION)
        assert _shape(engine) == bootstrapped

        # ...and the pre-119 token reads exactly as it always behaved: full, not AI-external.
        with engine.connect() as conn:
            row = conn.execute(sa.text("SELECT label, jti, scope, ai_external FROM api_tokens WHERE id = 7")).one()
        assert tuple(row)[:3] == ("pre-119 bot", "jti-pre-119", "full")
        # SQLite keeps the quoted DEFAULT 'false' literally (exactly as it does for 088's
        # ``revoked``, whose plain-string default 088's test pins); PostgreSQL casts the same
        # literal to boolean false. Either way: not AI-external.
        assert row.ai_external in (0, "false")

        # 4. The CHECK the migration built refuses a scope no gate knows.
        with pytest.raises(sa.exc.IntegrityError):
            with engine.begin() as conn:
                conn.execute(sa.text("UPDATE api_tokens SET scope = 'admin' WHERE id = 7"))
        with engine.begin() as conn:
            conn.execute(sa.text("UPDATE api_tokens SET scope = 'read_only' WHERE id = 7"))
    finally:
        engine.dispose()
