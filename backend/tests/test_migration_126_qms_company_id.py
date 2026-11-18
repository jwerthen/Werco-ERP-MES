"""Coverage for 126_qms_company_id: ``company_id`` restored on ``qms_standards``, ``qms_clauses``
and ``qms_clause_evidence`` where a create_all + stamp bootstrap left it out.

Production built its QMS tables with ``create_all`` before the QMS models carried
``TenantMixin``, and was stamped past ``026_add_multi_tenancy``, so the three tables never got the
column the models have declared since ``59dbf26d``. 126 is the repair. What is load-bearing:

1. **No-op where the column exists.** Every ``create_all`` database, and every one that ran
   ``026``, already has it. There the revision may only READ: no ALTER, no UPDATE, nothing.
2. **The repair builds exactly create_all's shape.** On a production-shaped database (the
   models' tables minus ``company_id``, which is exactly production's column list) the result is
   object for object what ``create_all`` builds -- type, nullability, FK, index -- and the names
   the migration uses are the ones the model produces (lock-step).
3. **The backfill derives, never guesses.** A standard takes its creator's company (a platform
   principal's own company is no evidence), a clause its standard's, evidence its clause's. A row
   still unattributed is given the database's company only when exactly ONE company exists; with
   more, the revision refuses, naming the tables, counts and ids, and leaves those rows NULL.
4. **Idempotent, object by object**, including the operator remediation the refusal prescribes
   (set ``company_id`` by hand, re-run) and ``026``'s differently-named FK.
5. **The downgrade deliberately changes nothing** -- the column is part of revision 125's schema
   (see the migration docstring) -- and a re-upgrade is a no-op.
6. **The PostgreSQL DDL**, rendered offline: create_all's FK name, no ON DELETE, the refusal
   before NOT NULL, no Supabase role, no ``audit_log``.

All of it runs on scratch SQLite files. The same scenarios were run by hand on a throwaway
PostgreSQL 16 when the revision was written (repair, refusal and rollback, no-op, downgrade); that is
not a CI gate. ``verify_fresh_bootstrap_postgres`` (the non-blocking e2e.yml) runs this revision's
no-op path on Postgres with the rest of the chain.
"""

import importlib.util
import io
import logging
import os
import re
import shutil

import pytest
import sqlalchemy as sa

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from app.db.database import Base
from app.db.mixins import TenantMixin
from app.models.qms_standard import QMSClause, QMSClauseEvidence, QMSStandard

pytestmark = [pytest.mark.unit]

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIGRATION_FILE = os.path.join(BACKEND_DIR, "alembic", "versions", "126_qms_company_id.py")
REVISION = "126_qms_company_id"
DOWN_REVISION = "125_work_order_comments"

QMS_TABLES = ("qms_standards", "qms_clauses", "qms_clause_evidence")

# Statements that change a database. A no-op run may issue none of them.
_WRITE = re.compile(r"^\s*(ALTER|UPDATE|INSERT|DELETE|CREATE|DROP)\b", re.IGNORECASE)


def _module():
    spec = importlib.util.spec_from_file_location("_migtest_126", MIGRATION_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bound(connection):
    module = _module()
    module.op = Operations(MigrationContext.configure(connection))
    return module


def _script_directory() -> ScriptDirectory:
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))
    return ScriptDirectory.from_config(cfg)


# --------------------------------------------------------------------------- scratch databases


@pytest.fixture(scope="module")
def create_all_template(tmp_path_factory):
    """One ``create_all`` SQLite file per worker; every test copies it."""
    path = tmp_path_factory.mktemp("qms126") / "create_all.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    return path


@pytest.fixture
def scratch_engine(create_all_template, tmp_path):
    path = tmp_path / "scratch.db"
    shutil.copy(create_all_template, path)
    engine = sa.create_engine(f"sqlite:///{path}")
    yield engine
    engine.dispose()


@pytest.fixture
def reference_engine(create_all_template, tmp_path):
    """An untouched ``create_all`` database: the shape the repair must reproduce."""
    path = tmp_path / "reference.db"
    shutil.copy(create_all_template, path)
    engine = sa.create_engine(f"sqlite:///{path}")
    yield engine
    engine.dispose()


def _record_statements(engine):
    statements = []
    sa.event.listen(engine, "before_cursor_execute", lambda conn, cursor, stmt, *a: statements.append(stmt))
    return statements


def _writes(statements):
    return [stmt for stmt in statements if _WRITE.match(stmt)]


def _production_shaped_table(metadata, table_name, *, company_fk_name=None, company_nullable=None):
    """The model table minus ``company_id`` -- column for column, production's list (2026-11-18).

    With ``company_nullable`` set, ``company_id`` is put back with the given nullability, an FK
    (named ``company_fk_name`` if given) and its index: the partial / 026-shaped states.
    """
    source = Base.metadata.tables[table_name]
    columns = [column for column in source.columns if column.name != "company_id"]
    foreign_keys = [
        sa.ForeignKeyConstraint([column.name], [fk.target_fullname]) for column in columns for fk in column.foreign_keys
    ]
    extra = []
    if company_nullable is not None:
        extra.append(sa.Column("company_id", sa.Integer(), nullable=company_nullable, index=True))
        extra.append(sa.ForeignKeyConstraint(["company_id"], ["companies.id"], name=company_fk_name))
    return sa.Table(table_name, metadata, *[column._copy() for column in columns], *extra, *foreign_keys)


def _reproduce_production(connection, **company_column):
    """Swap the create_all QMS tables for production's shape (no ``company_id``)."""
    for table_name in reversed(QMS_TABLES):
        connection.execute(sa.text(f"DROP TABLE {table_name}"))
    metadata = sa.MetaData()
    metadata.reflect(bind=connection, only=["users", "documents", "companies"])
    tables = [_production_shaped_table(metadata, name, **company_column) for name in QMS_TABLES]
    metadata.create_all(connection, tables=tables)


def _companies(connection, *ids):
    for company_id in ids:
        connection.execute(
            sa.text(
                "INSERT INTO companies (id, name, slug, is_active, allow_ai_egress, allow_sms_egress) "
                "VALUES (:id, :name, :slug, 1, 0, 0)"
            ),
            {"id": company_id, "name": f"Company {company_id}", "slug": f"company-{company_id}"},
        )


def _user(connection, user_id, company_id, *, role="QUALITY", superuser=False):
    connection.execute(
        sa.text(
            "INSERT INTO users (id, company_id, employee_id, email, hashed_password, first_name, last_name, "
            "role, is_superuser) VALUES (:id, :company, :emp, :email, 'x', 'Test', 'User', :role, :su)"
        ),
        {
            "id": user_id,
            "company": company_id,
            "emp": f"E{user_id}",
            "email": f"user{user_id}@example.test",
            "role": role,
            "su": superuser,
        },
    )


def _standard(connection, standard_id, created_by, *, deleted=False, company_id=None):
    columns = "id, name, created_by, is_deleted"
    values = ":id, :name, :by, :deleted"
    params = {"id": standard_id, "name": f"Standard {standard_id}", "by": created_by, "deleted": deleted}
    if company_id is not None:
        columns += ", company_id"
        values += ", :company"
        params["company"] = company_id
    connection.execute(sa.text(f"INSERT INTO qms_standards ({columns}) VALUES ({values})"), params)


def _clause(connection, clause_id, standard_id, *, parent=None, deleted=False, company_id=None):
    columns = "id, standard_id, clause_number, title, parent_clause_id, is_deleted"
    values = ":id, :standard, :number, 'Clause', :parent, :deleted"
    params = {
        "id": clause_id,
        "standard": standard_id,
        "number": f"8.{clause_id}",
        "parent": parent,
        "deleted": deleted,
    }
    if company_id is not None:
        columns += ", company_id"
        values += ", :company"
        params["company"] = company_id
    connection.execute(sa.text(f"INSERT INTO qms_clauses ({columns}) VALUES ({values})"), params)


def _evidence(connection, evidence_id, clause_id, *, created_by=None, auto=False, company_id=None):
    columns = "id, clause_id, evidence_type, title, created_by, is_auto_linked, is_deleted"
    values = ":id, :clause, 'document', 'Evidence', :by, :auto, 0"
    params = {"id": evidence_id, "clause": clause_id, "by": created_by, "auto": auto}
    if company_id is not None:
        columns += ", company_id"
        values += ", :company"
        params["company"] = company_id
    connection.execute(sa.text(f"INSERT INTO qms_clause_evidence ({columns}) VALUES ({values})"), params)


def _companies_by_id(connection, table_name):
    return dict(connection.execute(sa.text(f"SELECT id, company_id FROM {table_name} ORDER BY id")).all())


def _shape(connection, table_name):
    inspector = sa.inspect(connection)
    return {
        "columns": [
            (c["name"], str(c["type"]), c["nullable"], c.get("default")) for c in inspector.get_columns(table_name)
        ],
        "pk": inspector.get_pk_constraint(table_name)["constrained_columns"],
        "fks": sorted(
            (tuple(fk["constrained_columns"]), fk["referred_table"], tuple(fk["referred_columns"]), fk.get("name"))
            for fk in inspector.get_foreign_keys(table_name)
        ),
        "indexes": sorted(
            (ix["name"], tuple(ix["column_names"]), bool(ix["unique"])) for ix in inspector.get_indexes(table_name)
        ),
        "uniques": sorted(uq["name"] or "" for uq in inspector.get_unique_constraints(table_name)),
        "checks": sorted(ck["sqltext"] for ck in inspector.get_check_constraints(table_name)),
    }


def _company_fks(connection, table_name):
    return [
        fk
        for fk in sa.inspect(connection).get_foreign_keys(table_name)
        if fk["constrained_columns"] == ["company_id"] and fk["referred_table"] == "companies"
    ]


def _company_column(connection, table_name):
    return next(c for c in sa.inspect(connection).get_columns(table_name) if c["name"] == "company_id")


# --------------------------------------------------------------------------- wiring


def test_single_head_and_revision_chain():
    scripts = _script_directory()
    assert len(scripts.get_heads()) == 1, scripts.get_heads()
    assert scripts.get_revision(REVISION).down_revision == DOWN_REVISION


def test_revision_id_is_the_file_stem_and_fits_alembic_version():
    module = _module()
    assert module.revision == REVISION == os.path.basename(MIGRATION_FILE)[:-3]
    assert len(REVISION) <= 32


# --------------------------------------------------------------------------- lock-step with the models


def test_the_three_tables_are_the_tenant_mixin_qms_models_and_the_backfill_path_exists():
    module = _module()
    models = (QMSStandard, QMSClause, QMSClauseEvidence)
    assert tuple(model.__tablename__ for model in models) == module.TABLES == QMS_TABLES
    assert all(issubclass(model, TenantMixin) for model in models)
    # The derivation follows the models' own foreign keys, parent first.
    assert {fk.target_fullname for fk in QMSStandard.__table__.c.created_by.foreign_keys} == {"users.id"}
    assert {fk.target_fullname for fk in QMSClause.__table__.c.standard_id.foreign_keys} == {"qms_standards.id"}
    assert {fk.target_fullname for fk in QMSClauseEvidence.__table__.c.clause_id.foreign_keys} == {"qms_clauses.id"}
    assert QMSClause.__table__.c.standard_id.nullable is False
    assert QMSClauseEvidence.__table__.c.clause_id.nullable is False


@pytest.mark.parametrize("model", [QMSStandard, QMSClause, QMSClauseEvidence])
def test_the_restored_column_is_exactly_tenant_mixins(model):
    module = _module()
    table = model.__table__
    column = table.c.company_id
    assert isinstance(column.type, sa.Integer) and column.nullable is False
    assert column.server_default is None and column.index is True
    (fk,) = column.foreign_keys
    assert fk.target_fullname == "companies.id"
    # Unnamed on the model (PostgreSQL names it <table>_company_id_fkey), no referential action.
    assert fk.name is None and fk.ondelete is None and fk.onupdate is None and not fk.deferrable
    (index,) = [ix for ix in table.indexes if [c.name for c in ix.columns] == ["company_id"]]
    assert index.name == module.index_name(table.name) == f"ix_{table.name}_company_id"
    assert index.unique is False
    assert module.fk_name(table.name) == f"{table.name}_company_id_fkey"


@pytest.mark.parametrize("model", [QMSStandard, QMSClause, QMSClauseEvidence])
def test_create_all_postgres_ddl_matches_the_migration(model):
    statements = []
    engine = sa.create_mock_engine(
        "postgresql://", lambda statement, *a, **k: statements.append(str(statement.compile(dialect=engine.dialect)))
    )
    model.__table__.create(engine, checkfirst=False)
    ddl = " ".join(" ".join(statements).split())
    table_name = model.__tablename__
    assert "company_id INTEGER NOT NULL" in ddl
    # Unnamed in CREATE TABLE: PostgreSQL's default name is <table>_company_id_fkey.
    assert "FOREIGN KEY(company_id) REFERENCES companies (id)" in ddl
    # No "CONSTRAINT <name>" between the previous comma and the clause: the FK is not named.
    assert "CONSTRAINT" not in ddl.split("FOREIGN KEY(company_id)")[0].rsplit(",", 1)[-1]
    assert f"CREATE INDEX ix_{table_name}_company_id ON {table_name} (company_id)" in ddl


# --------------------------------------------------------------------------- 1. no-op where the column exists


def test_a_create_all_database_is_untouched_in_both_directions(scratch_engine, reference_engine):
    with scratch_engine.begin() as connection:
        _companies(connection, 1, 2)
        _user(connection, 10, 1)
        _standard(connection, 1, 10, company_id=2)  # deliberately NOT its creator's company
        _clause(connection, 1, 1, company_id=2)
        _evidence(connection, 1, 1, company_id=2)
        before = {name: (_shape(connection, name), _companies_by_id(connection, name)) for name in QMS_TABLES}

        statements = _record_statements(scratch_engine)
        _bound(connection).upgrade()
        _bound(connection).downgrade()
        assert _writes(statements) == []
        # A value already set is never re-derived: the standard keeps company 2.
        assert {name: (_shape(connection, name), _companies_by_id(connection, name)) for name in QMS_TABLES} == before
    with reference_engine.connect() as reference:
        for name in QMS_TABLES:
            assert before[name][0] == _shape(reference, name)


def test_a_026_named_foreign_key_counts_as_present(scratch_engine):
    """026 named the FK ``fk_<table>_company_id``; the repair must not add create_all's twin."""
    with scratch_engine.begin() as connection:
        for name in reversed(QMS_TABLES):
            connection.execute(sa.text(f"DROP TABLE {name}"))
        metadata = sa.MetaData()
        metadata.reflect(bind=connection, only=["users", "documents", "companies"])
        tables = [
            _production_shaped_table(metadata, name, company_fk_name=f"fk_{name}_company_id", company_nullable=False)
            for name in QMS_TABLES
        ]
        metadata.create_all(connection, tables=tables)
        statements = _record_statements(scratch_engine)
        _bound(connection).upgrade()
        assert _writes(statements) == []
        for name in QMS_TABLES:
            assert [fk["name"] for fk in _company_fks(connection, name)] == [f"fk_{name}_company_id"]


# --------------------------------------------------------------------------- 2-3. repair + backfill


def test_repairs_a_production_shaped_database_to_create_alls_shape(scratch_engine, reference_engine, caplog):
    caplog.set_level(logging.INFO, logger="alembic.runtime.migration")
    with scratch_engine.begin() as connection:
        _reproduce_production(connection)
        for name in QMS_TABLES:
            assert "company_id" not in {c["name"] for c in sa.inspect(connection).get_columns(name)}
        _companies(connection, 1, 2)
        _user(connection, 10, 1, role="ADMIN")
        _user(connection, 20, 2, role="QUALITY")
        _standard(connection, 1, 10)
        _standard(connection, 2, 20)
        _standard(connection, 3, 20, deleted=True)  # soft-deleted rows are attributed too
        _clause(connection, 1, 1)
        _clause(connection, 2, 1, parent=1)
        _clause(connection, 3, 2)
        _clause(connection, 4, 3, deleted=True)
        # Evidence follows its clause, never its own creator (auto-linked rows have none).
        _evidence(connection, 1, 2, created_by=20)
        _evidence(connection, 2, 3, auto=True)
        _evidence(connection, 3, 4)

        _bound(connection).upgrade()

        assert _companies_by_id(connection, "qms_standards") == {1: 1, 2: 2, 3: 2}
        assert _companies_by_id(connection, "qms_clauses") == {1: 1, 2: 1, 3: 2, 4: 2}
        assert _companies_by_id(connection, "qms_clause_evidence") == {1: 1, 2: 2, 3: 2}
        repaired = {name: _shape(connection, name) for name in QMS_TABLES}
    with reference_engine.connect() as reference:
        for name in QMS_TABLES:
            assert repaired[name] == _shape(reference, name), name
    assert "qms_standards -- 3 row(s) attributed by derivation, 0 to the sole company" in caplog.text
    assert "qms_clause_evidence -- 3 row(s) attributed by derivation, 0 to the sole company" in caplog.text


def test_the_restored_column_is_enforced(scratch_engine):
    with scratch_engine.begin() as connection:
        _reproduce_production(connection)
        _companies(connection, 1)
        _bound(connection).upgrade()
        nested = connection.begin_nested()
        with pytest.raises(sa.exc.IntegrityError):
            _standard(connection, 1, None)  # company_id NOT NULL
        nested.rollback()
        for name in QMS_TABLES:
            assert _company_column(connection, name)["nullable"] is False


def test_a_platform_principals_own_company_is_no_evidence(scratch_engine):
    """Multi-company: a standard whose only link is a platform principal is not attributed."""
    with scratch_engine.begin() as connection:
        _reproduce_production(connection)
        _companies(connection, 1, 2)
        _user(connection, 10, 1)
        _user(connection, 30, 1, role="PLATFORM_ADMIN")
        _user(connection, 31, 1, role="platform_admin")  # the enum may carry the values (026)
        _user(connection, 32, 1, role="ADMIN", superuser=True)
        _standard(connection, 1, 10)
        _standard(connection, 2, 30)
        _standard(connection, 3, 31)
        _standard(connection, 4, 32)
        with pytest.raises(Exception) as refused:
            _bound(connection).upgrade()
        assert type(refused.value).__name__ == "UnattributedQmsRowsError"
        assert "qms_standards: 3 row(s) without a company, ids [2, 3, 4]" in str(refused.value)
        # The derivable standard was derived; the others were NOT given the platform user's company.
        assert _companies_by_id(connection, "qms_standards") == {1: 1, 2: None, 3: None, 4: None}


def test_a_multi_company_database_refuses_instead_of_guessing(scratch_engine):
    with scratch_engine.begin() as connection:
        _reproduce_production(connection)
        _companies(connection, 1, 2)
        _user(connection, 10, 1)
        _standard(connection, 1, 10)
        _standard(connection, 2, None)  # no creator recorded
        _standard(connection, 3, 999)  # creator id that matches no user
        _clause(connection, 1, 1)
        _clause(connection, 2, 2)
        _clause(connection, 3, 3)
        _evidence(connection, 1, 1)
        _evidence(connection, 2, 2)
        _evidence(connection, 3, 3)

        with pytest.raises(Exception) as refused:
            _bound(connection).upgrade()
        message = str(refused.value)
        assert type(refused.value).__name__ == "UnattributedQmsRowsError"
        assert isinstance(refused.value, RuntimeError)
        assert "refusing to guess which company owns 6 QMS row(s)" in message
        assert "qms_standards: 2 row(s) without a company, ids [2, 3]" in message
        assert "qms_clauses: 2 row(s) without a company, ids [2, 3]" in message
        assert "qms_clause_evidence: 2 row(s) without a company, ids [2, 3]" in message
        assert "This database holds 2 companies." in message
        assert "Standard 2" not in message  # ids only, never content
        # Nothing was guessed: only the attributable chain got a company. (SQLite has no
        # transactional DDL, so the added nullable columns survive here; PostgreSQL rolls back.)
        assert _companies_by_id(connection, "qms_standards") == {1: 1, 2: None, 3: None}
        assert _companies_by_id(connection, "qms_clauses") == {1: 1, 2: None, 3: None}
        assert _companies_by_id(connection, "qms_clause_evidence") == {1: 1, 2: None, 3: None}
        for name in QMS_TABLES:
            assert _company_column(connection, name)["nullable"] is True
            assert f"ix_{name}_company_id" not in {ix["name"] for ix in sa.inspect(connection).get_indexes(name)}

        # The remediation the refusal prescribes: a person records the owner, then re-runs. The
        # recorded value is kept, and the children inherit it.
        connection.execute(sa.text("UPDATE qms_standards SET company_id = 2 WHERE id IN (2, 3)"))
        _bound(connection).upgrade()
        assert _companies_by_id(connection, "qms_standards") == {1: 1, 2: 2, 3: 2}
        assert _companies_by_id(connection, "qms_clauses") == {1: 1, 2: 2, 3: 2}
        assert _companies_by_id(connection, "qms_clause_evidence") == {1: 1, 2: 2, 3: 2}
        for name in QMS_TABLES:
            assert _company_column(connection, name)["nullable"] is False


def test_the_refusal_lists_at_most_twenty_ids_and_the_exact_count(scratch_engine):
    with scratch_engine.begin() as connection:
        _reproduce_production(connection)
        _companies(connection, 1, 2)
        for standard_id in range(1, 26):
            _standard(connection, standard_id, None)
        with pytest.raises(Exception) as refused:
            _bound(connection).upgrade()
        assert f"qms_standards: 25 row(s) without a company, ids {list(range(1, 21))} (first 20 listed)" in str(
            refused.value
        )


def test_a_single_company_database_assigns_its_only_company(scratch_engine, caplog):
    caplog.set_level(logging.INFO, logger="alembic.runtime.migration")
    with scratch_engine.begin() as connection:
        _reproduce_production(connection)
        _companies(connection, 7)
        _user(connection, 10, 7)
        _user(connection, 30, 7, role="PLATFORM_ADMIN")
        _standard(connection, 1, 10)
        _standard(connection, 2, None)
        _standard(connection, 3, 30)
        _clause(connection, 1, 2)
        _evidence(connection, 1, 1)
        _bound(connection).upgrade()
        assert _companies_by_id(connection, "qms_standards") == {1: 7, 2: 7, 3: 7}
        assert _companies_by_id(connection, "qms_clauses") == {1: 7}
        assert _companies_by_id(connection, "qms_clause_evidence") == {1: 7}
    # Visible in the deploy log: how many rows took the one-company fallback.
    assert "qms_standards -- 1 row(s) attributed by derivation, 2 to the sole company" in caplog.text
    assert "qms_clauses -- 1 row(s) attributed by derivation, 0 to the sole company" in caplog.text


def test_an_empty_production_shaped_database_is_repaired(scratch_engine, reference_engine):
    """Production's actual state: the column missing and zero rows."""
    with scratch_engine.begin() as connection:
        _reproduce_production(connection)
        _bound(connection).upgrade()
        repaired = {name: _shape(connection, name) for name in QMS_TABLES}
    with reference_engine.connect() as reference:
        assert repaired == {name: _shape(reference, name) for name in QMS_TABLES}


# --------------------------------------------------------------------------- 4. idempotence


def test_a_second_run_after_the_repair_writes_nothing(scratch_engine):
    with scratch_engine.begin() as connection:
        _reproduce_production(connection)
        _companies(connection, 1, 2)
        _user(connection, 10, 1)
        _standard(connection, 1, 10)
        _clause(connection, 1, 1)
        _bound(connection).upgrade()
        before = {name: (_shape(connection, name), _companies_by_id(connection, name)) for name in QMS_TABLES}
        statements = _record_statements(scratch_engine)
        _bound(connection).upgrade()
        assert _writes(statements) == []
        assert {name: (_shape(connection, name), _companies_by_id(connection, name)) for name in QMS_TABLES} == before


def test_each_missing_object_is_restored_on_its_own(scratch_engine):
    with scratch_engine.begin() as connection:
        # The column exists but is nullable, has no FK and no index (an interrupted hand repair),
        # and one row already carries an operator's value that derivation would contradict.
        _reproduce_production(connection)
        _companies(connection, 1, 2)
        _user(connection, 10, 1)
        for name in QMS_TABLES:
            connection.execute(sa.text(f"ALTER TABLE {name} ADD COLUMN company_id INTEGER"))
        _standard(connection, 1, 10, company_id=2)
        _standard(connection, 2, 10)
        _clause(connection, 1, 1)
        _clause(connection, 2, 2)

        _bound(connection).upgrade()
        assert _companies_by_id(connection, "qms_standards") == {1: 2, 2: 1}
        assert _companies_by_id(connection, "qms_clauses") == {1: 2, 2: 1}
        for name in QMS_TABLES:
            assert _company_column(connection, name)["nullable"] is False
            # SQLite can only add it by a batch rebuild, which refuses an unnamed constraint: it
            # carries PostgreSQL's name (the 125 precedent). No create_all database is in this state.
            assert [fk["name"] for fk in _company_fks(connection, name)] == [f"{name}_company_id_fkey"]
            assert f"ix_{name}_company_id" in {ix["name"] for ix in sa.inspect(connection).get_indexes(name)}

        # A dropped index alone comes back; nothing else moves.
        connection.execute(sa.text("DROP INDEX ix_qms_clauses_company_id"))
        statements = _record_statements(scratch_engine)
        _bound(connection).upgrade()
        assert [s.split("(")[0].strip() for s in _writes(statements)] == [
            "CREATE INDEX ix_qms_clauses_company_id ON qms_clauses"
        ]


# --------------------------------------------------------------------------- 5. downgrade


def test_the_downgrade_keeps_the_column_and_a_reupgrade_is_a_no_op(scratch_engine, reference_engine):
    with scratch_engine.begin() as connection:
        _reproduce_production(connection)
        _companies(connection, 1, 2)
        _user(connection, 10, 1)
        _standard(connection, 1, 10)
        _clause(connection, 1, 1)
        _evidence(connection, 1, 1)
        _bound(connection).upgrade()
        after_upgrade = {name: (_shape(connection, name), _companies_by_id(connection, name)) for name in QMS_TABLES}

        statements = _record_statements(scratch_engine)
        _bound(connection).downgrade()
        _bound(connection).downgrade()  # twice: still nothing
        assert statements == []
        assert {
            name: (_shape(connection, name), _companies_by_id(connection, name)) for name in QMS_TABLES
        } == after_upgrade

        _bound(connection).upgrade()
        assert _writes(statements) == []
    with reference_engine.connect() as reference:
        for name in QMS_TABLES:
            assert after_upgrade[name][0] == _shape(reference, name)


# --------------------------------------------------------------------------- 6. PostgreSQL DDL


def _render_postgres(direction):
    output = io.StringIO()
    module = _module()
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output})
    module.op = Operations(context)
    getattr(module, direction)()
    return " ".join(output.getvalue().split())


def test_postgres_upgrade_sql_is_the_repair_in_order():
    sql = _render_postgres("upgrade")
    expected_in_order = [
        "SET LOCAL lock_timeout = '5s'",
        "ALTER TABLE qms_standards ADD COLUMN company_id INTEGER",
        "ALTER TABLE qms_clauses ADD COLUMN company_id INTEGER",
        "ALTER TABLE qms_clause_evidence ADD COLUMN company_id INTEGER",
        "UPDATE qms_standards SET company_id = (SELECT u.company_id FROM users u WHERE u.id = "
        "qms_standards.created_by AND COALESCE(u.is_superuser, FALSE) = FALSE AND UPPER(CAST(u.role AS "
        "VARCHAR(32))) <> 'PLATFORM_ADMIN') WHERE company_id IS NULL AND (SELECT u.company_id FROM users u "
        "WHERE u.id = qms_standards.created_by AND COALESCE(u.is_superuser, FALSE) = FALSE AND "
        "UPPER(CAST(u.role AS VARCHAR(32))) <> 'PLATFORM_ADMIN') IS NOT NULL",
        "UPDATE qms_standards SET company_id = (SELECT MIN(id) FROM companies) WHERE company_id IS NULL "
        "AND (SELECT COUNT(*) FROM companies) = 1",
        "UPDATE qms_clauses SET company_id = (SELECT s.company_id FROM qms_standards s WHERE s.id = "
        "qms_clauses.standard_id) WHERE company_id IS NULL AND (SELECT s.company_id FROM qms_standards s "
        "WHERE s.id = qms_clauses.standard_id) IS NOT NULL",
        "UPDATE qms_clauses SET company_id = (SELECT MIN(id) FROM companies) WHERE company_id IS NULL "
        "AND (SELECT COUNT(*) FROM companies) = 1",
        "UPDATE qms_clause_evidence SET company_id = (SELECT c.company_id FROM qms_clauses c WHERE c.id = "
        "qms_clause_evidence.clause_id) WHERE company_id IS NULL AND (SELECT c.company_id FROM qms_clauses c "
        "WHERE c.id = qms_clause_evidence.clause_id) IS NOT NULL",
        "UPDATE qms_clause_evidence SET company_id = (SELECT MIN(id) FROM companies) WHERE company_id IS NULL "
        "AND (SELECT COUNT(*) FROM companies) = 1",
        "RAISE EXCEPTION '126_qms_company_id: QMS rows have no attributable company.",
        "ALTER TABLE qms_standards ALTER COLUMN company_id SET NOT NULL",
        "ALTER TABLE qms_standards ADD CONSTRAINT qms_standards_company_id_fkey FOREIGN KEY(company_id) "
        "REFERENCES companies (id)",
        "CREATE INDEX ix_qms_standards_company_id ON qms_standards (company_id)",
        "ALTER TABLE qms_clauses ALTER COLUMN company_id SET NOT NULL",
        "ALTER TABLE qms_clauses ADD CONSTRAINT qms_clauses_company_id_fkey FOREIGN KEY(company_id) "
        "REFERENCES companies (id)",
        "CREATE INDEX ix_qms_clauses_company_id ON qms_clauses (company_id)",
        "ALTER TABLE qms_clause_evidence ALTER COLUMN company_id SET NOT NULL",
        "ALTER TABLE qms_clause_evidence ADD CONSTRAINT qms_clause_evidence_company_id_fkey FOREIGN "
        "KEY(company_id) REFERENCES companies (id)",
        "CREATE INDEX ix_qms_clause_evidence_company_id ON qms_clause_evidence (company_id)",
        "SET LOCAL lock_timeout = DEFAULT",
    ]
    position = 0
    for fragment in expected_in_order:
        found = sql.find(fragment, position)
        assert found >= 0, f"missing or out of order: {fragment}"
        position = found + len(fragment)
    # create_all's FK has no referential action, and none is added here.
    assert "ON DELETE" not in sql and "ON UPDATE" not in sql and "DEFERRABLE" not in sql
    # No table, no RLS, no Supabase role, nothing on the audit log, nothing dropped.
    lowered = sql.lower()
    for forbidden in ("create table", "row level security", "revoke", "audit_log", "drop ", "delete "):
        assert forbidden not in lowered, forbidden
    for role in ("anon", "authenticated", "service_role"):
        assert not re.search(rf"\b{role}\b", lowered), role


def test_postgres_downgrade_renders_nothing():
    assert _render_postgres("downgrade") == ""
