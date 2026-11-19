"""Coverage for 131_document_access (who may open a document: ``documents.access_level`` with its
CHECK and ``(company_id, access_level)`` index, and the per-user export authorization
``users.export_authorized`` / ``_at`` / ``_by`` with the named self-FK).

131 is purely additive over two EXISTING tables -- four columns, one FK, one CHECK, one index; no
table, no data. This file covers the MIGRATION (the access rule itself is tested with
``app/services/document_access.py`` and the API):

1. **Script wiring** -- single head, the down_revision, an id that is the file stem and fits
   ``alembic_version``'s varchar(32).
2. **Model / migration lock-step** -- every column (name, type compiled for PostgreSQL,
   nullability, the server default), the FK (name, target, no ``ON DELETE``), the CHECK
   text (byte-identical to the model's ``DOCUMENT_ACCESS_LEVEL_CHECK``, to the PR contract and to
   ``DocumentAccessLevel``) and the index are exactly what ``Document`` and ``User`` declare (the
   stamped-over hazard), and ``export_authorized``'s default is ``sa.false()`` on both, which fails
   CLOSED on SQLite (``0``), unlike the plain string ``"false"`` (TEXT the ORM reads as True).
3. **A real upgrade -> re-run -> downgrade round-trip** over a disposable SQLite database with
   foreign keys OFF (SQLite's default, and the app's and ``alembic/env.py``'s), legacy rows, an
   ``ON DELETE CASCADE`` and ``SET NULL`` child of ``users``, NO ACTION children of ``documents``,
   both self-referencing FKs and a trigger whose body reads both tables: the SQLite rebuilds carry
   every row, child, legacy column / index / constraint and the trigger across in both directions;
   nothing is backfilled (every document reads ``'standard'``, no user is authorized -- read
   through the model's Boolean, which only the SQLite storage normalization makes true); the CHECK
   and the NOT NULLs are real; a pre-131 insert still lands; a partially built schema recovers each
   missing object independently (a plain column alone without any rebuild); the downgrade removes
   exactly what the upgrade added.
4. **Foreign-key enforcement** -- with ``PRAGMA foreign_keys = ON`` a rebuild's ``DROP TABLE`` really
   would cascade-delete and null ``users``' children, so both directions refuse before any DDL and
   change nothing.
5. **The create_all bootstrap is a no-op** -- over a schema the models already built, the upgrade
   emits nothing.
6. **The SQL PostgreSQL receives** -- offline (``--sql``) rendering of both directions: the
   lock_timeout taken LOCAL and handed back, the metadata-only ``ADD COLUMN``\\s, the FK, CHECK and
   index, ``users`` before ``documents`` up and the reverse down, no backfill, no ``ON DELETE`` and
   no RLS / role statement; and the models' own PostgreSQL DDL carries the same objects.
"""

import importlib.util
import io
import os
import re
from contextlib import contextmanager

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

import app.models  # noqa: F401  -- registers every table the two models reference
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from app.models.document import DOCUMENT_ACCESS_LEVEL_CHECK, Document, DocumentAccessLevel
from app.models.user import User

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIGRATION_FILE = os.path.join(BACKEND_DIR, "alembic", "versions", "131_document_access.py")
REVISION = "131_document_access"
DOWN_REVISION = "130_floor_pin"

pytestmark = pytest.mark.unit

MODELS = {"users": User, "documents": Document}
TABLES = tuple(MODELS)

# The PR contract, copied here independently of both the migration and the models:
# name -> (PostgreSQL type, nullable, server default, Python default). A server default is the
# plain string the column declares, or SA_FALSE for an ``sa.false()`` expression.
TIMESTAMP = "TIMESTAMP WITHOUT TIME ZONE"
SA_FALSE = "sa.false()"
CONTRACT = {
    "users": {
        "export_authorized": ("BOOLEAN", False, SA_FALSE, False),
        "export_authorized_at": (TIMESTAMP, True, None, None),
        "export_authorized_by": ("INTEGER", True, None, None),
    },
    "documents": {
        "access_level": ("VARCHAR(30)", False, "standard", "standard"),
    },
}
NEW_COLUMNS = {table: tuple(columns) for table, columns in CONTRACT.items()}
CONTRACT_FK = ("fk_users_export_authorized_by", "users", "export_authorized_by")
CONTRACT_CHECK = ("ck_documents_access_level", "access_level IN ('standard', 'restricted', 'export_controlled')")
CONTRACT_INDEX = ("ix_documents_access_level", ("company_id", "access_level"))
NEW_OBJECTS = {
    "users": {"fks": {CONTRACT_FK[0]}, "checks": set(), "indexes": set()},
    "documents": {"fks": set(), "checks": {CONTRACT_CHECK[0]}, "indexes": {CONTRACT_INDEX[0]}},
}


def _module():
    spec = importlib.util.spec_from_file_location("_migtest_131", MIGRATION_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _script_directory() -> ScriptDirectory:
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))
    return ScriptDirectory.from_config(cfg)


def _pg(type_) -> str:
    return type_.compile(dialect=postgresql.dialect())


def _ws(value) -> str:
    return re.sub(r"\s+", " ", str(value)).strip()


def _change(module, table):
    return next(change for change in module.TABLE_CHANGES if change.table == table)


def _migration_columns(module, table):
    return {column.name: column for column in _change(module, table).columns()}


def _default_spelling(column):
    """A column's server default as declared: its plain string, or SA_FALSE for ``sa.false()``.

    Anything else (``sa.text``, ``sa.true()``, a function) fails: the fresh-bootstrap diff compares
    the rendered DEFAULT, so model and migration must declare the identical form.
    """
    arg = column.server_default.arg
    if isinstance(arg, str):
        return arg
    assert isinstance(arg, sa.sql.elements.False_), (column.name, arg)
    return SA_FALSE


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
    # users first, then documents: the lock order the docstring states.
    assert [change.table for change in module.TABLE_CHANGES] == list(TABLES)
    assert module.USER_COLUMN_NAMES == NEW_COLUMNS["users"]
    assert module.DOCUMENT_COLUMN_NAMES == NEW_COLUMNS["documents"]
    for table in TABLES:
        columns = _migration_columns(module, table)
        assert tuple(columns) == NEW_COLUMNS[table], table
        for name, (pg_type, nullable, server_default, _python_default) in CONTRACT[table].items():
            column = columns[name]
            assert _pg(column.type) == pg_type, name
            assert column.nullable is nullable, name
            if server_default is None:
                assert column.server_default is None, name
            else:
                # A plain string or sa.false(), never sa.text: the fresh-bootstrap diff compares the
                # rendered DEFAULT.
                assert _default_spelling(column) == server_default, name
            # The FK is a separate named constraint; a ForeignKey on the Column would make the
            # SQLite native ADD COLUMN path attempt an ALTER ADD CONSTRAINT.
            assert not column.index and not column.unique and not column.foreign_keys, name
    assert _change(module, "users").foreign_keys == (CONTRACT_FK,)
    assert _change(module, "users").checks == () and _change(module, "users").index is None
    assert _change(module, "documents").foreign_keys == ()
    assert _change(module, "documents").checks == (CONTRACT_CHECK,)
    assert _change(module, "documents").index == CONTRACT_INDEX


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
        else:
            assert _default_spelling(model_column) == _default_spelling(column), name
        python_default = CONTRACT[table][name][3]
        if python_default is None:
            assert model_column.default is None, name
        else:
            assert model_column.default is not None and model_column.default.arg == python_default, name
        assert not model_column.unique, name


def test_the_foreign_key_matches_the_model():
    name, referred, local = CONTRACT_FK
    fks = list(User.__table__.c[local].foreign_keys)
    assert len(fks) == 1
    fk = fks[0]
    assert fk.constraint.name == name
    assert fk.target_fullname == f"{referred}.id"
    # No ON DELETE: users are deactivated, never deleted, so a grant keeps naming its grantor.
    assert fk.ondelete is None and fk.onupdate is None
    for other in ("export_authorized", "export_authorized_at"):
        assert not User.__table__.c[other].foreign_keys, other


def test_the_check_text_is_byte_identical_to_the_model_the_contract_and_the_enum():
    module = _module()
    name, text = CONTRACT_CHECK
    assert module.ACCESS_LEVEL_CHECK_SQL == text == DOCUMENT_ACCESS_LEVEL_CHECK
    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in Document.__table__.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }
    assert checks.get(name) == text
    # The CHECK admits exactly the levels the enum names, in the enum's order.
    assert re.findall(r"'([a-z_]+)'", text) == [level.value for level in DocumentAccessLevel]
    assert DocumentAccessLevel.STANDARD.value == module.DEFAULT_ACCESS_LEVEL


def test_the_index_matches_the_model():
    name, columns = CONTRACT_INDEX
    indexes = {index.name: index for index in Document.__table__.indexes}
    assert name in indexes
    assert tuple(column.name for column in indexes[name].columns) == columns
    assert not indexes[name].unique
    # No user column is indexed (the migration creates no users index).
    for index in User.__table__.indexes:
        assert not set(NEW_COLUMNS["users"]) & {column.name for column in index.columns}, index.name


def test_export_authorized_default_fails_closed_on_both_dialects():
    """``export_authorized`` is a security flag, so its default must read False however a row is
    written. The plain string ``"false"`` the company switches use does not: SQLite stores it as
    TEXT, which SQLAlchemy's Boolean reads as True. ``sa.false()`` renders ``false`` on PostgreSQL
    and ``0`` on SQLite, identically from the model and the migration."""
    from sqlalchemy.dialects import sqlite

    model_column = User.__table__.c["export_authorized"]
    migrated = _migration_columns(_module(), "users")["export_authorized"]
    for column in (model_column, migrated):
        assert _default_spelling(column) == SA_FALSE
        default = column.server_default.arg
        assert str(default.compile(dialect=postgresql.dialect())) == "false"
        assert str(default.compile(dialect=sqlite.dialect())) == "0"


# ------------------------------------------------------------- 3. real SQLite round-trip

# Synthetic names only: the repository is public.
DOCUMENT_VIEW_TRIGGER_SQL = (
    "CREATE TRIGGER tr_document_view_insert BEFORE INSERT ON document_views\n"
    "BEGIN\n"
    " SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM users AS u JOIN documents AS d ON d.company_id = u.company_id\n"
    " WHERE u.id = NEW.user_id AND d.id = NEW.document_id AND d.company_id = NEW.company_id)\n"
    " THEN RAISE(ABORT, 'Document view must name a tenant user and document.') END;\n"
    "END"
)

USERS_DDL = (
    "CREATE TABLE users (id INTEGER NOT NULL, company_id INTEGER NOT NULL, employee_id VARCHAR(50) NOT NULL, "
    "email VARCHAR(255) NOT NULL, role VARCHAR(14) NOT NULL, is_active BOOLEAN, is_superuser BOOLEAN, "
    "floor_pin_failed_attempts INTEGER DEFAULT '0' NOT NULL, created_by INTEGER, PRIMARY KEY (id), "
    "CONSTRAINT uq_users_company_email UNIQUE (company_id, email), "
    "CONSTRAINT uq_users_company_employee_id UNIQUE (company_id, employee_id), "
    "FOREIGN KEY(company_id) REFERENCES companies (id), "
    "CONSTRAINT fk_users_created_by FOREIGN KEY(created_by) REFERENCES users (id) ON DELETE SET NULL)"
)
# users' children: under foreign_keys = ON a rebuild's DROP TABLE would delete the first's rows
# and null the second's. documents' children (below) are NO ACTION, as on the models.
USER_WORKSPACE_DDL = (
    "CREATE TABLE user_workspace_records (id INTEGER PRIMARY KEY, "
    "user_id INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE, payload TEXT)"
)
WORK_ORDERS_DDL = (
    "CREATE TABLE work_orders (id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL, created_by INTEGER, "
    "CONSTRAINT fk_work_orders_created_by FOREIGN KEY(created_by) REFERENCES users (id) ON DELETE SET NULL)"
)

LEGACY_DDL = [
    "CREATE TABLE companies (id INTEGER NOT NULL, name VARCHAR(255) NOT NULL, PRIMARY KEY (id))",
    (
        "CREATE TABLE parts (id INTEGER NOT NULL, company_id INTEGER NOT NULL, part_number VARCHAR(50) NOT NULL, "
        "PRIMARY KEY (id), FOREIGN KEY(company_id) REFERENCES companies (id))"
    ),
    USERS_DDL,
    "CREATE INDEX ix_users_company_active ON users (company_id, is_active)",
    "CREATE INDEX ix_users_email ON users (email)",
    (
        "CREATE TABLE documents (id INTEGER NOT NULL, previous_revision_id INTEGER, "
        "document_number VARCHAR(100) NOT NULL, revision VARCHAR(20), title VARCHAR(255) NOT NULL, "
        "document_type VARCHAR(16) NOT NULL, part_id INTEGER, status VARCHAR(50), is_controlled BOOLEAN, "
        "created_by INTEGER, company_id INTEGER NOT NULL, PRIMARY KEY (id), "
        "CONSTRAINT fk_documents_previous_revision FOREIGN KEY(previous_revision_id) REFERENCES documents (id), "
        "FOREIGN KEY(part_id) REFERENCES parts (id), FOREIGN KEY(created_by) REFERENCES users (id), "
        "FOREIGN KEY(company_id) REFERENCES companies (id))"
    ),
    "CREATE UNIQUE INDEX ix_documents_document_number ON documents (document_number)",
    "CREATE INDEX ix_documents_company_id ON documents (company_id)",
    "CREATE INDEX ix_documents_company_part ON documents (company_id, part_id) WHERE part_id IS NOT NULL",
    "CREATE INDEX ix_documents_company_previous_revision ON documents (company_id, previous_revision_id)",
    USER_WORKSPACE_DDL,
    WORK_ORDERS_DDL,
    "CREATE TABLE laser_nests (id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL, "
    "document_id INTEGER REFERENCES documents (id))",
    "CREATE TABLE qms_clause_evidence (id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL, "
    "document_id INTEGER REFERENCES documents (id))",
    # No real trigger reads users or documents today (pinned below); this one proves a later one
    # cannot break 131's rebuilds, which run under 129's legacy_alter_table wrapper.
    (
        "CREATE TABLE document_views (id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL, "
        "user_id INTEGER NOT NULL, document_id INTEGER NOT NULL)"
    ),
    DOCUMENT_VIEW_TRIGGER_SQL,
    "INSERT INTO companies VALUES (1, 'Synthetic Co'), (2, 'Other tenant')",
    "INSERT INTO parts VALUES (1, 1, 'P-100')",
    "INSERT INTO users VALUES (5, 1, '1001', 'admin@example.test', 'ADMIN', 1, 0, 0, NULL)",
    "INSERT INTO users VALUES (6, 1, '1002', 'op@example.test', 'OPERATOR', 1, 0, 2, 5)",
    "INSERT INTO users VALUES (7, 2, '2001', 'other@example.test', 'QUALITY', 0, 0, 0, NULL)",
    "INSERT INTO documents VALUES (10, NULL, 'DOC-1', 'A', 'Drawing rev A', 'DRAWING', 1, 'obsolete', 1, 5, 1)",
    "INSERT INTO documents VALUES (11, 10, 'DOC-1-B', 'B', 'Drawing rev B', 'DRAWING', 1, 'released', 1, 6, 1)",
    "INSERT INTO documents VALUES (12, NULL, 'DOC-2', 'A', 'Procedure', 'PROCEDURE', NULL, 'released', 1, 7, 2)",
    "INSERT INTO user_workspace_records VALUES (1, 5, '{}'), (2, 6, '{}'), (3, 7, '{}')",
    "INSERT INTO work_orders VALUES (1, 1, 5), (2, 1, 6), (3, 2, 7)",
    "INSERT INTO laser_nests VALUES (1, 1, 11)",
    "INSERT INTO qms_clause_evidence VALUES (1, 1, 10), (2, 2, 12)",
    "INSERT INTO document_views VALUES (1, 1, 5, 11)",
]

LEGACY_ROWS_SQL = {
    "users": "SELECT id, company_id, employee_id, email, role, is_active, is_superuser, floor_pin_failed_attempts, "
    "created_by FROM users ORDER BY id",
    "documents": "SELECT id, previous_revision_id, document_number, revision, title, document_type, part_id, status, "
    "is_controlled, created_by, company_id FROM documents ORDER BY id",
    "user_workspace_records": "SELECT id, user_id, payload FROM user_workspace_records ORDER BY id",
    "work_orders": "SELECT id, company_id, created_by FROM work_orders ORDER BY id",
    "laser_nests": "SELECT id, company_id, document_id FROM laser_nests ORDER BY id",
    "qms_clause_evidence": "SELECT id, company_id, document_id FROM qms_clause_evidence ORDER BY id",
    "document_views": "SELECT id, company_id, user_id, document_id FROM document_views ORDER BY id",
}
LEGACY_ROWS = {
    "users": [
        (5, 1, "1001", "admin@example.test", "ADMIN", 1, 0, 0, None),
        (6, 1, "1002", "op@example.test", "OPERATOR", 1, 0, 2, 5),
        (7, 2, "2001", "other@example.test", "QUALITY", 0, 0, 0, None),
    ],
    "documents": [
        (10, None, "DOC-1", "A", "Drawing rev A", "DRAWING", 1, "obsolete", 1, 5, 1),
        (11, 10, "DOC-1-B", "B", "Drawing rev B", "DRAWING", 1, "released", 1, 6, 1),
        (12, None, "DOC-2", "A", "Procedure", "PROCEDURE", None, "released", 1, 7, 2),
    ],
    "user_workspace_records": [(1, 5, "{}"), (2, 6, "{}"), (3, 7, "{}")],
    "work_orders": [(1, 1, 5), (2, 1, 6), (3, 2, 7)],
    "laser_nests": [(1, 1, 11)],
    "qms_clause_evidence": [(1, 1, 10), (2, 2, 12)],
    "document_views": [(1, 1, 5, 11)],
}
EXPORT_SQL = "SELECT export_authorized, export_authorized_at, export_authorized_by FROM users ORDER BY id"
ACCESS_SQL = "SELECT access_level FROM documents ORDER BY id"
NOT_AUTHORIZED = (0, None, None)


def _enable_foreign_keys(dbapi_connection, _record):
    dbapi_connection.execute("PRAGMA foreign_keys = ON")


def _engine(path, *, enforce: bool, statements=None):
    engine = sa.create_engine(f"sqlite:///{path}")
    if enforce:
        sa.event.listen(engine, "connect", _enable_foreign_keys)
    if statements is not None:
        sa.event.listen(
            engine, "before_cursor_execute", lambda _c, _cur, statement, *_a: statements.append(_ws(statement))
        )
    return engine


@contextmanager
def _migrating(path, *, enforce: bool = False, statements=None):
    """A connection to ``path`` (foreign keys OFF unless ``enforce``) with the migration bound to it."""
    engine = _engine(path, enforce=enforce, statements=statements)
    try:
        with engine.begin() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == int(enforce)
            migration = _module()
            migration.op = Operations(MigrationContext.configure(connection))
            yield connection, migration
    finally:
        engine.dispose()


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "m131.db"
    engine = _engine(path, enforce=False)
    with engine.begin() as connection:
        for statement in LEGACY_DDL:
            connection.exec_driver_sql(statement)
    engine.dispose()
    return path


@pytest.fixture
def scratch(db_path):
    with _migrating(db_path) as pair:
        yield pair


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
        "checks": {ck["name"]: _ws(ck["sqltext"]) for ck in inspector.get_check_constraints(table)},
        "indexes": {ix["name"]: (tuple(ix["column_names"]), bool(ix["unique"])) for ix in inspector.get_indexes(table)},
        "uniques": {uq["name"]: tuple(uq["column_names"]) for uq in inspector.get_unique_constraints(table)},
    }


def _stored_index_sql(connection):
    rows = connection.exec_driver_sql(
        "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL AND tbl_name IN "
        "('users', 'documents')"
    )
    return {name: _ws(sql) for name, sql in rows}


def _root_pages(connection):
    """Each table's b-tree root page: a copy-and-rename rebuild gives the table a new one."""
    return dict(connection.exec_driver_sql("SELECT name, rootpage FROM sqlite_master WHERE type = 'table'").fetchall())


def _assert_legacy_rows_intact(connection):
    for table, sql in LEGACY_ROWS_SQL.items():
        assert [tuple(row) for row in connection.exec_driver_sql(sql)] == LEGACY_ROWS[table], table
    triggers = connection.exec_driver_sql("SELECT name, tbl_name FROM sqlite_master WHERE type = 'trigger'")
    assert [tuple(row) for row in triggers] == [("tr_document_view_insert", "document_views")]
    assert connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall() == []
    assert not connection.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE name LIKE '_alembic_tmp_%'"
    ).fetchall(), "a rebuild left its temp table behind"


def _authorized_through_the_model(connection):
    """Each user's ``export_authorized`` read through the MODEL's Boolean type, as the ORM reads it."""
    table = User.__table__
    rows = connection.execute(sa.select(table.c.id, table.c.export_authorized).order_by(table.c.id))
    return [(row[0], row[1]) for row in rows]


def _legacy_shape(connection):
    return {table: _objects(connection, table) for table in TABLES}


def test_upgrade_adds_every_object_and_backfills_nothing(scratch):
    connection, migration = scratch
    legacy = _legacy_shape(connection)
    legacy_index_sql = _stored_index_sql(connection)
    pages = _root_pages(connection)
    migration.upgrade()

    # Both tables really were rebuilt (the FK and the CHECK force it on SQLite), and only they were:
    # everything asserted below about rows and children held THROUGH a copy-and-rename.
    after = _root_pages(connection)
    assert {table for table in pages if after.get(table) != pages[table]} == set(TABLES)
    for table in TABLES:
        objects, before = _objects(connection, table), legacy[table]
        assert set(objects["columns"]) == set(before["columns"]) | set(NEW_COLUMNS[table]), table
        for name in before["columns"]:
            assert objects["columns"][name] == before["columns"][name], name
        # Every legacy FK, CHECK, index and unique survived the rebuild; exactly the new ones were added.
        assert {fk[0] for fk in objects["fks"]} - {fk[0] for fk in before["fks"]} == NEW_OBJECTS[table]["fks"]
        assert [fk for fk in objects["fks"] if fk[0] not in NEW_OBJECTS[table]["fks"]] == before["fks"], table
        assert set(objects["checks"]) - set(before["checks"]) == NEW_OBJECTS[table]["checks"], table
        assert set(objects["indexes"]) - set(before["indexes"]) == NEW_OBJECTS[table]["indexes"], table
        for kind in ("checks", "indexes"):
            for name, value in before[kind].items():
                assert objects[kind][name] == value, (table, kind, name)
        assert objects["uniques"] == before["uniques"], table
    # The partial index predicates survive the rebuild byte for byte, and the new index is plain.
    stored = _stored_index_sql(connection)
    for name, sql in legacy_index_sql.items():
        assert stored[name] == sql, name
    assert stored[CONTRACT_INDEX[0]] == "CREATE INDEX ix_documents_access_level ON documents (company_id, access_level)"

    users, documents = _objects(connection, "users"), _objects(connection, "documents")
    assert users["columns"]["export_authorized"] == ("BOOLEAN", False, "0")
    assert users["columns"]["export_authorized_at"] == ("DATETIME", True, None)
    assert users["columns"]["export_authorized_by"] == ("INTEGER", True, None)
    assert documents["columns"]["access_level"] == ("VARCHAR(30)", False, "'standard'")
    assert documents["checks"][CONTRACT_CHECK[0]] == CONTRACT_CHECK[1]
    assert documents["indexes"][CONTRACT_INDEX[0]] == (CONTRACT_INDEX[1], False)
    _assert_legacy_rows_intact(connection)

    # No backfill: every document reads the server default and nobody is authorized. DEFAULT 0
    # (sa.false() on SQLite) fills each pre-existing user with the integer 0, which the model's
    # Boolean reads as False (the SQLite-only normalization is then a no-op), so every
    # pre-existing user reads NOT authorized.
    assert [row[0] for row in connection.exec_driver_sql(ACCESS_SQL)] == ["standard"] * 3
    assert [tuple(row) for row in connection.exec_driver_sql(EXPORT_SQL)] == [NOT_AUTHORIZED] * 3
    assert _authorized_through_the_model(connection) == [(5, False), (6, False), (7, False)]


def test_without_the_normalization_sqlite_would_read_every_user_as_export_authorized(scratch):
    """The reason the SQLite-only UPDATE is kept: a column added with the TEXT default (an earlier
    draft of 131, or a hand-run ALTER) stores TEXT 'false' on a pre-existing row, and the model's
    Boolean reads that as True -- every user would open every export-controlled document on a
    local dev database. Proven on the raw ALTER; the UPDATE re-spells it as 0."""
    connection, _migration = scratch
    connection.exec_driver_sql("ALTER TABLE users ADD COLUMN export_authorized BOOLEAN DEFAULT 'false' NOT NULL")
    assert _authorized_through_the_model(connection) == [(5, True), (6, True), (7, True)]
    connection.exec_driver_sql(_module().SQLITE_NORMALIZE_EXPORT_AUTHORIZED_SQL)
    assert _authorized_through_the_model(connection) == [(5, False), (6, False), (7, False)]


def test_the_normalization_is_sqlite_only_and_runs_once_right_after_the_column(db_path):
    normalize = _module().SQLITE_NORMALIZE_EXPORT_AUTHORIZED_SQL
    assert normalize == "UPDATE users SET export_authorized = 0 WHERE export_authorized = 'false'"
    statements = []
    with _migrating(db_path, statements=statements) as (_connection, migration):
        migration.upgrade()
        assert statements.count(normalize) == 1
        migration.upgrade()  # a re-run adds no column, so it re-spells nothing
        assert statements.count(normalize) == 1
    assert not any("UPDATE" in statement for statement in _render("upgrade"))


def test_the_check_and_the_not_nulls_are_real(scratch):
    connection, migration = scratch
    migration.upgrade()
    for level in DocumentAccessLevel:
        connection.exec_driver_sql(f"UPDATE documents SET access_level = '{level.value}' WHERE id = 11")
    for statement in (
        "UPDATE documents SET access_level = 'secret' WHERE id = 11",
        "UPDATE documents SET access_level = 'STANDARD' WHERE id = 11",  # the CHECK is case-sensitive
        "UPDATE documents SET access_level = NULL WHERE id = 11",
        "UPDATE users SET export_authorized = NULL WHERE id = 5",
        "INSERT INTO documents (id, document_number, title, document_type, company_id, access_level) "
        "VALUES (13, 'DOC-3', 'Bad', 'OTHER', 1, 'confidential')",
    ):
        nested = connection.begin_nested()
        with pytest.raises(sa.exc.IntegrityError):
            connection.exec_driver_sql(statement)
        nested.rollback()


def test_the_new_foreign_key_names_its_grantor_with_no_delete_action(scratch):
    connection, migration = scratch
    migration.upgrade()
    reflected = {fk["name"]: fk for fk in sa.inspect(connection).get_foreign_keys("users") if fk["name"]}
    fk = reflected[CONTRACT_FK[0]]
    assert fk["constrained_columns"] == [CONTRACT_FK[2]]
    assert fk["referred_table"] == CONTRACT_FK[1] and fk["referred_columns"] == ["id"]
    assert not (fk.get("options") or {})  # no ON DELETE / ON UPDATE
    # A grant naming a real grantor is consistent; one naming nobody is what the FK refuses.
    connection.exec_driver_sql(
        "UPDATE users SET export_authorized = 1, export_authorized_at = '2026-11-19 08:00:00', "
        "export_authorized_by = 5 WHERE id = 6"
    )
    assert connection.exec_driver_sql("PRAGMA foreign_key_check(users)").fetchall() == []
    connection.exec_driver_sql("UPDATE users SET export_authorized_by = 999 WHERE id = 7")
    violations = connection.exec_driver_sql("PRAGMA foreign_key_check(users)").fetchall()
    assert [(row[0], row[1], row[2]) for row in violations] == [("users", 7, "users")]


def test_a_pre_131_insert_still_lands_standard_and_unauthorized(scratch):
    """Old code never names the columns: the server defaults satisfy every NOT NULL and the CHECK
    -- which is what makes a stamp rollback safe for inserts. On SQLite the raw insert stores the
    ``sa.false()`` default as the integer 0, which the model reads as False: an insert that omits
    the column can never make a user export-authorized (it would with a TEXT 'false' default)."""
    connection, migration = scratch
    migration.upgrade()
    connection.exec_driver_sql(
        "INSERT INTO users (id, company_id, employee_id, email, role) VALUES (8, 1, '1003', 'n@example.test', 'VIEWER')"
    )
    connection.exec_driver_sql(
        "INSERT INTO documents (id, document_number, title, document_type, company_id) "
        "VALUES (13, 'DOC-3', 'New drawing', 'DRAWING', 1)"
    )
    assert connection.exec_driver_sql(EXPORT_SQL).fetchall()[-1] == (0, None, None)
    assert _authorized_through_the_model(connection)[-1] == (8, False)
    assert connection.exec_driver_sql(ACCESS_SQL).fetchall()[-1] == ("standard",)


def test_a_raw_insert_on_the_create_all_schema_reads_not_authorized_through_the_model(tmp_path):
    """The same fail-closed default on the schema the MODELS build (``create_all``, every
    non-production boot and the whole test suite): a raw-SQL insert that omits ``export_authorized``
    reads False through the model on SQLite."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'm131_create_all.db'}")
    try:
        User.__table__.create(engine)
        with engine.begin() as connection:
            ddl = connection.exec_driver_sql("SELECT sql FROM sqlite_master WHERE name = 'users'").scalar()
            assert "export_authorized BOOLEAN DEFAULT 0 NOT NULL" in _ws(ddl)
            connection.exec_driver_sql(
                "INSERT INTO users (id, company_id, employee_id, email, hashed_password, first_name, last_name, role) "
                "VALUES (1, 1, '1001', 'raw@example.test', 'x', 'Raw', 'Insert', 'OPERATOR')"
            )
            assert connection.exec_driver_sql("SELECT export_authorized FROM users").scalar() == 0
            assert _authorized_through_the_model(connection) == [(1, False)]
    finally:
        engine.dispose()


def test_rerun_is_a_no_op_and_a_partial_schema_recovers_each_object(scratch):
    connection, migration = scratch
    migration.upgrade()
    connection.exec_driver_sql(
        "UPDATE users SET export_authorized = 1, export_authorized_at = '2026-11-19 08:00:00', "
        "export_authorized_by = 5 WHERE id = 6"
    )
    connection.exec_driver_sql("UPDATE documents SET access_level = 'export_controlled' WHERE id = 11")
    connection.exec_driver_sql("UPDATE documents SET access_level = 'restricted' WHERE id = 12")
    full = _legacy_shape(connection)
    migration.upgrade()  # a re-run is a guarded no-op
    assert _legacy_shape(connection) == full

    # Knock out one object of each kind -- the index, the CHECK, the FK and a plain column -- and
    # prove each guard restores only what is missing. The knock-out rebuilds go through the
    # migration's own ``op`` and legacy_alter_table wrapper: a bare rebuild dies on the trigger.
    ops = migration.op
    ops.drop_index(CONTRACT_INDEX[0], table_name="documents")
    with migration._sqlite_rebuild_safe(True), ops.batch_alter_table("documents") as batch:
        batch.drop_constraint(CONTRACT_CHECK[0], type_="check")
    with migration._sqlite_rebuild_safe(True), ops.batch_alter_table("users") as batch:
        batch.drop_constraint(CONTRACT_FK[0], type_="foreignkey")
    connection.exec_driver_sql("ALTER TABLE users DROP COLUMN export_authorized_at")
    assert CONTRACT_INDEX[0] not in _objects(connection, "documents")["indexes"]
    assert CONTRACT_CHECK[0] not in _objects(connection, "documents")["checks"]
    assert "export_authorized_at" not in _objects(connection, "users")["columns"]

    migration.upgrade()
    assert _legacy_shape(connection) == full
    # Values recorded in the surviving columns are kept; nothing is overwritten or re-derived.
    assert connection.exec_driver_sql(EXPORT_SQL).fetchall() == [(0, None, None), (1, None, 5), (0, None, None)]
    assert [row[0] for row in connection.exec_driver_sql(ACCESS_SQL)] == ["standard", "export_controlled", "restricted"]
    _assert_legacy_rows_intact(connection)


def test_a_plain_column_alone_is_added_natively_even_with_foreign_keys_enforced(db_path):
    """Only an FK / CHECK (or a drop) forces a SQLite rebuild. With every other object in place,
    re-adding ``export_authorized`` is a native ALTER -- no rebuild, so no refusal under
    ``foreign_keys = ON`` -- and the storage normalization still follows it."""
    with _migrating(db_path) as (connection, migration):
        migration.upgrade()
        connection.exec_driver_sql("ALTER TABLE users DROP COLUMN export_authorized")
    with _migrating(db_path, enforce=True) as (connection, migration):
        pages = _root_pages(connection)
        migration.upgrade()
        assert _root_pages(connection) == pages  # never rebuilt
        assert _authorized_through_the_model(connection) == [(5, False), (6, False), (7, False)]
        assert "export_authorized" in _objects(connection, "users")["columns"]
        _assert_legacy_rows_intact(connection)


def test_downgrade_removes_exactly_what_upgrade_added_and_replays(scratch):
    connection, migration = scratch
    legacy = _legacy_shape(connection)
    legacy_index_sql = _stored_index_sql(connection)
    migration.upgrade()
    connection.exec_driver_sql("UPDATE users SET export_authorized = 1, export_authorized_by = 5 WHERE id = 6")
    connection.exec_driver_sql("UPDATE documents SET access_level = 'export_controlled' WHERE id = 11")
    migration.downgrade()

    assert _legacy_shape(connection) == legacy
    assert _stored_index_sql(connection) == legacy_index_sql
    _assert_legacy_rows_intact(connection)
    migration.downgrade()  # a second downgrade is a guarded no-op, not an error
    assert _legacy_shape(connection) == legacy

    migration.upgrade()  # the chain replays forward cleanly...
    for table in TABLES:
        assert set(NEW_COLUMNS[table]) <= set(_objects(connection, table)["columns"]), table
    # ...and the downgrade was LOSSY, as the docstring warns: the export-controlled drawing came
    # back 'standard' (openable by everyone) and the grant is gone. A code rollback stamps 130.
    assert [row[0] for row in connection.exec_driver_sql(ACCESS_SQL)] == ["standard"] * 3
    assert _authorized_through_the_model(connection) == [(5, False), (6, False), (7, False)]
    _assert_legacy_rows_intact(connection)


def _assert_the_trigger_resolves_both_tables(connection, view_id):
    """The trigger fires against the REBUILT tables: a real pair passes, anything else aborts."""
    connection.exec_driver_sql(f"INSERT INTO document_views VALUES ({view_id}, 1, 6, 10)")
    for company_id, user_id, document_id in ((1, 999, 10), (1, 5, 999), (2, 7, 10)):
        nested = connection.begin_nested()
        with pytest.raises(sa.exc.IntegrityError, match="Document view must name a tenant user and document"):
            connection.exec_driver_sql(
                f"INSERT INTO document_views VALUES ({view_id + 1}, {company_id}, {user_id}, {document_id})"
            )
        nested.rollback()
    connection.exec_driver_sql(f"DELETE FROM document_views WHERE id = {view_id}")


def test_a_trigger_that_reads_both_tables_survives_both_rebuilds(scratch):
    connection, migration = scratch
    migration.upgrade()
    _assert_legacy_rows_intact(connection)
    _assert_the_trigger_resolves_both_tables(connection, 10)
    migration.downgrade()
    _assert_legacy_rows_intact(connection)
    _assert_the_trigger_resolves_both_tables(connection, 20)


def test_the_connection_keeps_its_own_legacy_alter_table_setting(scratch):
    connection, migration = scratch
    assert connection.exec_driver_sql("PRAGMA legacy_alter_table").scalar() == 0
    migration.upgrade()
    assert connection.exec_driver_sql("PRAGMA legacy_alter_table").scalar() == 0
    connection.exec_driver_sql("PRAGMA legacy_alter_table = ON")
    migration.downgrade()
    assert connection.exec_driver_sql("PRAGMA legacy_alter_table").scalar() == 1


def test_no_model_trigger_or_view_reads_users_or_documents(tmp_path):
    """No trigger built from the models is ON either table (a rebuild would silently drop it) or
    names either in its body. Pin it so a future trigger is a decision, not a surprise."""
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
        assert not re.search(r"\b(users|documents)\b", sql or ""), f"{kind} {name} reads users / documents"


# ------------------------------------------------------- 4. foreign-key enforcement


def test_the_cascade_hazard_the_refusal_guards_against_is_real(tmp_path):
    """Why the migration refuses under ``foreign_keys = ON``: a plain SQLite rebuild of ``users``
    there deletes its ON DELETE CASCADE children and nulls its SET NULL ones."""
    engine = _engine(tmp_path / "hazard.db", enforce=True)
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE companies (id INTEGER PRIMARY KEY)")
        connection.exec_driver_sql("INSERT INTO companies VALUES (1)")
        for statement in (USERS_DDL, USER_WORKSPACE_DDL, WORK_ORDERS_DDL):
            connection.exec_driver_sql(statement)
        connection.exec_driver_sql("INSERT INTO users VALUES (5, 1, '1001', 'a@example.test', 'ADMIN', 1, 0, 0, NULL)")
        connection.exec_driver_sql("INSERT INTO user_workspace_records VALUES (1, 5, '{}')")
        connection.exec_driver_sql("INSERT INTO work_orders VALUES (1, 1, 5)")
        with Operations(MigrationContext.configure(connection)).batch_alter_table("users", recreate="always"):
            pass
        assert connection.exec_driver_sql("SELECT COUNT(*) FROM user_workspace_records").scalar() == 0
        assert connection.exec_driver_sql("SELECT created_by FROM work_orders").scalar() is None
    engine.dispose()


def test_upgrade_refuses_before_any_ddl_when_foreign_keys_are_enforced(db_path):
    with _migrating(db_path) as (connection, _migration):
        legacy = _legacy_shape(connection)
    statements = []
    with _migrating(db_path, enforce=True, statements=statements) as (connection, migration):
        with pytest.raises(RuntimeError, match=r"rebuild users and documents.*PRAGMA foreign_keys = ON"):
            migration.upgrade()
        assert not [s for s in statements if re.match(r"(CREATE|ALTER|DROP|INSERT|UPDATE|DELETE|PRAGMA legacy)", s)]
        assert _legacy_shape(connection) == legacy
        _assert_legacy_rows_intact(connection)


def test_downgrade_refuses_before_any_ddl_when_foreign_keys_are_enforced(db_path):
    with _migrating(db_path) as (connection, migration):
        migration.upgrade()
        connection.exec_driver_sql("UPDATE documents SET access_level = 'export_controlled' WHERE id = 11")
        upgraded = _legacy_shape(connection)
    statements = []
    with _migrating(db_path, enforce=True, statements=statements) as (connection, migration):
        with pytest.raises(RuntimeError, match=r"rebuild documents and users.*PRAGMA foreign_keys = ON"):
            migration.downgrade()
        assert not [s for s in statements if re.match(r"(CREATE|ALTER|DROP|INSERT|UPDATE|DELETE|PRAGMA legacy)", s)]
        assert _legacy_shape(connection) == upgraded
        assert [row[0] for row in connection.exec_driver_sql(ACCESS_SQL)] == [
            "standard",
            "export_controlled",
            "standard",
        ]
        _assert_legacy_rows_intact(connection)


def test_documents_children_carry_no_delete_action_and_the_app_never_enforces_foreign_keys():
    """The docstring's two premises: nothing references ``documents`` with an ON DELETE action, and
    neither the app engine nor alembic's env turns SQLite foreign keys on (so a real SQLite run is
    never refused, and its rebuild's DROP TABLE is a plain drop)."""
    from app.db.database import Base

    for table in Base.metadata.sorted_tables:
        for fk in table.foreign_keys:
            if fk.column.table.name == "documents":
                assert fk.ondelete is None, f"{table.name}.{fk.parent.name}"
    users_children = {
        f"{table.name}.{fk.parent.name}": fk.ondelete
        for table in Base.metadata.sorted_tables
        for fk in table.foreign_keys
        if fk.column.table.name == "users" and fk.ondelete
    }
    assert users_children.get("user_workspace_records.user_id") == "CASCADE"
    for relative in (("app", "db", "database.py"), ("alembic", "env.py")):
        with open(os.path.join(BACKEND_DIR, *relative)) as handle:
            assert not re.search(r"foreign_keys\s*=\s*(ON|1|true)", handle.read(), re.I), os.path.join(*relative)


# -------------------------------------------------------- 5. create_all bootstrap no-op


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
        Base.metadata.create_all(conn, tables=[Base.metadata.tables[name] for name in ("companies", *TABLES)])
        recorder = _Recorder(conn)
        module.op = recorder
        module.upgrade()
    engine.dispose()
    assert recorder.calls == []


# ------------------------------------------------------------- 6. offline PostgreSQL SQL


def _render(function_name: str):
    module = _module()
    buffer = io.StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buffer})
    module.op = Operations(context)
    with context.begin_transaction():
        getattr(module, function_name)()
    statements = [" ".join(statement.split()) for statement in buffer.getvalue().split(";") if statement.strip()]
    return [statement for statement in statements if statement not in ("BEGIN", "COMMIT")]


EXPECTED_USER_DDL = [
    # Metadata-only on PostgreSQL 11+: nullable with no default, or a constant default; no rewrite.
    "ALTER TABLE users ADD COLUMN export_authorized BOOLEAN DEFAULT false NOT NULL",
    f"ALTER TABLE users ADD COLUMN export_authorized_at {TIMESTAMP}",
    "ALTER TABLE users ADD COLUMN export_authorized_by INTEGER",
    "ALTER TABLE users ADD CONSTRAINT fk_users_export_authorized_by FOREIGN KEY(export_authorized_by) "
    "REFERENCES users (id)",
]
EXPECTED_DOCUMENT_DDL = [
    "ALTER TABLE documents ADD COLUMN access_level VARCHAR(30) DEFAULT 'standard' NOT NULL",
    f"ALTER TABLE documents ADD CONSTRAINT ck_documents_access_level CHECK ({CONTRACT_CHECK[1]})",
    "CREATE INDEX ix_documents_access_level ON documents (company_id, access_level)",
]


def _assert_pure_ddl(sql: str):
    assert not re.search(r"\b(UPDATE|INSERT|DELETE)\b", sql), "131 is pure DDL on PostgreSQL: no backfill"
    assert "ON DELETE" not in sql and "ON UPDATE" not in sql and "CONCURRENTLY" not in sql
    assert not re.search(r"ROW LEVEL SECURITY|REVOKE|GRANT|\banon\b|authenticated|service_role", sql)
    assert "audit_log" not in sql and "CREATE TABLE" not in sql and "DROP TABLE" not in sql
    assert "TRIGGER" not in sql and "PRAGMA" not in sql


def test_offline_upgrade_renders_the_canonical_postgres_ddl():
    statements = _render("upgrade")
    # Exactly these, in this order: the timeout, users (columns then the FK), documents (column,
    # CHECK, index), the reset.
    assert statements == [
        "SET LOCAL lock_timeout = '5s'",
        *EXPECTED_USER_DDL,
        *EXPECTED_DOCUMENT_DDL,
        "SET LOCAL lock_timeout = DEFAULT",
    ]
    _assert_pure_ddl("\n".join(statements))


def test_offline_downgrade_drops_exactly_what_upgrade_added_in_reverse():
    statements = _render("downgrade")
    assert statements == [
        "SET LOCAL lock_timeout = '5s'",
        # Reverse of the upgrade: documents first (index, CHECK, column), then users (FK, columns).
        "DROP INDEX ix_documents_access_level",
        "ALTER TABLE documents DROP CONSTRAINT ck_documents_access_level",
        "ALTER TABLE documents DROP COLUMN access_level",
        "ALTER TABLE users DROP CONSTRAINT fk_users_export_authorized_by",
        "ALTER TABLE users DROP COLUMN export_authorized_by",
        "ALTER TABLE users DROP COLUMN export_authorized_at",
        "ALTER TABLE users DROP COLUMN export_authorized",
        "SET LOCAL lock_timeout = DEFAULT",
    ]
    _assert_pure_ddl("\n".join(statements))


@pytest.mark.parametrize("table", TABLES)
def test_create_all_ddl_carries_the_same_objects_on_postgres(table):
    statements = []
    engine = sa.create_mock_engine(
        "postgresql://", lambda statement, *a, **k: statements.append(str(statement.compile(dialect=engine.dialect)))
    )
    MODELS[table].__table__.create(engine, checkfirst=False)
    ddl = " ".join(" ".join(statements).split())
    for statement in EXPECTED_USER_DDL + EXPECTED_DOCUMENT_DDL:
        column_prefix = f"ALTER TABLE {table} ADD COLUMN "
        constraint_prefix = f"ALTER TABLE {table} ADD "
        if statement.startswith(column_prefix):
            column_ddl = statement[len(column_prefix) :]
            # The model renders the identical column clause inside its CREATE TABLE.
            assert re.search(rf"[(,] {re.escape(column_ddl)} ?[,)]", ddl), column_ddl
        elif statement.startswith(constraint_prefix):
            constraint_ddl = statement[len(constraint_prefix) :]
            assert re.search(rf", {re.escape(constraint_ddl)} ?[,)]", ddl), constraint_ddl
        elif statement.startswith("CREATE INDEX") and f" ON {table} " in statement:
            assert statement in ddl, statement
