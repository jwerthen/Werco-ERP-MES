"""Coverage for 125_work_order_comments: ``comments``, ``comment_mentions`` and ``entity_watchers``.

Four layers:

1. Script wiring -- one head, the down-revision, the id is the file stem and fits ``alembic_version``.
2. A real upgrade -> replay -> downgrade -> replay round trip over a scratch SQLite database holding
   pre-existing companies and users: every object is guarded independently (a partially bootstrapped
   table recovers its unique constraint, its foreign keys and each index), the constraints are
   enforced, and the downgrade leaves the rest of the schema and its data alone.
3. Model == migration lock-step: columns (type, nullability, server default), index names and
   columns, unique-constraint names and columns, and the unnamed foreign keys, table by table.
4. The PostgreSQL posture, rendered offline: ENABLE ROW LEVEL SECURITY and the REVOKEs on every
   table and sequence, the ``anon`` / ``authenticated`` ones under a ``pg_roles`` check, in BOTH
   the migration and the models' ``after_create`` listeners -- and in the migration even when the
   tables already exist (the create_all path), because RLS must never sit under a table guard.
"""

import importlib.util
import io
import os
from datetime import timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from app.models import comment as comment_models
from app.models.comment import Comment, CommentMention, EntityWatcher

pytestmark = [pytest.mark.unit]

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIGRATION_FILE = os.path.join(BACKEND_DIR, "alembic", "versions", "125_work_order_comments.py")
REVISION = "125_work_order_comments"
DOWN_REVISION = "124_maintenance_assets"
MODELS = {"comments": Comment, "comment_mentions": CommentMention, "entity_watchers": EntityWatcher}
TABLES = tuple(MODELS)

EXPECTED_INDEXES = {
    "comments": {
        "ix_comments_author_id": ("author_id",),
        "ix_comments_is_deleted": ("is_deleted",),
        "ix_comments_company_id": ("company_id",),
        "ix_comments_entity": ("company_id", "entity_type", "entity_id", "created_at"),
    },
    "comment_mentions": {
        "ix_comment_mentions_comment_id": ("comment_id",),
        "ix_comment_mentions_user_id": ("user_id",),
        "ix_comment_mentions_company_id": ("company_id",),
    },
    "entity_watchers": {
        "ix_entity_watchers_user_id": ("user_id",),
        "ix_entity_watchers_company_id": ("company_id",),
    },
}
EXPECTED_UNIQUES = {
    "comments": {},
    "comment_mentions": {"uq_comment_mentions_company_comment_user": ("company_id", "comment_id", "user_id")},
    "entity_watchers": {
        "uq_entity_watchers_company_entity_user": ("company_id", "entity_type", "entity_id", "user_id")
    },
}
EXPECTED_FKS = {
    "comments": {(("author_id",), "users"), (("company_id",), "companies")},
    "comment_mentions": {(("comment_id",), "comments"), (("user_id",), "users"), (("company_id",), "companies")},
    "entity_watchers": {(("user_id",), "users"), (("company_id",), "companies")},
}


def _module():
    spec = importlib.util.spec_from_file_location("_migtest_125", MIGRATION_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _script_directory() -> ScriptDirectory:
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))
    return ScriptDirectory.from_config(cfg)


# --------------------------------------------------------------------------- wiring


def test_single_head_and_revision_chain():
    scripts = _script_directory()
    assert len(scripts.get_heads()) == 1, scripts.get_heads()
    assert scripts.get_revision(REVISION).down_revision == DOWN_REVISION


def test_revision_id_is_the_file_stem_and_fits_alembic_version():
    module = _module()
    assert module.revision == REVISION == os.path.basename(MIGRATION_FILE)[:-3]
    assert module.down_revision == DOWN_REVISION
    assert len(REVISION) <= 32


# --------------------------------------------------------------------------- round trip

PRE_125 = (
    "CREATE TABLE companies (id INTEGER PRIMARY KEY, name TEXT)",
    "CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)",
    "INSERT INTO companies VALUES (1, 'Existing company'), (2, 'Other company')",
    "INSERT INTO users VALUES (1, 'Existing user'), (2, 'Other user')",
)
NOW = "2026-11-16 12:00:00"


@pytest.fixture
def scratch(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'comments-migration.db'}")
    with engine.begin() as connection:
        connection.execute(sa.text("PRAGMA foreign_keys=ON"))
        for statement in PRE_125:
            connection.execute(sa.text(statement))
        migration = _module()
        migration.op = Operations(MigrationContext.configure(connection))
        yield connection, migration
    engine.dispose()


def _indexes(connection, table):
    return {row["name"]: tuple(row["column_names"]) for row in sa.inspect(connection).get_indexes(table)}


def _uniques(connection, table):
    return {row["name"]: tuple(row["column_names"]) for row in sa.inspect(connection).get_unique_constraints(table)}


def _fks(connection, table):
    return {
        (tuple(fk["constrained_columns"]), fk["referred_table"])
        for fk in sa.inspect(connection).get_foreign_keys(table)
    }


def _assert_full_shape(connection):
    for table in TABLES:
        assert _indexes(connection, table) == EXPECTED_INDEXES[table], table
        assert _uniques(connection, table) == EXPECTED_UNIQUES[table], table
        assert _fks(connection, table) == EXPECTED_FKS[table], table
        assert all(fk["referred_columns"] == ["id"] for fk in sa.inspect(connection).get_foreign_keys(table))


def _seed(connection):
    connection.execute(
        sa.text(
            "INSERT INTO comments (id, entity_type, entity_id, author_id, body, created_at, updated_at, company_id) "
            "VALUES (1, 'work_order', 42, 1, 'Material is on the dock', :now, :now, 1)"
        ),
        {"now": NOW},
    )
    connection.execute(
        sa.text(
            "INSERT INTO comment_mentions (id, comment_id, user_id, created_at, company_id) VALUES (1, 1, 2, :now, 1)"
        ),
        {"now": NOW},
    )
    connection.execute(
        sa.text(
            "INSERT INTO entity_watchers (id, entity_type, entity_id, user_id, created_at, company_id) "
            "VALUES (1, 'work_order', 42, 1, :now, 1)"
        ),
        {"now": NOW},
    )


def test_upgrade_replay_downgrade_reupgrade(scratch):
    connection, migration = scratch
    migration.upgrade()
    _assert_full_shape(connection)
    _seed(connection)
    migration.upgrade()  # replay over the full schema is a no-op
    _assert_full_shape(connection)
    assert connection.execute(sa.text("SELECT body FROM comments")).scalar() == "Material is on the dock"
    # The mixin's server default is the quoted string 'false' on both bootstrap paths: SQLite stores it
    # verbatim (no boolean type), PostgreSQL casts it to false. Only a raw INSERT ever relies on it.
    assert connection.execute(sa.text("SELECT is_deleted FROM comments")).scalar() == "false"
    for table in TABLES:
        assert connection.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar() == 1

    migration.downgrade()
    migration.downgrade()  # a second downgrade is a no-op, not an error
    remaining = set(sa.inspect(connection).get_table_names())
    assert remaining == {"companies", "users"}
    assert connection.execute(sa.text("SELECT name FROM companies WHERE id = 1")).scalar() == "Existing company"
    assert connection.execute(sa.text("SELECT name FROM users WHERE id = 1")).scalar() == "Existing user"

    migration.upgrade()
    _assert_full_shape(connection)
    for table in TABLES:
        assert connection.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar() == 0


def test_each_missing_index_is_recovered_on_its_own(scratch):
    connection, migration = scratch
    migration.upgrade()
    _seed(connection)
    for table in TABLES:
        for index_name in EXPECTED_INDEXES[table]:
            connection.execute(sa.text(f"DROP INDEX {index_name}"))
            migration.upgrade()
            assert index_name in _indexes(connection, table)
    _assert_full_shape(connection)
    assert connection.execute(sa.text("SELECT count(*) FROM comment_mentions")).scalar() == 1


def test_a_partially_bootstrapped_table_gets_its_unique_and_foreign_keys(scratch):
    # The table exists (an older model ran create_all) without its business key and FKs.
    connection, migration = scratch
    connection.execute(
        sa.text(
            "CREATE TABLE comments (id INTEGER NOT NULL, entity_type VARCHAR(40) NOT NULL, entity_id INTEGER NOT NULL, "
            "author_id INTEGER NOT NULL, body TEXT NOT NULL, edited_at DATETIME, created_at DATETIME NOT NULL, "
            "updated_at DATETIME NOT NULL, is_deleted BOOLEAN DEFAULT 'false' NOT NULL, deleted_at DATETIME, "
            "deleted_by INTEGER, company_id INTEGER NOT NULL, PRIMARY KEY (id))"
        )
    )
    connection.execute(
        sa.text(
            "CREATE TABLE entity_watchers (id INTEGER NOT NULL, entity_type VARCHAR(40) NOT NULL, "
            "entity_id INTEGER NOT NULL, user_id INTEGER NOT NULL, created_at DATETIME NOT NULL, "
            "company_id INTEGER NOT NULL, PRIMARY KEY (id))"
        )
    )
    connection.execute(
        sa.text(
            "INSERT INTO entity_watchers (id, entity_type, entity_id, user_id, created_at, company_id) "
            "VALUES (7, 'work_order', 42, 1, :now, 1)"
        ),
        {"now": NOW},
    )
    migration.upgrade()
    _assert_full_shape(connection)
    # A recovered FK carries the name PostgreSQL would have generated for the unnamed one.
    recovered = {fk["name"] for fk in sa.inspect(connection).get_foreign_keys("entity_watchers")}
    assert recovered == {"entity_watchers_user_id_fkey", "entity_watchers_company_id_fkey"}
    assert connection.execute(sa.text("SELECT id FROM entity_watchers")).scalar() == 7
    migration.upgrade()  # and the recovered table replays cleanly
    _assert_full_shape(connection)


@pytest.mark.parametrize(
    "statement",
    [
        # One mention per (company, comment, user).
        "INSERT INTO comment_mentions (comment_id, user_id, created_at, company_id) VALUES (1, 2, :now, 1)",
        # One watch per (company, entity, user).
        "INSERT INTO entity_watchers (entity_type, entity_id, user_id, created_at, company_id) "
        "VALUES ('work_order', 42, 1, :now, 1)",
        # Required values.
        "INSERT INTO comments (entity_type, entity_id, author_id, body, created_at, updated_at) "
        "VALUES ('work_order', 42, 1, 'x', :now, :now)",
        "INSERT INTO comments (entity_type, entity_id, author_id, created_at, updated_at, company_id) "
        "VALUES ('work_order', 42, 1, :now, :now, 1)",
        "INSERT INTO comments (entity_id, author_id, body, created_at, updated_at, company_id) "
        "VALUES (42, 1, 'x', :now, :now, 1)",
        "INSERT INTO comments (entity_type, entity_id, author_id, body, updated_at, company_id) "
        "VALUES ('work_order', 42, 1, 'x', :now, 1)",
        "INSERT INTO entity_watchers (entity_type, entity_id, created_at, company_id) VALUES ('work_order', 42, :now, 1)",
        # Foreign keys.
        "INSERT INTO comments (entity_type, entity_id, author_id, body, created_at, updated_at, company_id) "
        "VALUES ('work_order', 42, 999, 'x', :now, :now, 1)",
        "INSERT INTO comments (entity_type, entity_id, author_id, body, created_at, updated_at, company_id) "
        "VALUES ('work_order', 42, 1, 'x', :now, :now, 999)",
        "INSERT INTO comment_mentions (comment_id, user_id, created_at, company_id) VALUES (999, 1, :now, 1)",
        "INSERT INTO entity_watchers (entity_type, entity_id, user_id, created_at, company_id) "
        "VALUES ('work_order', 42, 999, :now, 1)",
    ],
)
def test_constraints_are_enforced(scratch, statement):
    connection, migration = scratch
    migration.upgrade()
    _seed(connection)
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(sa.text(statement), {"now": NOW})


def test_the_business_keys_are_per_company_and_per_person(scratch):
    connection, migration = scratch
    migration.upgrade()
    _seed(connection)
    for statement in (
        # Another person mentioned in the same comment; the same person watching from another company.
        "INSERT INTO comment_mentions (comment_id, user_id, created_at, company_id) VALUES (1, 1, :now, 1)",
        "INSERT INTO entity_watchers (entity_type, entity_id, user_id, created_at, company_id) "
        "VALUES ('work_order', 42, 1, :now, 2)",
        "INSERT INTO entity_watchers (entity_type, entity_id, user_id, created_at, company_id) "
        "VALUES ('work_order', 42, 2, :now, 1)",
        # No CHECK on entity_type: enabling the next record type needs no migration.
        "INSERT INTO entity_watchers (entity_type, entity_id, user_id, created_at, company_id) "
        "VALUES ('purchase_order', 42, 1, :now, 1)",
    ):
        connection.execute(sa.text(statement), {"now": NOW})
    assert connection.execute(sa.text("SELECT count(*) FROM entity_watchers")).scalar() == 4


# --------------------------------------------------------------------------- lock-step


@pytest.mark.parametrize("table", TABLES)
def test_the_migrated_table_matches_the_model(scratch, table):
    connection, migration = scratch
    migration.upgrade()
    inspector = sa.inspect(connection)
    model = MODELS[table].__table__
    reflected = {column["name"]: column for column in inspector.get_columns(table)}
    assert set(reflected) == set(model.columns.keys())
    for column in model.columns:
        assert reflected[column.name]["nullable"] == column.nullable, column.name
        assert str(reflected[column.name]["type"]) == str(column.type), column.name
        if column.server_default is not None:
            assert str(reflected[column.name]["default"]).strip("'") == str(column.server_default.arg), column.name
        else:
            assert reflected[column.name]["default"] is None, column.name
    assert inspector.get_pk_constraint(table)["constrained_columns"] == ["id"]
    assert _indexes(connection, table) == {
        index.name: tuple(column.name for column in index.columns) for index in model.indexes
    }
    assert all(not index.unique for index in model.indexes)
    assert _uniques(connection, table) == {
        constraint.name: tuple(column.name for column in constraint.columns)
        for constraint in model.constraints
        if isinstance(constraint, sa.UniqueConstraint)
    }
    assert not [c for c in model.constraints if isinstance(c, sa.CheckConstraint)]
    assert _fks(connection, table) == {
        (tuple(fk.parent.name for fk in constraint.elements), constraint.referred_table.name)
        for constraint in model.foreign_key_constraints
    }
    # Unnamed on both sides, so PostgreSQL names them identically on either bootstrap path.
    assert {fk["name"] for fk in inspector.get_foreign_keys(table)} == {None}
    assert {constraint.name for constraint in model.foreign_key_constraints} == {None}


def test_the_model_declares_the_expected_names():
    for table, model in MODELS.items():
        assert {index.name: tuple(c.name for c in index.columns) for index in model.__table__.indexes} == (
            EXPECTED_INDEXES[table]
        )
    assert Comment.__table__.c.is_deleted.server_default.arg == "false"
    assert Comment.__table__.c.deleted_by.foreign_keys == set()
    assert next(iter(CommentMention.__table__.c.comment_id.foreign_keys)).ondelete is None


def test_security_sql_is_byte_identical_in_the_migration_and_the_model():
    migration = _module()
    for table in TABLES:
        assert migration.security_sql(table) == comment_models.security_sql(table)


# --------------------------------------------------------------------------- PostgreSQL posture


def _offline_sql(direction: str, migration=None) -> str:
    output = io.StringIO()
    migration = migration or _module()
    migration.op = Operations(
        MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output})
    )
    getattr(migration, direction)()
    return output.getvalue()


def _model_sql(model) -> str:
    statements = []
    engine = sa.create_mock_engine(
        "postgresql://",
        lambda statement, *args, **kwargs: statements.append(str(statement.compile(dialect=engine.dialect))),
    )
    model.__table__.create(engine, checkfirst=False)
    return "\n".join(statements)


def _assert_default_deny(sql: str, table: str) -> None:
    assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in sql
    for role in ("PUBLIC", "anon", "authenticated"):
        assert f"REVOKE ALL ON TABLE {table} FROM {role}" in sql
        assert f"REVOKE ALL ON SEQUENCE {table}_id_seq FROM {role}" in sql


def test_postgres_upgrade_and_model_bootstrap_are_default_deny():
    migration_sql = _offline_sql("upgrade")
    for table, model in MODELS.items():
        model_sql = _model_sql(model)
        for sql in (migration_sql, model_sql):
            _assert_default_deny(sql, table)
            for role in ("anon", "authenticated"):
                assert f"IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}')" in sql
            assert "CREATE POLICY" not in sql and "service_role" not in sql and "auth.uid()" not in sql
            assert "TIMESTAMP WITH TIME ZONE" in sql
    assert "UNIQUE (company_id, comment_id, user_id)" in migration_sql
    assert "UNIQUE (company_id, entity_type, entity_id, user_id)" in migration_sql
    assert "FOREIGN KEY(comment_id) REFERENCES comments (id)" in migration_sql
    assert "is_deleted BOOLEAN DEFAULT 'false' NOT NULL" in migration_sql
    assert (
        "CREATE INDEX ix_comments_entity ON comments (company_id, entity_type, entity_id, created_at)" in migration_sql
    )
    assert "CHECK" not in migration_sql  # no CHECK on entity_type, by design
    assert "SET LOCAL lock_timeout = '5s'" in migration_sql
    assert "INSERT INTO" not in migration_sql and "UPDATE " not in migration_sql
    # comments is created before comment_mentions, whose FK points at it.
    assert migration_sql.index("CREATE TABLE comments") < migration_sql.index("CREATE TABLE comment_mentions")


def test_security_sql_runs_even_when_create_all_already_built_the_tables():
    """RLS / REVOKE never sit under a table guard: on the create_all path every object already
    exists, every create no-ops, and the security DDL must still be issued."""
    migration = _module()
    migration._has_table = lambda table_name: True
    migration._has_index = lambda table_name, index_name: True
    migration._has_unique = lambda table_name, constraint_name: True
    migration._has_foreign_key = lambda table_name, columns, referred_table: True
    migration._offline = lambda: False
    sql = _offline_sql("upgrade", migration)
    assert "CREATE TABLE" not in sql and "CREATE INDEX" not in sql and "ADD CONSTRAINT" not in sql
    for table in TABLES:
        _assert_default_deny(sql, table)


def test_postgres_downgrade_drops_the_referencing_tables_first():
    sql = _offline_sql("downgrade")
    positions = [sql.index(f"DROP TABLE {table}") for table in ("comment_mentions", "entity_watchers", "comments")]
    assert positions == sorted(positions)
    assert "audit_log" not in sql and "DELETE" not in sql


# --------------------------------------------------------------------------- model behavior


def test_model_defaults_relationships_and_soft_delete(db_session, test_user, operator_user):
    comment = Comment(entity_type="work_order", entity_id=42, author_id=test_user.id, body="Ready", company_id=1)
    db_session.add(comment)
    db_session.flush()
    second = CommentMention(comment_id=comment.id, user_id=test_user.id, company_id=1)
    first = CommentMention(comment_id=comment.id, user_id=operator_user.id, company_id=1)
    db_session.add_all([first, second])
    db_session.add(EntityWatcher(entity_type="work_order", entity_id=42, user_id=test_user.id, company_id=1))
    db_session.flush()
    db_session.expire(comment, ["mentions"])
    assert comment.is_deleted is False and comment.edited_at is None
    assert comment.created_at.tzinfo == timezone.utc and comment.updated_at.tzinfo == timezone.utc
    assert comment.author.id == test_user.id
    assert [mention.id for mention in comment.mentions] == sorted([first.id, second.id])
    assert first.comment is comment and first.user.id == operator_user.id
    comment.soft_delete(test_user.id)
    db_session.flush()
    assert comment.is_deleted is True and comment.deleted_by == test_user.id
