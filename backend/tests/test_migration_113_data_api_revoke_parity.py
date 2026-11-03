"""091/093 objects get their siblings' Data API revocations; nothing is ever re-granted."""

import importlib.util
import re
from io import StringIO
from pathlib import Path

import sqlalchemy as sa

from alembic.migration import MigrationContext
from alembic.operations import Operations

TABLES = ('mrp_supply_links', 'user_workspace_records')


def revision():
    path = Path(__file__).parents[1] / 'alembic/versions/113_data_api_revoke_parity.py'
    spec = importlib.util.spec_from_file_location('data_api_revoke_parity_migration', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def render(dialect_name, step):
    sql = StringIO()
    context = MigrationContext.configure(dialect_name=dialect_name, opts={'as_sql': True, 'output_buffer': sql})
    with Operations.context(context):
        getattr(revision(), step)()
    return sql.getvalue()


def test_revision_identity():
    module = revision()
    assert module.revision == '113_data_api_revoke_parity'
    assert len(module.revision) <= 32
    assert module.down_revision == '112_consumable_checkouts'


def test_postgres_upgrade_revokes_public_and_role_guarded_data_api_access():
    output = render('postgresql', 'upgrade')
    for table in TABLES:
        assert f'REVOKE ALL ON TABLE {table} FROM PUBLIC;' in output
        assert f'REVOKE ALL ON SEQUENCE {table}_id_seq FROM PUBLIC;' in output
    # Each role's REVOKEs run only inside its own pg_roles existence check, so the
    # revision also applies on a Postgres without the Supabase roles (CI, local Docker).
    for role in ('anon', 'authenticated'):
        guarded = re.search(
            rf"IF EXISTS \(SELECT 1 FROM pg_roles WHERE rolname = '{role}'\) THEN(.*?)END IF;", output, re.S
        )
        assert guarded, f'no pg_roles existence guard for {role}'
        for table in TABLES:
            assert f'REVOKE ALL ON TABLE {table} FROM {role};' in guarded.group(1)
            assert f'REVOKE ALL ON SEQUENCE {table}_id_seq FROM {role};' in guarded.group(1)
    unguarded = re.sub(r'DO \$\$.*?END \$\$;', '', output, flags=re.S)
    assert 'anon' not in unguarded and 'authenticated' not in unguarded
    assert 'GRANT' not in output


def test_postgres_downgrade_never_regrants():
    assert render('postgresql', 'downgrade').strip() == ''


def test_sqlite_is_a_no_op(tmp_path):
    assert render('sqlite', 'upgrade').strip() == ''
    assert render('sqlite', 'downgrade').strip() == ''
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'parity.db'}")
    with engine.begin() as connection:
        connection.execute(sa.text('CREATE TABLE mrp_supply_links (id INTEGER PRIMARY KEY)'))
        before = connection.execute(sa.text('SELECT type, name, sql FROM sqlite_master ORDER BY name')).all()
        with Operations.context(MigrationContext.configure(connection)):
            revision().upgrade()
            revision().downgrade()
            revision().upgrade()
        after = connection.execute(sa.text('SELECT type, name, sql FROM sqlite_master ORDER BY name')).all()
    engine.dispose()
    assert after == before
