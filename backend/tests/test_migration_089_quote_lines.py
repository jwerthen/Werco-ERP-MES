"""Exercise migration data preservation and the production SQL shape locally."""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.models.quote import Quote, QuoteLine


def test_quote_line_migration_round_trip_preserves_legacy_rows():
    path = Path(__file__).parents[1] / 'alembic/versions/089_quote_line_conversion_track_work_orders_per_quote_line.py'
    spec = importlib.util.spec_from_file_location('quote_line_migration', path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine('sqlite://')
    with engine.begin() as conn:
        conn.exec_driver_sql(
            'CREATE TABLE quotes (id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL, total FLOAT NOT NULL)'
        )
        conn.exec_driver_sql('INSERT INTO quotes VALUES (1, 1, 125)')
        conn.exec_driver_sql('CREATE TABLE work_orders (id INTEGER PRIMARY KEY)')
        conn.exec_driver_sql('CREATE TABLE quote_lines (id INTEGER PRIMARY KEY, quantity FLOAT NOT NULL)')
        conn.exec_driver_sql('INSERT INTO quote_lines VALUES (1, 20)')
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
            assert conn.exec_driver_sql('SELECT quantity, work_order_id FROM quote_lines').one() == (20, None)
            assert conn.exec_driver_sql('SELECT total, request_key, request_hash FROM quotes').one() == (
                125,
                None,
                None,
            )
            migration.downgrade()
            assert conn.exec_driver_sql('SELECT quantity FROM quote_lines').scalar_one() == 20
            assert conn.exec_driver_sql('SELECT total FROM quotes').scalar_one() == 125
        assert 'work_order_id' not in {col['name'] for col in sa.inspect(conn).get_columns('quote_lines')}


def test_quote_line_mapping_has_indexed_nullable_foreign_key():
    table_sql = str(CreateTable(QuoteLine.__table__).compile(dialect=postgresql.dialect()))
    assert 'FOREIGN KEY(work_order_id) REFERENCES work_orders (id)' in table_sql
    assert (
        next(iter(QuoteLine.__table__.c.work_order_id.foreign_keys)).constraint.name == 'fk_quote_lines_work_order_id'
    )
    assert QuoteLine.__table__.c.work_order_id.nullable
    index_sql = [str(CreateIndex(index).compile(dialect=postgresql.dialect())) for index in QuoteLine.__table__.indexes]
    assert any('ix_quote_lines_work_order_id' in statement for statement in index_sql)


def test_quote_request_key_uniqueness_is_tenant_scoped_and_optional():
    table_sql = str(CreateTable(Quote.__table__).compile(dialect=postgresql.dialect()))
    assert 'CONSTRAINT uq_quotes_company_request_key UNIQUE (company_id, request_key)' in table_sql
    assert Quote.__table__.c.request_key.nullable
    assert Quote.__table__.c.request_hash.nullable
