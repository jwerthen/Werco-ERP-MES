"""Observation history is immutable without owning live inventory rows."""

import importlib.util
import io
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.models.stock_piece import (
    POSTGRES_DDL,
    SQLITE_DDL,
    StockPiece,
    StockPieceObservation,
)


def migration():
    path = Path(__file__).parents[1] / "alembic/versions/104_stock_piece_observations.py"
    spec = importlib.util.spec_from_file_location("stock_observation_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def seed(conn):
    for table in ("companies", "users", "api_tokens"):
        conn.exec_driver_sql(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
        conn.exec_driver_sql(f"INSERT INTO {table} VALUES (1), (2)")
    conn.exec_driver_sql("CREATE TABLE parts (id INTEGER PRIMARY KEY, company_id INTEGER, is_deleted BOOLEAN)")
    conn.exec_driver_sql("INSERT INTO parts VALUES (1,1,0), (2,2,0), (3,1,0), (4,1,1)")
    conn.exec_driver_sql(
        "CREATE TABLE inventory_items (id INTEGER PRIMARY KEY, company_id INTEGER, part_id INTEGER, quantity_on_hand FLOAT)"
    )
    conn.exec_driver_sql(
        "INSERT INTO inventory_items VALUES (11,1,1,10), (22,2,2,20), (33,1,3,30), (44,1,4,40), (55,2,1,50)"
    )


@pytest.fixture(params=["migration", "metadata"])
def connection(request):
    engine = sa.create_engine("sqlite://")
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        seed(conn)
        if request.param == "migration":
            with Operations.context(MigrationContext.configure(conn)):
                migration().upgrade()
        else:
            StockPiece.__table__.create(conn)
            StockPieceObservation.__table__.create(conn)
        conn.execute(
            sa.insert(StockPiece),
            {"id": 1, "company_id": 1, "label": "SYNTHETIC-001", "created_by": 1},
        )
        conn.commit()
        yield conn
    engine.dispose()


def observation(**changes):
    return (
        dict(
            company_id=1,
            stock_piece_id=1,
            observation_number=1,
            state="RECORDED",
            reason="Synthetic observation",
            observed_at=datetime(2026, 9, 8, tzinfo=timezone.utc),
            observer_name="Synthetic observer",
            payload_schema_version=1,
            payload_json={"shape": "unknown"},
            payload_sha256="a" * 64,
            payload_bytes=19,
            source_inventory_item_id=11,
            source_part_id=1,
            source_snapshot_json={"inventory_item_id": 11},
            source_sha256="b" * 64,
            created_by=1,
            request_key=str(uuid4()),
            request_hash="c" * 64,
        )
        | changes
    )


def advance(conn, old=1):
    return conn.execute(
        sa.update(StockPiece)
        .where(StockPiece.company_id == 1, StockPiece.id == 1, StockPiece.version == old)
        .values(version=old + 1, latest_observation_number=old + 1)
    ).rowcount


def test_frozen_guard_parity_and_no_operational_source_foreign_keys():
    module = migration()
    assert module.POSTGRES_DDL == POSTGRES_DDL
    assert module.SQLITE_DDL == SQLITE_DDL
    assert module.down_revision == "103_nesting_spacing_policies"
    assert len(module.revision) <= 32
    assert all(
        fk.referred_table.name not in {"inventory_items", "parts"}
        for fk in StockPieceObservation.__table__.foreign_key_constraints
    )


@pytest.mark.parametrize(
    "environment,url",
    [
        ("production", "postgresql://localhost/disposable"),
        ("test", "postgresql://database.example.invalid/disposable"),
        ("test", "sqlite://"),
    ],
)
def test_postgres_verifier_rejects_nonlocal_or_nontest_database(monkeypatch, environment, url):
    from scripts.verify_stock_piece_postgres import assert_stock_piece_races

    class UnconnectableEngine:
        def __init__(self):
            self.url = sa.engine.make_url(url)

        def begin(self):
            pytest.fail("The verifier must refuse before opening any connection")

    monkeypatch.setenv("ENVIRONMENT", environment)
    with pytest.raises(RuntimeError, match="local disposable"):
        assert_stock_piece_races(UnconnectableEngine())


@pytest.mark.parametrize(
    "change",
    [
        {"company_id": 2},
        {"source_inventory_item_id": 22, "source_part_id": 2},
        {"source_inventory_item_id": 33},
        {"source_inventory_item_id": 44, "source_part_id": 4},
        {"source_inventory_item_id": 55},
        {"source_inventory_item_id": 999},
        {"observation_number": 2},
        {"state": "WITHDRAWN"},
        {"state": "AVAILABLE"},
        {"payload_bytes": 131073},
        {"payload_bytes": 0},
        {"payload_schema_version": 2},
        {"source_sha256": "invalid"},
        {"request_key": "invalid"},
        {"reason": " "},
    ],
)
def test_invalid_or_foreign_initial_evidence_is_rejected(connection, change):
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(sa.insert(StockPieceObservation), observation(**change))
    assert connection.scalar(sa.select(sa.func.count()).select_from(StockPieceObservation)) == 0


def test_corrections_and_withdrawal_leave_inventory_unchanged(connection):
    before = connection.exec_driver_sql("SELECT * FROM inventory_items ORDER BY id").all()
    connection.execute(sa.insert(StockPieceObservation), observation())
    assert advance(connection) == 1
    assert advance(connection) == 0
    connection.execute(
        sa.insert(StockPieceObservation),
        observation(observation_number=2, reason="Corrected observation"),
    )
    assert advance(connection, 2) == 1
    connection.execute(
        sa.insert(StockPieceObservation),
        observation(observation_number=3, state="WITHDRAWN"),
    )
    assert connection.exec_driver_sql("SELECT * FROM inventory_items ORDER BY id").all() == before
    assert connection.execute(
        sa.select(StockPieceObservation.state).order_by(StockPieceObservation.observation_number)
    ).scalars().all() == ["RECORDED", "RECORDED", "WITHDRAWN"]


def test_missing_live_source_preserves_history_and_permits_withdrawal(connection):
    connection.execute(sa.insert(StockPieceObservation), observation())
    connection.exec_driver_sql("DELETE FROM inventory_items WHERE id=11")
    connection.exec_driver_sql("DELETE FROM parts WHERE id=1")
    advance(connection)
    connection.execute(
        sa.insert(StockPieceObservation),
        observation(observation_number=2, state="WITHDRAWN"),
    )
    assert connection.scalar(sa.select(sa.func.count()).select_from(StockPieceObservation)) == 2


@pytest.mark.parametrize(
    "change",
    [
        {"source_inventory_item_id": 33},
        {"source_part_id": 3},
        {"source_sha256": "d" * 64},
        {"source_snapshot_json": {"id": 33}},
        {"payload_sha256": "d" * 64},
        {"payload_json": {"shape": "rectangle"}},
        {"payload_bytes": 20},
    ],
)
def test_withdrawal_cannot_replace_earlier_measurement_or_source(connection, change):
    connection.execute(sa.insert(StockPieceObservation), observation())
    advance(connection)
    with pytest.raises(IntegrityError, match="preserve"), connection.begin_nested():
        connection.execute(
            sa.insert(StockPieceObservation),
            observation(observation_number=2, state="WITHDRAWN", **change),
        )


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE stock_pieces SET label='REPLACED' WHERE id=1",
        "DELETE FROM stock_pieces WHERE id=1",
        "UPDATE stock_piece_observations SET reason='REPLACED' WHERE stock_piece_id=1",
        "DELETE FROM stock_piece_observations WHERE stock_piece_id=1",
    ],
)
def test_raw_sql_cannot_rewrite_history_or_identity(connection, statement):
    connection.execute(sa.insert(StockPieceObservation), observation())
    with pytest.raises(IntegrityError, match="immutable"), connection.begin_nested():
        connection.exec_driver_sql(statement)


def test_counter_cannot_advance_without_its_previous_observation(connection):
    with pytest.raises(IntegrityError), connection.begin_nested():
        advance(connection)


def test_label_and_request_key_are_unique_within_the_company(connection):
    first = observation()
    connection.execute(sa.insert(StockPieceObservation), first)
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(
            sa.insert(StockPiece),
            {"company_id": 1, "label": "SYNTHETIC-001", "created_by": 1},
        )
    connection.execute(
        sa.insert(StockPiece),
        {"id": 2, "company_id": 2, "label": "SYNTHETIC-001", "created_by": 1},
    )
    connection.execute(
        sa.insert(StockPieceObservation),
        observation(
            company_id=2,
            stock_piece_id=2,
            source_inventory_item_id=22,
            source_part_id=2,
            request_key=first["request_key"],
        ),
    )
    advance(connection)
    with pytest.raises(IntegrityError), connection.begin_nested():
        connection.execute(
            sa.insert(StockPieceObservation),
            observation(observation_number=2, request_key=first["request_key"]),
        )


def test_idempotent_migration_roundtrip_preserves_operational_rows():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        seed(conn)
        before = conn.exec_driver_sql("SELECT * FROM inventory_items ORDER BY id").all()
        with Operations.context(MigrationContext.configure(conn)):
            module = migration()
            module.upgrade()
            module.upgrade()
            module.downgrade()
            module.downgrade()
            module.upgrade()
        assert connection_tables(conn) >= {"stock_pieces", "stock_piece_observations"}
        assert conn.exec_driver_sql("SELECT * FROM inventory_items ORDER BY id").all() == before
    engine.dispose()


def connection_tables(conn):
    return set(sa.inspect(conn).get_table_names())


def test_postgres_offline_guards_do_not_alter_operational_tables():
    output = io.StringIO()
    with Operations.context(
        MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output})
    ):
        migration().upgrade()
    sql = output.getvalue()
    for fragment in [
        "ENABLE ROW LEVEL SECURITY",
        "REVOKE ALL",
        "FOR UPDATE",
        "SET search_path = ''",
        "BEFORE TRUNCATE",
    ]:
        assert fragment in sql
    for table in ("inventory_items", "parts", "inventory_transactions"):
        assert f"ALTER TABLE {table}" not in sql
        assert f"UPDATE {table}" not in sql
    assert "SECURITY DEFINER" not in sql
