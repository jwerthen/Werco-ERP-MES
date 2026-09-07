"""Revision linking survives a reversible migration without losing historical documents."""

import importlib.util
from io import StringIO
from pathlib import Path

import sqlalchemy as sa

from alembic.migration import MigrationContext
from alembic.operations import Operations


def _revision():
    path = Path(__file__).parents[1] / 'alembic/versions/090_document_revision_chain.py'
    spec = importlib.util.spec_from_file_location('document_revision_migration', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_document_revision_migration_roundtrip_keeps_records(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'revisions.db'}")
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                'CREATE TABLE documents (id INTEGER PRIMARY KEY, company_id INTEGER NOT NULL, document_number VARCHAR(100))'
            )
        )
        connection.execute(sa.text("INSERT INTO documents VALUES (1, 1, 'DOC-ORIGINAL')"))
        with Operations.context(MigrationContext.configure(connection)):
            _revision().upgrade()
        connection.execute(
            sa.text(
                "INSERT INTO documents (id,company_id,document_number,previous_revision_id) VALUES (2,1,'DOC-REVISION',1)"
            )
        )
        assert connection.execute(sa.text('SELECT previous_revision_id FROM documents WHERE id=2')).scalar() == 1
        with Operations.context(MigrationContext.configure(connection)):
            _revision().downgrade()
        assert connection.execute(sa.text('SELECT count(*) FROM documents')).scalar() == 2
        with Operations.context(MigrationContext.configure(connection)):
            _revision().upgrade()
        assert 'previous_revision_id' in {column['name'] for column in sa.inspect(connection).get_columns('documents')}
        assert (
            connection.execute(sa.text('SELECT document_number FROM documents WHERE id=1')).scalar() == 'DOC-ORIGINAL'
        )
    engine.dispose()


def test_document_revision_migration_compiles_for_postgres():
    sql = StringIO()
    context = MigrationContext.configure(dialect_name='postgresql', opts={'as_sql': True, 'output_buffer': sql})
    with Operations.context(context):
        _revision().upgrade()
        _revision().downgrade()
    assert 'FOREIGN KEY(previous_revision_id) REFERENCES documents (id)' in sql.getvalue()
    assert 'DROP COLUMN previous_revision_id' in sql.getvalue()
