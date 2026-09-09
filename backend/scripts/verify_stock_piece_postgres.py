"""Test advisory-stock guards and races only in UUID-named local PostgreSQL schemas."""

import importlib.util
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import sqlalchemy as sa

from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.models.stock_piece import StockPiece, StockPieceObservation


def assert_stock_piece_races(engine):
    if (
        os.environ.get("ENVIRONMENT") != "test"
        or engine.url.get_backend_name() != "postgresql"
        or engine.url.host not in {"localhost", "127.0.0.1", "postgres"}
    ):
        raise RuntimeError("Stock observation verification requires a local disposable PostgreSQL database")
    path = Path(__file__).parents[1] / "alembic/versions/104_stock_piece_observations.py"
    spec = importlib.util.spec_from_file_location("stock_piece_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for bootstrap in ("migration", "metadata"):
        schema = "stock_obs_check_" + uuid4().hex
        quoted = engine.dialect.identifier_preparer.quote(schema)

        def scoped(conn):
            conn.exec_driver_sql(f"SET LOCAL search_path TO {quoted}")
            conn.exec_driver_sql("SET LOCAL lock_timeout TO '10s'")
            conn.exec_driver_sql("SET LOCAL statement_timeout TO '15s'")

        def insert_piece(conn, label, company=1):
            return conn.execute(
                sa.insert(StockPiece).returning(StockPiece.id),
                {"company_id": company, "label": label, "created_by": 1},
            ).scalar_one()

        def observation(piece_id, **changes):
            return (
                dict(
                    company_id=1,
                    stock_piece_id=piece_id,
                    observation_number=1,
                    state="RECORDED",
                    reason="Synthetic PostgreSQL proof",
                    observed_at=datetime(2026, 9, 8, tzinfo=timezone.utc),
                    observer_name="Synthetic observer",
                    payload_schema_version=1,
                    payload_json={"shape": {"type": "unknown"}},
                    payload_sha256="a" * 64,
                    payload_bytes=28,
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

        def refused(conn, statement, params, code="23514"):
            savepoint = conn.begin_nested()
            try:
                conn.execute(statement, params)
            except sa.exc.DBAPIError as error:
                savepoint.rollback()
                assert error.orig.pgcode == code, str(error.orig)
            else:
                savepoint.rollback()
                raise AssertionError("PostgreSQL accepted prohibited observation evidence")

        def advance(conn, piece_id, old=1):
            return conn.execute(
                sa.update(StockPiece)
                .where(
                    StockPiece.id == piece_id,
                    StockPiece.company_id == 1,
                    StockPiece.version == old,
                )
                .values(version=old + 1, latest_observation_number=old + 1)
            ).rowcount

        try:
            with engine.begin() as conn:
                conn.exec_driver_sql(f"CREATE SCHEMA {quoted}")
                scoped(conn)
                # Prove revokes defeat inherited grants, not merely empty defaults.
                for object_type in ("TABLES", "SEQUENCES"):
                    conn.exec_driver_sql(
                        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {quoted} GRANT ALL ON {object_type} TO PUBLIC"
                    )
                for table in ("companies", "users", "api_tokens"):
                    conn.exec_driver_sql(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
                    conn.exec_driver_sql(f"INSERT INTO {table} VALUES (1),(2)")
                conn.exec_driver_sql(
                    "CREATE TABLE parts (id INTEGER PRIMARY KEY, company_id INTEGER, is_deleted BOOLEAN)"
                )
                conn.exec_driver_sql("INSERT INTO parts VALUES (1,1,false),(2,2,false),(3,1,false),(4,1,true)")
                conn.exec_driver_sql(
                    "CREATE TABLE inventory_items (id INTEGER PRIMARY KEY, company_id INTEGER, part_id INTEGER, quantity_on_hand DOUBLE PRECISION)"
                )
                conn.exec_driver_sql(
                    "INSERT INTO inventory_items VALUES (11,1,1,10),(22,2,2,20),(33,1,3,30),(44,1,4,40)"
                )
                temporary_roles = []
                for role in ("anon", "authenticated"):
                    if not conn.scalar(
                        sa.text("SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=:role)"), {"role": role}
                    ):
                        conn.exec_driver_sql(f"CREATE ROLE {role} NOLOGIN")
                        temporary_roles.append(role)
                    for object_type in ("TABLES", "SEQUENCES"):
                        conn.exec_driver_sql(
                            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {quoted} GRANT ALL ON {object_type} TO {role}"
                        )
                if bootstrap == "migration":
                    with Operations.context(MigrationContext.configure(conn)):
                        module.upgrade()
                        module.upgrade()
                else:
                    StockPiece.__table__.create(conn)
                    StockPieceObservation.__table__.create(conn)
                for table in ("stock_pieces", "stock_piece_observations"):
                    assert (
                        conn.scalar(
                            sa.text("SELECT relrowsecurity FROM pg_class WHERE oid=CAST(:name AS regclass)"),
                            {"name": f"{schema}.{table}"},
                        )
                        is True
                    )
                    assert (
                        conn.scalar(
                            sa.text(
                                "SELECT COUNT(*) FROM pg_class c, LATERAL aclexplode(COALESCE(c.relacl,acldefault('r',c.relowner))) AS a WHERE c.oid=CAST(:name AS regclass) AND a.grantee=0"
                            ),
                            {"name": f"{schema}.{table}"},
                        )
                        == 0
                    )
                    assert (
                        conn.scalar(
                            sa.text(
                                "SELECT COUNT(*) FROM pg_class c, LATERAL aclexplode(COALESCE(c.relacl,acldefault('S',c.relowner))) AS a WHERE c.oid=CAST(:name AS regclass) AND a.grantee=0"
                            ),
                            {"name": f"{schema}.{table}_id_seq"},
                        )
                        == 0
                    )
                    for role in ("anon", "authenticated"):
                        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"):
                            assert not conn.scalar(
                                sa.text("SELECT has_table_privilege(:role,:name,:privilege)"),
                                {"role": role, "name": f"{schema}.{table}", "privilege": privilege},
                            )
                        for privilege in ("USAGE", "SELECT", "UPDATE"):
                            assert not conn.scalar(
                                sa.text("SELECT has_sequence_privilege(:role,:name,:privilege)"),
                                {"role": role, "name": f"{schema}.{table}_id_seq", "privilege": privilege},
                            )
                # Temporary roles and their schema-scoped default grants never
                # commit. Existing roles are neither changed nor removed.
                for role in ("anon", "authenticated"):
                    for object_type in ("TABLES", "SEQUENCES"):
                        conn.exec_driver_sql(
                            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {quoted} REVOKE ALL ON {object_type} FROM {role}"
                        )
                for role in temporary_roles:
                    conn.exec_driver_sql(f"DROP ROLE {role}")
                piece = insert_piece(conn, "SYNTHETIC-001")
                for change in (
                    {"company_id": 2},
                    {"source_inventory_item_id": 22, "source_part_id": 2},
                    {"source_inventory_item_id": 33},
                    {"source_inventory_item_id": 44, "source_part_id": 4},
                    {"state": "WITHDRAWN"},
                    {"observation_number": 2},
                ):
                    refused(
                        conn,
                        sa.insert(StockPieceObservation),
                        observation(piece, **change),
                    )
                first = observation(piece)
                conn.execute(sa.insert(StockPieceObservation), first)
                for sql in (
                    "UPDATE stock_pieces SET label='REPLACED' WHERE id=:id",
                    "DELETE FROM stock_pieces WHERE id=:id",
                    "UPDATE stock_piece_observations SET reason='REPLACED' WHERE stock_piece_id=:id",
                    "DELETE FROM stock_piece_observations WHERE stock_piece_id=:id",
                    "TRUNCATE stock_piece_observations",
                    "TRUNCATE stock_pieces CASCADE",
                ):
                    refused(conn, sa.text(sql), {"id": piece})
                # Evidence IDs do not become restrictive FKs on live stock.
                conn.exec_driver_sql("DELETE FROM inventory_items WHERE id=11")
                conn.exec_driver_sql("DELETE FROM parts WHERE id=1")
                assert advance(conn, piece) == 1
                refused(
                    conn,
                    sa.insert(StockPieceObservation),
                    observation(
                        piece,
                        observation_number=2,
                        state="WITHDRAWN",
                        payload_sha256="d" * 64,
                    ),
                )
                conn.execute(
                    sa.insert(StockPieceObservation),
                    observation(piece, observation_number=2, state="WITHDRAWN"),
                )
                assert conn.scalar(sa.select(sa.func.count()).select_from(StockPieceObservation)) == 2
                conn.exec_driver_sql("INSERT INTO parts VALUES (1,1,false)")
                conn.exec_driver_sql("INSERT INTO inventory_items VALUES (11,1,1,10)")
                racing_piece = insert_piece(conn, "SYNTHETIC-CAS")
                conn.execute(sa.insert(StockPieceObservation), observation(racing_piece))

            barrier = Barrier(2)

            def compare_and_append():
                with engine.begin() as conn:
                    scoped(conn)
                    barrier.wait(timeout=10)
                    changed = advance(conn, racing_piece)
                    if changed:
                        conn.execute(
                            sa.insert(StockPieceObservation),
                            observation(racing_piece, observation_number=2),
                        )
                    return changed

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(lambda _: compare_and_append(), range(2)))
            assert sorted(outcomes) == [0, 1]

            def creation_race(same_label):
                barrier = Barrier(2)
                request = str(uuid4())

                def create(index):
                    try:
                        with engine.begin() as conn:
                            scoped(conn)
                            barrier.wait(timeout=10)
                            label = "SYNTHETIC-LABEL-RACE" if same_label else f"SYNTHETIC-REQUEST-{index}"
                            new_piece = insert_piece(conn, label)
                            key = str(uuid4()) if same_label else request
                            conn.execute(
                                sa.insert(StockPieceObservation),
                                observation(new_piece, request_key=key),
                            )
                        return "created"
                    except sa.exc.IntegrityError as error:
                        assert error.orig.pgcode == "23505"
                        return "conflict"

                with ThreadPoolExecutor(max_workers=2) as pool:
                    result = list(pool.map(create, range(2)))
                assert sorted(result) == ["conflict", "created"]

            creation_race(True)
            creation_race(False)

            with engine.begin() as conn:
                scoped(conn)
                assert conn.scalar(sa.select(sa.func.count()).select_from(StockPiece)) == 4
                assert conn.scalar(sa.select(sa.func.count()).select_from(StockPieceObservation)) == 6
                assert conn.exec_driver_sql("SELECT id,quantity_on_hand FROM inventory_items ORDER BY id").all() == [
                    (11, 10),
                    (22, 20),
                    (33, 30),
                    (44, 40),
                ]
                if bootstrap == "migration":
                    with Operations.context(MigrationContext.configure(conn)):
                        module.downgrade()
                        module.downgrade()
                        module.upgrade()
                        module.downgrade()
                    assert conn.scalar(sa.text("SELECT COUNT(*) FROM inventory_items")) == 4
            print(
                f"Stock observations PostgreSQL {bootstrap}: tenant/source/immutability/withdrawal/CAS/label/request races passed."
            )
        finally:
            with engine.begin() as conn:
                conn.exec_driver_sql(f"DROP SCHEMA IF EXISTS {quoted} CASCADE")


if __name__ == "__main__":
    engine = sa.create_engine(os.environ["DATABASE_URL"], pool_size=4)
    try:
        assert_stock_piece_races(engine)
    finally:
        engine.dispose()
