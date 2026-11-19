"""Coverage for 130_floor_pin (the floor PIN for badge / employee-ID sign-on: five PIN columns on
``users`` and the ``require_floor_pin`` policy switch on ``companies``).

130 is purely additive over two EXISTING tables -- six plain columns, three of them NOT NULL with a
constant server default; no table, no index, no constraint, no data. This file covers the MIGRATION
(the PIN step itself is tested with the auth services and the API):

1. **Script wiring** -- single head, the down_revision, an id that is the file stem and fits
   ``alembic_version``'s varchar(32).
2. **Model / migration lock-step** -- every column (name, type compiled for PostgreSQL,
   nullability, the plain-string server default) is exactly what ``User`` and ``Company`` declare
   and what the PR contract says (the stamped-over hazard), and ``require_floor_pin`` spells its
   default exactly as the ``allow_ai_egress`` / ``allow_sms_egress`` switches do.
3. **A real upgrade -> re-run -> downgrade round-trip** over a disposable SQLite database with
   ``PRAGMA foreign_keys = ON``, legacy rows, ``ON DELETE CASCADE`` and NO ACTION children of both
   tables, a self-referencing FK and a trigger whose body reads both tables: both directions are
   SQLite's native ``ALTER`` (each table keeps its b-tree root page -- never rebuilt, so no child is
   cascade-deleted), nothing is backfilled (no user has a PIN, the counter reads 0, the password
   lockout is untouched), a pre-existing company reads ``require_floor_pin`` False through the
   model's Boolean type (the SQLite-only storage normalization re-spells SQLite's TEXT ``'false'``
   as ``0``), a pre-130 insert still lands, a partially built schema recovers each missing column
   independently, and the downgrade removes exactly what the upgrade added.
4. **The create_all bootstrap is a no-op** -- over a schema the models already built, the upgrade
   emits nothing.
5. **The SQL PostgreSQL receives** -- offline (``--sql``) rendering of both directions: the
   lock_timeout taken LOCAL and handed back, the metadata-only ``ADD COLUMN``\\s, ``users`` before
   ``companies`` up and the reverse down, no backfill, no index / constraint and no RLS / role
   statement; and the models' own PostgreSQL DDL carries the same columns.
"""

import importlib.util
import io
import os
import re

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

import app.models  # noqa: F401  -- registers every table the two models reference
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from app.models.company import Company
from app.models.user import User

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIGRATION_FILE = os.path.join(BACKEND_DIR, "alembic", "versions", "130_floor_pin.py")
REVISION = "130_floor_pin"
DOWN_REVISION = "129_owned_property"

pytestmark = pytest.mark.unit

MODELS = {"users": User, "companies": Company}
TABLES = tuple(MODELS)

# The PR contract, copied here independently of both the migration and the models:
# name -> (PostgreSQL type, nullable, server default, Python default). A server default is either
# a plain string (spelled like 054's ``allow_ai_egress``) or ``FALSE_ELEMENT``: ``sa.false()``,
# which the later boolean (``floor_pin_reset_required``) uses on model and migration alike
# (``DEFAULT false`` on PostgreSQL, ``DEFAULT 0`` on SQLite).
TIMESTAMP = "TIMESTAMP WITHOUT TIME ZONE"
FALSE_ELEMENT = "sa.false()"
CONTRACT = {
    "users": {
        "floor_pin_hash": ("VARCHAR(255)", True, None, None),
        "floor_pin_set_at": (TIMESTAMP, True, None, None),
        "floor_pin_failed_attempts": ("INTEGER", False, "0", 0),
        "floor_pin_locked_until": (TIMESTAMP, True, None, None),
        "floor_pin_reset_required": ("BOOLEAN", False, FALSE_ELEMENT, False),
    },
    "companies": {
        "require_floor_pin": ("BOOLEAN", False, "false", False),
    },
}
NEW_COLUMNS = {table: tuple(columns) for table, columns in CONTRACT.items()}


def _is_false_element(arg) -> bool:
    return isinstance(arg, sa.sql.elements.False_)


def _module():
    spec = importlib.util.spec_from_file_location("_migtest_130", MIGRATION_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _script_directory() -> ScriptDirectory:
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))
    return ScriptDirectory.from_config(cfg)


def _pg(type_) -> str:
    return type_.compile(dialect=postgresql.dialect())


def _migration_columns(module, table):
    change = next(change for change in module.TABLE_CHANGES if change.table == table)
    return {column.name: column for column in change.columns()}


# ---------------------------------------------------------------------------- 1. wiring


def test_single_head_and_revision_chain():
    scripts = _script_directory()
    assert len(scripts.get_heads()) == 1, scripts.get_heads()
    assert scripts.get_revision(REVISION).down_revision == DOWN_REVISION


def test_revision_id_is_the_file_stem_and_fits_alembic_version():
    module = _module()
    assert module.revision == REVISION == os.path.basename(MIGRATION_FILE)[:-3]
    assert module.down_revision == DOWN_REVISION
    assert len(REVISION) <= 32


# ------------------------------------------------------------------- 2. model lock-step


def test_the_migration_declares_the_contract():
    module = _module()
    # users first, then companies: the lock order the docstring states.
    assert [change.table for change in module.TABLE_CHANGES] == list(TABLES)
    assert module.USER_COLUMN_NAMES == NEW_COLUMNS["users"]
    assert module.COMPANY_COLUMN_NAMES == NEW_COLUMNS["companies"]
    for table in TABLES:
        columns = _migration_columns(module, table)
        assert tuple(columns) == NEW_COLUMNS[table], table
        for name, (pg_type, nullable, server_default, _python_default) in CONTRACT[table].items():
            column = columns[name]
            assert _pg(column.type) == pg_type, name
            assert column.nullable is nullable, name
            if server_default is None:
                assert column.server_default is None, name
            elif server_default == FALSE_ELEMENT:
                assert _is_false_element(column.server_default.arg), name
            else:
                # A plain string, never sa.text: the fresh-bootstrap diff compares the rendered DEFAULT.
                assert isinstance(column.server_default.arg, str), name
                assert column.server_default.arg == server_default, name
            assert not column.index and not column.unique and not column.foreign_keys, name
    # Native ALTER on SQLite: never a copy-and-rename rebuild of users / companies.
    assert module.BATCH_RECREATE == "never"


@pytest.mark.parametrize("table", TABLES)
def test_every_column_matches_the_model(table):
    module = _module()
    model_table = MODELS[table].__table__
    for name, column in _migration_columns(module, table).items():
        model_column = model_table.c[name]
        assert _pg(model_column.type) == _pg(column.type), name
        assert model_column.nullable == column.nullable, name
        if column.server_default is None:
            assert model_column.server_default is None, name
        elif _is_false_element(column.server_default.arg):
            assert _is_false_element(model_column.server_default.arg), name
        else:
            assert isinstance(model_column.server_default.arg, str), name
            assert model_column.server_default.arg == column.server_default.arg, name
        python_default = CONTRACT[table][name][3]
        if python_default is None:
            assert model_column.default is None, name
        else:
            assert model_column.default is not None and model_column.default.arg == python_default, name
        assert not model_column.foreign_keys and not model_column.unique, name


@pytest.mark.parametrize("table", TABLES)
def test_the_model_indexes_none_of_the_new_columns(table):
    for index in MODELS[table].__table__.indexes:
        assert not set(NEW_COLUMNS[table]) & {column.name for column in index.columns}, index.name


def test_require_floor_pin_is_spelled_like_the_egress_switches():
    """Model and migration compile identically only if the default is the same plain string the
    other company switches use (054's ``server_default="false"``), not ``sa.text("false")``."""
    columns = Company.__table__.c
    for switch in ("allow_ai_egress", "allow_sms_egress", "require_floor_pin"):
        assert columns[switch].server_default.arg == "false", switch
        assert _pg(columns[switch].type) == "BOOLEAN" and columns[switch].nullable is False, switch
    migrated = _migration_columns(_module(), "companies")["require_floor_pin"]
    assert migrated.server_default.arg == columns["allow_ai_egress"].server_default.arg


def test_the_pin_counters_are_separate_from_the_password_lockout():
    columns = User.__table__.c
    # The password lockout columns are still there and are not what 130 adds.
    assert {"failed_login_attempts", "locked_until"} <= set(columns.keys())
    assert not {"failed_login_attempts", "locked_until"} & set(NEW_COLUMNS["users"])


# ------------------------------------------------------------- 3. real SQLite round-trip

# Synthetic names only: the repository is public.
LEGACY_DDL = [
    (
        "CREATE TABLE companies (id INTEGER NOT NULL, name VARCHAR(255) NOT NULL, slug VARCHAR(100) NOT NULL, "
        "is_active BOOLEAN NOT NULL, allow_ai_egress BOOLEAN DEFAULT 'false' NOT NULL, "
        "allow_sms_egress BOOLEAN DEFAULT 'false' NOT NULL, parent_company_id INTEGER, PRIMARY KEY (id), "
        "FOREIGN KEY(parent_company_id) REFERENCES companies (id))"
    ),
    "CREATE UNIQUE INDEX ix_companies_slug ON companies (slug)",
    (
        "CREATE TABLE users (id INTEGER NOT NULL, company_id INTEGER NOT NULL, employee_id VARCHAR(50) NOT NULL, "
        "email VARCHAR(255) NOT NULL, hashed_password VARCHAR(255) NOT NULL, is_active BOOLEAN, "
        "failed_login_attempts INTEGER, locked_until DATETIME, created_by INTEGER, PRIMARY KEY (id), "
        "CONSTRAINT uq_users_company_email UNIQUE (company_id, email), "
        "CONSTRAINT uq_users_company_employee_id UNIQUE (company_id, employee_id), "
        "FOREIGN KEY(company_id) REFERENCES companies (id), "
        "CONSTRAINT fk_users_created_by FOREIGN KEY(created_by) REFERENCES users (id) ON DELETE SET NULL)"
    ),
    "CREATE INDEX ix_users_company_active ON users (company_id, is_active)",
    "CREATE INDEX ix_users_employee_id ON users (employee_id)",
    # Children: a rebuild's DROP TABLE under foreign_keys = ON would cascade-delete the first and
    # abort on the second. The native ALTER leaves both alone.
    (
        "CREATE TABLE user_workspaces (id INTEGER PRIMARY KEY, "
        "company_id INTEGER NOT NULL REFERENCES companies (id) ON DELETE CASCADE, "
        "user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE)"
    ),
    (
        "CREATE TABLE work_orders (id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL REFERENCES companies (id), "
        "created_by INTEGER REFERENCES users (id))"
    ),
    # No real trigger reads users or companies today; this one proves the native ALTER would keep
    # one resolving, without 129's legacy_alter_table wrapper.
    "CREATE TABLE badge_scans (id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL, user_id INTEGER NOT NULL)",
    (
        "CREATE TRIGGER tr_badge_scan_insert BEFORE INSERT ON badge_scans\n"
        "BEGIN\n"
        " SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM users AS u JOIN companies AS c ON c.id = u.company_id\n"
        " WHERE u.id=NEW.user_id AND c.id=NEW.company_id)\n"
        " THEN RAISE(ABORT, 'Badge scan must name a tenant user.') END;\n"
        "END"
    ),
    "INSERT INTO companies VALUES (1, 'Synthetic Co', 'synthetic', 1, 1, 0, NULL)",
    "INSERT INTO companies VALUES (2, 'Other tenant', 'other', 0, 0, 0, 1)",
    "INSERT INTO users VALUES (5, 1, '1001', 'op@example.test', 'x', 1, 0, NULL, NULL)",
    # A user locked out of PASSWORD login: 130 must not touch that lockout.
    "INSERT INTO users VALUES (6, 1, '1002', 'locked@example.test', 'x', 1, 5, '2026-11-19 12:00:00', 5)",
    "INSERT INTO users VALUES (7, 2, '2001', 'other@example.test', 'x', 0, 0, NULL, NULL)",
    "INSERT INTO user_workspaces VALUES (1, 1, 5), (2, 1, 6), (3, 2, 7)",
    "INSERT INTO work_orders VALUES (1, 1, 5), (2, 2, 7)",
    "INSERT INTO badge_scans VALUES (1, 1, 5)",
]

LEGACY_ROWS_SQL = {
    "companies": "SELECT id, name, slug, is_active, allow_ai_egress, allow_sms_egress, parent_company_id "
    "FROM companies ORDER BY id",
    "users": "SELECT id, company_id, employee_id, email, hashed_password, is_active, failed_login_attempts, "
    "locked_until, created_by FROM users ORDER BY id",
    "user_workspaces": "SELECT id, company_id, user_id FROM user_workspaces ORDER BY id",
    "work_orders": "SELECT id, company_id, created_by FROM work_orders ORDER BY id",
    "badge_scans": "SELECT id, company_id, user_id FROM badge_scans ORDER BY id",
}
LEGACY_ROWS = {
    "companies": [(1, "Synthetic Co", "synthetic", 1, 1, 0, None), (2, "Other tenant", "other", 0, 0, 0, 1)],
    "users": [
        (5, 1, "1001", "op@example.test", "x", 1, 0, None, None),
        (6, 1, "1002", "locked@example.test", "x", 1, 5, "2026-11-19 12:00:00", 5),
        (7, 2, "2001", "other@example.test", "x", 0, 0, None, None),
    ],
    "user_workspaces": [(1, 1, 5), (2, 1, 6), (3, 2, 7)],
    "work_orders": [(1, 1, 5), (2, 2, 7)],
    "badge_scans": [(1, 1, 5)],
}
USER_PIN_SQL = (
    "SELECT floor_pin_hash, floor_pin_set_at, floor_pin_failed_attempts, floor_pin_locked_until, "
    "floor_pin_reset_required FROM users ORDER BY id"
)
NO_PIN = (None, None, 0, None, 0)


def _policy_through_the_model(connection):
    """Each company's ``require_floor_pin`` read through the MODEL's Boolean type, as the ORM reads it."""
    table = Company.__table__
    rows = connection.execute(sa.select(table.c.id, table.c.require_floor_pin).order_by(table.c.id))
    return [(row[0], row[1]) for row in rows]


def _enable_foreign_keys(dbapi_connection, _record):
    dbapi_connection.execute("PRAGMA foreign_keys = ON")


@pytest.fixture
def scratch(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm130.db'}")
    sa.event.listen(engine, "connect", _enable_foreign_keys)
    with engine.begin() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        for statement in LEGACY_DDL:
            connection.exec_driver_sql(statement)
        migration = _module()
        migration.op = Operations(MigrationContext.configure(connection))
        yield connection, migration
    engine.dispose()


def _objects(connection, table):
    inspector = sa.inspect(connection)
    return {
        "columns": {
            column["name"]: (str(column["type"]), column["nullable"], column["default"])
            for column in inspector.get_columns(table)
        },
        "fks": sorted(
            (
                fk["name"] or "",
                tuple(fk["constrained_columns"]),
                fk["referred_table"],
                tuple(fk["referred_columns"]),
                tuple(sorted((fk.get("options") or {}).items())),
            )
            for fk in inspector.get_foreign_keys(table)
        ),
        "checks": {ck["name"]: ck["sqltext"] for ck in inspector.get_check_constraints(table)},
        "indexes": {ix["name"]: (tuple(ix["column_names"]), bool(ix["unique"])) for ix in inspector.get_indexes(table)},
        "uniques": {uq["name"]: tuple(uq["column_names"]) for uq in inspector.get_unique_constraints(table)},
    }


def _root_pages(connection):
    """Each table's b-tree root page: a copy-and-rename rebuild gives the table a new one."""
    rows = connection.exec_driver_sql("SELECT name, rootpage FROM sqlite_master WHERE type = 'table'")
    return dict(rows.fetchall())


def _assert_legacy_rows_intact(connection):
    for table, sql in LEGACY_ROWS_SQL.items():
        assert [tuple(row) for row in connection.exec_driver_sql(sql)] == LEGACY_ROWS[table], table
    triggers = connection.exec_driver_sql("SELECT name, tbl_name FROM sqlite_master WHERE type = 'trigger'")
    assert [tuple(row) for row in triggers] == [("tr_badge_scan_insert", "badge_scans")]
    assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
    # 130 never needs 129's wrapper, so it never touches the connection's setting.
    assert connection.exec_driver_sql("PRAGMA legacy_alter_table").scalar() == 0


def _assert_the_trigger_resolves_both_tables(connection, scan_id):
    connection.exec_driver_sql(f"INSERT INTO badge_scans VALUES ({scan_id}, 1, 6)")
    for company_id, user_id in ((1, 999), (2, 5)):  # an unknown user; a user of another tenant
        nested = connection.begin_nested()
        with pytest.raises(sa.exc.IntegrityError, match="Badge scan must name a tenant user"):
            connection.exec_driver_sql(f"INSERT INTO badge_scans VALUES ({scan_id + 1}, {company_id}, {user_id})")
        nested.rollback()
    connection.exec_driver_sql(f"DELETE FROM badge_scans WHERE id = {scan_id}")


def test_upgrade_adds_every_column_natively_and_backfills_nothing(scratch):
    connection, migration = scratch
    legacy = {table: _objects(connection, table) for table in TABLES}
    pages = _root_pages(connection)
    migration.upgrade()

    for table in TABLES:
        objects, before = _objects(connection, table), legacy[table]
        assert set(objects["columns"]) == set(before["columns"]) | set(NEW_COLUMNS[table]), table
        for name in before["columns"]:
            assert objects["columns"][name] == before["columns"][name], name
        # Nothing but columns: every FK, CHECK, index and unique is exactly what was there.
        for kind in ("fks", "checks", "indexes", "uniques"):
            assert objects[kind] == before[kind], (table, kind)
    reflected = {**_objects(connection, "users")["columns"], **_objects(connection, "companies")["columns"]}
    assert reflected["floor_pin_hash"] == ("VARCHAR(255)", True, None)
    assert reflected["floor_pin_set_at"] == ("DATETIME", True, None)
    assert reflected["floor_pin_failed_attempts"] == ("INTEGER", False, "'0'")
    assert reflected["floor_pin_locked_until"] == ("DATETIME", True, None)
    # sa.false() renders DEFAULT 0 on SQLite: an integer, so no TEXT-'false' artifact to normalize.
    assert reflected["floor_pin_reset_required"] == ("BOOLEAN", False, "0")
    assert reflected["require_floor_pin"] == ("BOOLEAN", False, "'false'")
    # Never rebuilt: the same b-tree, so no child row was cascade-deleted along the way.
    assert _root_pages(connection) == pages
    _assert_legacy_rows_intact(connection)

    # No backfill: no user has a PIN (the password-locked user included -- the two lockouts are
    # separate), and every company reads the server default.
    assert [tuple(row) for row in connection.exec_driver_sql(USER_PIN_SQL)] == [NO_PIN] * 3
    # SQLite's ADD COLUMN leaves a pre-existing row's DEFAULT 'false' as that TEXT, which the
    # model's Boolean reads as True; the SQLite-only normalization re-spells it as the integer 0
    # (the same false -- a storage fix, not a backfill), so every pre-existing company reads OFF.
    companies = connection.exec_driver_sql("SELECT require_floor_pin FROM companies ORDER BY id")
    assert [row[0] for row in companies] == [0, 0]
    assert _policy_through_the_model(connection) == [(1, False), (2, False)]
    # Only the new column is re-spelled: the older egress switches keep what they stored.
    _assert_legacy_rows_intact(connection)


def test_without_the_normalization_sqlite_would_read_every_company_as_requiring_a_pin(scratch):
    """The reason the SQLite-only UPDATE exists: SQLite's own ADD COLUMN stores TEXT 'false' on a
    pre-existing row, and the model's Boolean reads that as True -- every badge user without a
    PIN would be refused on a local dev database. Proven on the raw ALTER the migration wraps."""
    connection, _migration = scratch
    connection.exec_driver_sql("ALTER TABLE companies ADD COLUMN require_floor_pin BOOLEAN DEFAULT 'false' NOT NULL")
    assert _policy_through_the_model(connection) == [(1, True), (2, True)]
    connection.exec_driver_sql(_module().SQLITE_NORMALIZE_REQUIRE_FLOOR_PIN_SQL)
    assert _policy_through_the_model(connection) == [(1, False), (2, False)]


def test_a_pre_130_insert_still_lands_with_no_pin_and_the_policy_off(scratch):
    """Old code never names the columns: the server defaults satisfy both NOT NULLs -- which is
    what makes a stamp rollback safe for inserts."""
    connection, migration = scratch
    migration.upgrade()
    connection.exec_driver_sql("INSERT INTO companies (id, name, slug, is_active) VALUES (3, 'New Co', 'new', 1)")
    connection.exec_driver_sql(
        "INSERT INTO users (id, company_id, employee_id, email, hashed_password) VALUES (8, 3, '3001', 'n@x.test', 'x')"
    )
    assert connection.exec_driver_sql(USER_PIN_SQL).fetchall()[-1] == NO_PIN
    flag = connection.exec_driver_sql("SELECT require_floor_pin FROM companies WHERE id = 3").scalar()
    assert flag == "false"
    # An explicit NULL is refused: the NOT NULLs are real.
    for statement in (
        "UPDATE users SET floor_pin_failed_attempts = NULL WHERE id = 5",
        "UPDATE companies SET require_floor_pin = NULL WHERE id = 1",
    ):
        nested = connection.begin_nested()
        with pytest.raises(sa.exc.IntegrityError):
            connection.exec_driver_sql(statement)
        nested.rollback()


def test_rerun_is_a_no_op_and_a_partial_schema_recovers_each_column(scratch):
    connection, migration = scratch
    migration.upgrade()
    connection.exec_driver_sql(
        "UPDATE users SET floor_pin_hash = 'bcrypt-hash', floor_pin_set_at = '2026-11-19 08:00:00', "
        "floor_pin_failed_attempts = 2 WHERE id = 5"
    )
    connection.exec_driver_sql("UPDATE companies SET require_floor_pin = 1 WHERE id = 1")
    full = {table: _objects(connection, table) for table in TABLES}
    migration.upgrade()  # a re-run is a guarded no-op
    assert {table: _objects(connection, table) for table in TABLES} == full
    # ...the SQLite normalization included: it runs only when the column is added, so a company
    # an Admin turned ON keeps its policy.
    assert _policy_through_the_model(connection) == [(1, True), (2, False)]

    # Knock out one column on each table and prove each guard restores only what is missing.
    connection.exec_driver_sql("ALTER TABLE users DROP COLUMN floor_pin_locked_until")
    connection.exec_driver_sql("ALTER TABLE companies DROP COLUMN require_floor_pin")
    assert "floor_pin_locked_until" not in _objects(connection, "users")["columns"]
    migration.upgrade()
    assert {table: _objects(connection, table) for table in TABLES} == full
    # Values recorded in the surviving columns are kept; nothing is overwritten or re-derived.
    assert connection.exec_driver_sql(USER_PIN_SQL).fetchall()[0] == (
        "bcrypt-hash",
        "2026-11-19 08:00:00",
        2,
        None,
        0,
    )
    _assert_legacy_rows_intact(connection)


def test_downgrade_removes_exactly_what_upgrade_added_and_replays(scratch):
    connection, migration = scratch
    legacy = {table: _objects(connection, table) for table in TABLES}
    pages = _root_pages(connection)
    migration.upgrade()
    connection.exec_driver_sql(
        "UPDATE users SET floor_pin_hash = 'bcrypt-hash', floor_pin_locked_until = '2026-11-19 09:00:00' WHERE id = 6"
    )
    connection.exec_driver_sql("UPDATE companies SET require_floor_pin = 1 WHERE id = 1")
    migration.downgrade()

    for table in TABLES:
        assert _objects(connection, table) == legacy[table], table
    assert _root_pages(connection) == pages  # the native DROP COLUMN rewrote no table either
    _assert_legacy_rows_intact(connection)
    migration.downgrade()  # a second downgrade is a guarded no-op, not an error
    for table in TABLES:
        assert _objects(connection, table) == legacy[table], table

    migration.upgrade()  # the chain replays forward cleanly...
    # ...and the downgrade was LOSSY, as the docstring warns: the PIN, its lockout and the policy
    # are gone. A code rollback stamps 129 instead.
    assert [tuple(row) for row in connection.exec_driver_sql(USER_PIN_SQL)] == [NO_PIN] * 3
    companies = connection.exec_driver_sql("SELECT require_floor_pin FROM companies ORDER BY id")
    assert [row[0] for row in companies] == [0, 0]
    assert _policy_through_the_model(connection) == [(1, False), (2, False)]
    _assert_legacy_rows_intact(connection)


def test_a_trigger_that_reads_both_tables_keeps_resolving(scratch):
    connection, migration = scratch
    migration.upgrade()
    _assert_the_trigger_resolves_both_tables(connection, 10)
    migration.downgrade()
    _assert_the_trigger_resolves_both_tables(connection, 20)
    _assert_legacy_rows_intact(connection)


def test_no_model_trigger_or_view_reads_users_or_companies(tmp_path):
    """The reason 130 needs no ``PRAGMA legacy_alter_table`` wrapper is twofold: it never rebuilds,
    and no trigger built from the models names either table. Pin the second half so a future
    trigger is a decision, not a surprise."""
    from app.db.database import Base

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'models.db'}")
    with engine.begin() as connection:
        Base.metadata.create_all(connection)
        stored = connection.exec_driver_sql(
            "SELECT type, name, tbl_name, sql FROM sqlite_master WHERE type IN ('trigger', 'view')"
        ).fetchall()
    engine.dispose()
    assert stored, "create_all builds the model triggers"
    for kind, name, table, sql in stored:
        assert table not in TABLES, name
        assert not re.search(r"\b(users|companies)\b", sql or ""), f"{kind} {name} reads users / companies"


# -------------------------------------------------------- 4. create_all bootstrap no-op


class _Recorder:
    def __init__(self, bind):
        self._bind = bind
        self.calls = []

    def get_bind(self):
        return self._bind

    def get_context(self):
        return MigrationContext.configure(self._bind)

    def __getattr__(self, name):  # any DDL call is recorded instead of run
        def record(*args, **kwargs):
            self.calls.append(name)

        return record


def test_upgrade_over_a_create_all_schema_emits_nothing(tmp_path):
    from app.db.database import Base

    module = _module()
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'bootstrap.db'}")
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys = OFF")
        Base.metadata.create_all(conn, tables=[Base.metadata.tables[name] for name in TABLES])
        recorder = _Recorder(conn)
        module.op = recorder
        module.upgrade()
    engine.dispose()
    assert recorder.calls == []


# ------------------------------------------------------------- 5. offline PostgreSQL SQL


def _render(function_name: str, dialect_name: str = "postgresql"):
    module = _module()
    buffer = io.StringIO()
    context = MigrationContext.configure(dialect_name=dialect_name, opts={"as_sql": True, "output_buffer": buffer})
    module.op = Operations(context)
    with context.begin_transaction():
        getattr(module, function_name)()
    statements = [" ".join(statement.split()) for statement in buffer.getvalue().split(";") if statement.strip()]
    return [statement for statement in statements if statement not in ("BEGIN", "COMMIT")]


EXPECTED_ADD_COLUMNS = [
    # Metadata-only on PostgreSQL 11+: nullable with no default, or a constant default; no rewrite.
    "ALTER TABLE users ADD COLUMN floor_pin_hash VARCHAR(255)",
    f"ALTER TABLE users ADD COLUMN floor_pin_set_at {TIMESTAMP}",
    "ALTER TABLE users ADD COLUMN floor_pin_failed_attempts INTEGER DEFAULT '0' NOT NULL",
    f"ALTER TABLE users ADD COLUMN floor_pin_locked_until {TIMESTAMP}",
    "ALTER TABLE users ADD COLUMN floor_pin_reset_required BOOLEAN DEFAULT false NOT NULL",
    "ALTER TABLE companies ADD COLUMN require_floor_pin BOOLEAN DEFAULT 'false' NOT NULL",
]


def _assert_pure_column_ddl(sql: str):
    assert not re.search(r"\b(UPDATE|INSERT|DELETE)\b", sql), "130 is pure DDL: no backfill"
    assert not re.search(r"\b(INDEX|CONSTRAINT|FOREIGN KEY|CHECK|UNIQUE|TRIGGER)\b", sql)
    assert not re.search(r"ROW LEVEL SECURITY|REVOKE|GRANT|\banon\b|authenticated|service_role", sql)
    assert "audit_log" not in sql and "CREATE TABLE" not in sql and "DROP TABLE" not in sql
    # The password lockout (failed_login_attempts / locked_until) is never touched.
    assert not re.search(r"\b(failed_login_attempts|locked_until)\b", sql)


def test_offline_upgrade_renders_the_canonical_postgres_ddl():
    statements = _render("upgrade")
    # Exactly these, in this order: the timeout, users' five columns, companies' one, the reset.
    assert statements == ["SET LOCAL lock_timeout = '5s'", *EXPECTED_ADD_COLUMNS, "SET LOCAL lock_timeout = DEFAULT"]
    _assert_pure_column_ddl("\n".join(statements))


def test_only_sqlite_gets_the_storage_normalization_right_after_the_column():
    """PostgreSQL receives no UPDATE (the canonical render above is pure DDL); SQLite receives the
    one storage-normalization statement directly after ``require_floor_pin`` is added."""
    statements = _render("upgrade", dialect_name="sqlite")
    normalize = _module().SQLITE_NORMALIZE_REQUIRE_FLOOR_PIN_SQL
    assert normalize == "UPDATE companies SET require_floor_pin = 0 WHERE require_floor_pin = 'false'"
    assert [statement for statement in statements if statement.startswith("UPDATE")] == [normalize]
    added = next(i for i, statement in enumerate(statements) if "ADD COLUMN require_floor_pin" in statement)
    assert statements[added + 1] == normalize
    assert not any("lock_timeout" in statement for statement in statements)  # PostgreSQL only
    assert not any("UPDATE" in statement for statement in _render("upgrade"))


def test_offline_downgrade_drops_exactly_what_upgrade_added_in_reverse():
    statements = _render("downgrade")
    assert statements == [
        "SET LOCAL lock_timeout = '5s'",
        # Reverse of the upgrade: companies first, then users, each table's columns reversed.
        "ALTER TABLE companies DROP COLUMN require_floor_pin",
        "ALTER TABLE users DROP COLUMN floor_pin_reset_required",
        "ALTER TABLE users DROP COLUMN floor_pin_locked_until",
        "ALTER TABLE users DROP COLUMN floor_pin_failed_attempts",
        "ALTER TABLE users DROP COLUMN floor_pin_set_at",
        "ALTER TABLE users DROP COLUMN floor_pin_hash",
        "SET LOCAL lock_timeout = DEFAULT",
    ]
    _assert_pure_column_ddl("\n".join(statements))


@pytest.mark.parametrize("table", TABLES)
def test_create_all_ddl_carries_the_same_columns_on_postgres(table):
    statements = []
    engine = sa.create_mock_engine(
        "postgresql://", lambda statement, *a, **k: statements.append(str(statement.compile(dialect=engine.dialect)))
    )
    MODELS[table].__table__.create(engine, checkfirst=False)
    ddl = " ".join(" ".join(statements).split())
    for statement in EXPECTED_ADD_COLUMNS:
        prefix = f"ALTER TABLE {table} ADD COLUMN "
        if statement.startswith(prefix):
            column_ddl = statement[len(prefix) :]
            # The model renders the identical column clause inside its CREATE TABLE.
            assert re.search(rf"[(,] {re.escape(column_ddl)} ?[,)]", ddl), column_ddl
