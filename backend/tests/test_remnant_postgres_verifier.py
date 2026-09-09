"""The mandatory remnant PG gate cannot skip, connect remotely or retain fixtures."""

import ast
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts import verify_remnant_planning_postgres as verifier


@pytest.mark.parametrize(
    'environment,url',
    [
        ('production', 'postgresql://localhost/synthetic'),
        ('test', 'postgresql://remote.example.invalid/synthetic'),
        ('test', 'postgresql:///synthetic'),
        ('test', 'sqlite://'),
        ('test', 'postgresql://localhost/synthetic?host=remote.example.invalid'),
        ('test', 'postgresql://localhost/synthetic?hostaddr=203.0.113.1'),
        ('test', 'postgresql://localhost/synthetic?service=external'),
    ],
)
def test_refuses_before_any_database_connection(monkeypatch, environment, url):
    monkeypatch.setenv('ENVIRONMENT', environment)
    monkeypatch.setenv('DATABASE_URL', url)
    connect = Mock(side_effect=AssertionError('No database connection is permitted'))
    monkeypatch.setattr(verifier.sa, 'create_engine', connect)
    with pytest.raises(RuntimeError, match='local disposable PostgreSQL'):
        verifier.verify()
    connect.assert_not_called()


@pytest.mark.parametrize('failure', [None, 'presence', 'lock'])
def test_owned_schema_cleanup_and_assertion_failures_propagate(monkeypatch, failure):
    from app.db.database import Base

    monkeypatch.setenv('ENVIRONMENT', 'test')
    monkeypatch.setenv('DATABASE_URL', 'postgresql://localhost/synthetic')
    statements = []
    connection = Mock()
    connection.exec_driver_sql.side_effect = statements.append
    owner = Mock()
    owner.dialect.identifier_preparer.quote.side_effect = lambda value: '"' + value + '"'

    @contextmanager
    def transaction():
        yield connection

    owner.begin = transaction
    scoped = Mock()
    factory = Mock(side_effect=[owner, scoped])
    monkeypatch.setattr(verifier.sa, 'create_engine', factory)
    monkeypatch.setattr(Base.metadata, 'create_all', Mock())

    @contextmanager
    def session(_engine):
        yield Mock()

    monkeypatch.setattr(verifier, 'Session', session)
    monkeypatch.setattr(verifier, '_project', Mock(return_value=({}, 1, Mock())))
    presence = Mock(side_effect=AssertionError('presence failure') if failure == 'presence' else None)
    locking = Mock(side_effect=AssertionError('lock failure') if failure == 'lock' else None)
    monkeypatch.setattr(verifier, 'assert_json_presence', presence)
    monkeypatch.setattr(verifier, 'assert_source_header_lock', locking)
    if failure:
        with pytest.raises(AssertionError, match=failure + ' failure'):
            verifier.verify()
    else:
        verifier.verify()
        presence.assert_called_once()
        locking.assert_called_once()
    create, drop = statements
    schema = create.removeprefix('CREATE SCHEMA ')
    assert schema.startswith('"remnant_check_') and len(schema) == len('"remnant_check_"') + 32
    assert drop == f'DROP SCHEMA IF EXISTS {schema} CASCADE'
    assert 'public' not in create + drop
    options = factory.call_args_list[1].kwargs['connect_args']['options']
    assert '-csearch_path=' + schema.strip('"') in options
    assert '-clock_timeout=10000' in options and '-cstatement_timeout=15000' in options
    scoped.dispose.assert_called_once()
    owner.dispose.assert_called_once()


def test_operations_gate_runs_remnant_child_with_failure_propagation_and_deadline():
    """Pin the existing E2E entry point, so locally passing checks cannot be omitted in CI."""
    path = Path(__file__).parents[1] / 'scripts/verify_operations_postgres.py'
    tree = ast.parse(path.read_text())
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == 'run'
        and any(
            isinstance(child, ast.Constant) and child.value == 'scripts.verify_remnant_planning_postgres'
            for child in ast.walk(node)
        )
    ]
    assert len(calls) == 1
    arguments = {keyword.arg: keyword.value for keyword in calls[0].keywords}
    assert ast.literal_eval(arguments['check']) is True
    assert ast.literal_eval(arguments['timeout']) == 60
    assert ast.unparse(arguments['stdin']) == 'subprocess.DEVNULL'
