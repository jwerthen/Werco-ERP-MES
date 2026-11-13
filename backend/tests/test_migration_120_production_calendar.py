"""Migration 120: the production calendar tables + ``oee_records.shift_id``, pinned to the models.

Additive configuration, no data: four new tenant tables (no row seeded -- an
unconfigured company keeps local-midnight days and no shifts) and a nullable FK column on
``oee_records`` with NO backfill (legacy free-text ``shift`` labels stay verbatim). Every
object is guarded on its own; RLS / REVOKE run outside the guards, naming the Supabase
roles only under ``pg_roles`` checks; the SQLite batch rebuild of ``oee_records`` keeps
migration 063's ``COALESCE(shift, '')`` expression index (reflection cannot see it).
"""

import importlib.util
import io
from datetime import datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.models.oee import OEERecord
from app.models.production_calendar import (
    ProductionCalendarSettings,
    ShiftDefinition,
    ShiftPattern,
    ShiftPatternWorkCenter,
)

VERSIONS = Path(__file__).parents[1] / "alembic/versions"
FILENAME = "120_production_calendar.py"
NEW_MODELS = (ProductionCalendarSettings, ShiftPattern, ShiftPatternWorkCenter, ShiftDefinition)
pytestmark = [pytest.mark.unit]

NOW = datetime(2026, 11, 13, 12, 0)


def module():
    spec = importlib.util.spec_from_file_location(FILENAME.removesuffix(".py"), VERSIONS / FILENAME)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def _index_names(connection, table):
    rows = connection.execute(
        sa.text("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = :t"), {"t": table}
    )
    return {row[0] for row in rows}


@pytest.fixture
def local_migration(tmp_path):
    engine = sa.create_engine(f'sqlite:///{tmp_path / "production-calendar-migration.db"}')
    with engine.begin() as connection:
        connection.execute(sa.text("PRAGMA foreign_keys=ON"))
        connection.execute(sa.text("CREATE TABLE companies (id INTEGER PRIMARY KEY, name TEXT)"))
        connection.execute(sa.text("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)"))
        connection.execute(sa.text("CREATE TABLE work_centers (id INTEGER PRIMARY KEY, code TEXT)"))
        connection.execute(sa.text("INSERT INTO companies VALUES (1, 'A'), (2, 'B')"))
        connection.execute(sa.text("INSERT INTO users VALUES (1, 'Admin')"))
        connection.execute(sa.text("INSERT INTO work_centers VALUES (10, 'WLD-01'), (11, 'WLD-02'), (20, 'LASER')"))
        # The pre-120 shape of oee_records that matters here, with 063's expression index.
        connection.execute(sa.text("""
                CREATE TABLE oee_records (
                    id INTEGER PRIMARY KEY,
                    company_id INTEGER NOT NULL REFERENCES companies (id),
                    work_center_id INTEGER NOT NULL REFERENCES work_centers (id),
                    record_date DATE NOT NULL,
                    shift VARCHAR(50)
                )
                """))
        connection.execute(sa.text("CREATE INDEX ix_oee_records_record_date ON oee_records (record_date)"))
        connection.execute(sa.text("""
                CREATE UNIQUE INDEX uq_oee_company_wc_date_shift
                ON oee_records (company_id, work_center_id, record_date, COALESCE(shift, ''))
                """))
        connection.execute(sa.text("INSERT INTO oee_records VALUES (1, 1, 10, '2026-06-04', '2nd')"))
        migration = module()
        migration.op = Operations(MigrationContext.configure(connection))
        migration.upgrade()
        yield connection, migration
    engine.dispose()


def table(connection, name):
    return sa.Table(name, sa.MetaData(), autoload_with=connection)


def _seed_pattern(connection, *, pattern_id=1, company_id=1, name="Weld shop", is_default=False):
    connection.execute(
        table(connection, "shift_patterns")
        .insert()
        .values(
            id=pattern_id,
            company_id=company_id,
            name=name,
            is_default=is_default,
            sort_order=0,
            created_at=NOW,
            updated_at=NOW,
        )
    )


def _shift(**changes):
    return {
        "company_id": 1,
        "shift_pattern_id": 1,
        "code": "W2",
        "name": "Weld 2nd",
        "start_minute": 930,
        "end_minute": 1560,
        "days_mask": 0b0011111,
        "planned_minutes": 600,
        "sort_order": 0,
        "is_active": True,
        "created_at": NOW,
        "updated_at": NOW,
    } | changes


def test_revision_chain_is_the_assigned_one():
    migration = module()
    assert migration.revision == "120_production_calendar"
    assert migration.down_revision == "118_dashboard_layouts"
    assert len(migration.revision) <= 32


def test_upgrade_adds_tables_and_column_without_data_and_keeps_the_063_index(local_migration):
    connection, migration = local_migration
    inspector = sa.inspect(connection)
    for model in NEW_MODELS:
        assert inspector.has_table(model.__tablename__)
        assert connection.execute(sa.text(f"SELECT count(*) FROM {model.__tablename__}")).scalar() == 0
    # The legacy record keeps its label; shift_id is NULL (no backfill).
    assert connection.execute(sa.text("SELECT shift, shift_id FROM oee_records")).one() == ("2nd", None)
    assert {"uq_oee_company_wc_date_shift", "ix_oee_records_shift_id", "ix_oee_records_record_date"} <= _index_names(
        connection, "oee_records"
    )
    fks = {fk["name"]: fk for fk in inspector.get_foreign_keys("oee_records")}
    assert fks["fk_oee_records_shift_id"]["referred_table"] == "shift_definitions"
    # The expression key still guards (WC, date, label) after the batch rebuild.
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(
            sa.text(
                "INSERT INTO oee_records (company_id, work_center_id, record_date, shift) "
                "VALUES (1, 10, '2026-06-04', '2nd')"
            )
        )


def test_replay_downgrade_and_reupgrade(local_migration):
    connection, migration = local_migration
    migration.upgrade()  # replay is a no-op
    for index in ("ix_shift_definitions_company_id", "uq_shift_patterns_company_default", "ix_oee_records_shift_id"):
        connection.execute(sa.text(f"DROP INDEX {index}"))
        migration.upgrade()  # a partial bootstrap recovers each missing index
        assert index in _index_names(connection, "shift_definitions") | _index_names(
            connection, "shift_patterns"
        ) | _index_names(connection, "oee_records")
    migration.downgrade()
    migration.downgrade()  # a second downgrade is a no-op
    inspector = sa.inspect(connection)
    for model in NEW_MODELS:
        assert not inspector.has_table(model.__tablename__)
    assert "shift_id" not in {c["name"] for c in inspector.get_columns("oee_records")}
    assert "uq_oee_company_wc_date_shift" in _index_names(connection, "oee_records")
    assert connection.execute(sa.text("SELECT shift FROM oee_records")).scalar() == "2nd"
    migration.upgrade()
    assert connection.execute(sa.text("SELECT shift_id FROM oee_records")).scalar() is None


def test_constraints(local_migration):
    connection, _ = local_migration
    _seed_pattern(connection)
    shifts = table(connection, "shift_definitions")
    connection.execute(shifts.insert().values(**_shift()))
    for bad in (
        {"code": "X1", "start_minute": 1440},
        {"code": "X2", "start_minute": -1},
        {"code": "X3", "end_minute": 930},
        {"code": "X4", "end_minute": 930 + 1441},
        {"code": "X5", "days_mask": 0},
        {"code": "X6", "days_mask": 128},
        {"code": "X7", "planned_minutes": 0},
        {"code": "X8", "planned_minutes": 631},
        {"code": "W2"},  # unique per company
        {"code": "X9", "shift_pattern_id": 99},
    ):
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(shifts.insert().values(**_shift(**bad)))
    connection.execute(shifts.insert().values(**_shift(company_id=2)))  # same code, other company

    # One default pattern per company; a WC in one pattern per company.
    _seed_pattern(connection, pattern_id=2, name="Rest of shop", is_default=True)
    with pytest.raises(IntegrityError), connection.begin_nested():
        _seed_pattern(connection, pattern_id=3, name="Third", is_default=True)
    _seed_pattern(connection, pattern_id=4, company_id=2, name="Rest of shop", is_default=True)
    members = table(connection, "shift_pattern_work_centers")
    connection.execute(members.insert().values(company_id=1, shift_pattern_id=1, work_center_id=10))
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(members.insert().values(company_id=1, shift_pattern_id=2, work_center_id=10))

    settings = table(connection, "production_calendar_settings")
    row = {"company_id": 1, "production_day_start_minute": 300, "version": 1, "updated_by": 1, "updated_at": NOW}
    connection.execute(settings.insert().values(**row))
    for bad in (
        {"company_id": 1},
        {"company_id": 2, "production_day_start_minute": 1440},
        {"company_id": 2, "version": 0},
    ):
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(settings.insert().values(**(row | bad)))


def test_models_match_the_migration(local_migration):
    connection, _ = local_migration
    inspector = sa.inspect(connection)
    for model in NEW_MODELS:
        mapped = model.__table__
        columns = {column["name"]: column for column in inspector.get_columns(mapped.name)}
        assert set(columns) == set(mapped.columns.keys()), mapped.name
        for column in mapped.columns:
            reflected = columns[column.name]
            assert reflected["nullable"] == column.nullable, (mapped.name, column.name)
            assert str(reflected["type"]) == str(column.type), (mapped.name, column.name)
            if column.server_default is not None:
                assert str(reflected["default"]).strip("'()") == str(column.server_default.arg), column.name
            else:
                assert reflected["default"] is None, (mapped.name, column.name)
        assert {row["name"]: tuple(row["column_names"]) for row in inspector.get_indexes(mapped.name)} == {
            index.name: tuple(column.name for column in index.columns) for index in mapped.indexes
        }, mapped.name
        assert {row["name"]: row["sqltext"] for row in inspector.get_check_constraints(mapped.name)} == {
            constraint.name: str(constraint.sqltext)
            for constraint in mapped.constraints
            if isinstance(constraint, sa.CheckConstraint)
        }, mapped.name
        assert {row["name"]: tuple(row["column_names"]) for row in inspector.get_unique_constraints(mapped.name)} == {
            constraint.name: tuple(column.name for column in constraint.columns)
            for constraint in mapped.constraints
            if isinstance(constraint, sa.UniqueConstraint)
        }, mapped.name
        reflected_fks = {
            (tuple(fk["constrained_columns"]), fk["referred_table"]) for fk in inspector.get_foreign_keys(mapped.name)
        }
        assert reflected_fks == {((fk.parent.name,), fk.column.table.name) for fk in mapped.foreign_keys}, mapped.name
    partial = [index for index in ShiftPattern.__table__.indexes if index.name == "uq_shift_patterns_company_default"]
    assert partial and partial[0].unique
    assert str(partial[0].dialect_options["postgresql"]["where"]) == "is_default"
    assert str(partial[0].dialect_options["sqlite"]["where"]) == "is_default"
    oee_shift = OEERecord.__table__.c.shift_id
    assert oee_shift.nullable and oee_shift.index
    assert [fk.name for fk in oee_shift.foreign_keys] == ["fk_oee_records_shift_id"]


def test_postgres_migration_and_model_bootstrap_are_default_deny():
    output = io.StringIO()
    migration = module()
    migration.op = Operations(
        MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output})
    )
    migration.upgrade()
    migration_sql = output.getvalue()
    statements = []
    engine = sa.create_mock_engine(
        "postgresql://",
        lambda statement, *args, **kwargs: statements.append(str(statement.compile(dialect=engine.dialect))),
    )
    for model in NEW_MODELS:
        model.__table__.create(engine, checkfirst=False)
    bootstrap_sql = "\n".join(statements)
    for sql in (migration_sql, bootstrap_sql):
        for model in NEW_MODELS:
            name = model.__tablename__
            assert f"ALTER TABLE {name} ENABLE ROW LEVEL SECURITY" in sql
            for role in ("PUBLIC", "anon", "authenticated"):
                assert f"REVOKE ALL ON TABLE {name} FROM {role}" in sql
                assert f"REVOKE ALL ON SEQUENCE {name}_id_seq FROM {role}" in sql
        for role in ("anon", "authenticated"):
            assert f"IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{role}')" in sql
        assert "service_role" not in sql
        assert "CREATE POLICY" not in sql
        assert "INSERT INTO" not in sql and "UPDATE oee_records" not in sql
        assert "CHECK (end_minute > start_minute AND end_minute <= start_minute + 1440)" in sql
    assert "CREATE UNIQUE INDEX uq_shift_patterns_company_default ON shift_patterns (company_id) WHERE is_default" in (
        migration_sql
    )
    assert "ALTER TABLE oee_records ADD COLUMN shift_id INTEGER" in migration_sql
    assert "ADD CONSTRAINT fk_oee_records_shift_id FOREIGN KEY(shift_id) REFERENCES shift_definitions (id)" in (
        migration_sql
    )
    assert "CREATE INDEX ix_oee_records_shift_id ON oee_records (shift_id)" in migration_sql
    output.seek(0)
    output.truncate(0)
    migration.downgrade()
    downgrade_sql = output.getvalue()
    assert "ALTER TABLE oee_records DROP COLUMN shift_id" in downgrade_sql
    for model in NEW_MODELS:
        assert f"DROP TABLE {model.__tablename__}" in downgrade_sql


def test_no_shift_seeded_and_no_backfill_in_source():
    source = (VERSIONS / FILENAME).read_text()
    body = source[source.index("import sqlalchemy") :]
    assert "INSERT" not in body and ".update(" not in body and "bulk_insert" not in body
