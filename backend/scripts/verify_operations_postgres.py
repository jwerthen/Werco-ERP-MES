"""Exercise migrations 095–100 and runtime p75 on CI's disposable PostgreSQL.

This uses an isolated schema, rolls everything back, and refuses remote/production DBs.
Run before the E2E seed so schema migration failures stop the browser suite early.
"""

import importlib.util
import os
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.orm import Session

from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.models.runtime_metric import RuntimeMetricSample
from app.services.runtime_metric_service import summarize_runtime_metrics

DATA_API_ROLES = ("anon", "authenticated")
TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
SEQUENCE_PRIVILEGES = ("USAGE", "SELECT", "UPDATE")


def assert_private_objects(connection, schema, tables):
    for table in sorted(tables):
        relation = f'"{schema}"."{table}"'
        rls = connection.execute(
            sa.text("SELECT relrowsecurity FROM pg_class WHERE oid = to_regclass(:relation)"),
            {"relation": relation},
        ).scalar_one()
        assert rls, f"RLS is disabled for {table}"
        for role in DATA_API_ROLES:
            for privilege in TABLE_PRIVILEGES:
                allowed = connection.execute(
                    sa.text("SELECT has_table_privilege(:role, :relation, :privilege)"),
                    {"role": role, "relation": relation, "privilege": privilege},
                ).scalar_one()
                assert not allowed, f"{role} retains {privilege} on {table}"
    # All sequences in this newly created schema belong to the migrations: the
    # reference tables below use plain INTEGER keys, never SERIAL/IDENTITY.
    for sequence in sa.inspect(connection).get_sequence_names(schema=schema):
        relation = f'"{schema}"."{sequence}"'
        for role in DATA_API_ROLES:
            for privilege in SEQUENCE_PRIVILEGES:
                allowed = connection.execute(
                    sa.text("SELECT has_sequence_privilege(:role, :relation, :privilege)"),
                    {"role": role, "relation": relation, "privilege": privilege},
                ).scalar_one()
                assert not allowed, f"{role} retains {privilege} on {sequence}"


def verify():
    url = sa.engine.make_url(os.environ["DATABASE_URL"])
    if os.environ.get("ENVIRONMENT") != "test" or url.host not in {"localhost", "127.0.0.1", "postgres"}:
        raise RuntimeError("This check requires a local disposable test database")
    if url.get_backend_name() != "postgresql":
        raise RuntimeError("This check requires PostgreSQL")
    engine = sa.create_engine(url)
    schema = "ux_check_" + uuid4().hex
    migrations = []
    for filename in (
        "095_kiosk_production_receipts",
        "096_working_calendars",
        "097_team_workspaces",
        "098_runtime_metrics",
        "099_recoverable_import_batches",
        "100_receiving_supplier_followup",
    ):
        path = Path(__file__).resolve().parents[1] / "alembic/versions" / (filename + ".py")
        spec = importlib.util.spec_from_file_location(filename, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        migrations.append(module)
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            # CI's test owner is a superuser. Exercise the conditional REVOKE
            # branches even on stock PostgreSQL; never alter an existing role.
            # These temporary role definitions roll back with the schema below.
            for role in DATA_API_ROLES:
                exists = connection.execute(
                    sa.text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :role)"),
                    {"role": role},
                ).scalar_one()
                if not exists:
                    connection.execute(sa.text(f'CREATE ROLE "{role}" NOLOGIN'))
            connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(sa.text(f'SET LOCAL search_path TO "{schema}"'))
            # Simulate inherited Data API grants, so the denial checks exercise
            # the migrations' REVOKEs rather than pristine PostgreSQL defaults.
            for objects in ("TABLES", "SEQUENCES"):
                connection.execute(
                    sa.text(
                        f'ALTER DEFAULT PRIVILEGES IN SCHEMA "{schema}" '
                        f"GRANT ALL ON {objects} TO PUBLIC, anon, authenticated"
                    )
                )
            # Existing references only; these migrations must not depend on seed data.
            for table in ("companies", "users", "work_centers", "work_order_operations", "time_entries"):
                connection.execute(sa.text(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)"))
            connection.execute(sa.text("""CREATE TABLE work_orders (
              id INTEGER PRIMARY KEY, company_id INTEGER, is_deleted BOOLEAN,
              priority INTEGER, due_date DATE
            )"""))
            connection.execute(sa.text("CREATE TABLE documents (id INTEGER PRIMARY KEY)"))
            connection.execute(
                sa.text("CREATE TABLE purchase_orders (id INTEGER PRIMARY KEY, company_id INTEGER, status VARCHAR)")
            )
            connection.execute(sa.text("CREATE TABLE po_receipts (id INTEGER PRIMARY KEY)"))
            with Operations.context(MigrationContext.configure(connection)):
                for migration in migrations:
                    migration.upgrade()
                inspector = sa.inspect(connection)
                new_tables = set(inspector.get_table_names(schema=schema)) - {
                    "companies",
                    "users",
                    "work_centers",
                    "work_order_operations",
                    "time_entries",
                    "work_orders",
                    "documents",
                    "purchase_orders",
                    "po_receipts",
                }
                assert_private_objects(connection, schema, new_tables)
                connection.execute(sa.text("INSERT INTO companies (id) VALUES (1)"))
                connection.execute(sa.text("INSERT INTO users (id) VALUES (1)"))
                connection.execute(sa.text("INSERT INTO documents (id) VALUES (1)"))
                connection.execute(
                    sa.text("INSERT INTO purchase_orders (id, company_id, status) VALUES (1, 1, 'sent')")
                )
                connection.execute(sa.text("INSERT INTO po_receipts (id) VALUES (1)"))
                connection.execute(
                    sa.text(
                        """INSERT INTO import_batches
                    (id, company_id, entity, filename, source_hash, request_key, headers, version, created_by, created_at, updated_at)
                    VALUES (1, 1, 'parts', 'synthetic.csv', 'synthetic-source', 'synthetic-import', '[]', 1, 1, now(), now())"""
                    )
                )
                connection.execute(sa.text("""INSERT INTO import_batch_rows
                    (company_id, batch_id, row_key, group_key, source_row, data, status, created_at, updated_at)
                    VALUES (1, 1, 'synthetic-row', 'synthetic-group', 2, '{}', 'ready', now(), now())"""))
                connection.execute(sa.text("""INSERT INTO receiving_delivery_batches
                    (id, company_id, purchase_order_id, request_key, payload_hash, response, created_by, created_at)
                    VALUES (1, 1, 1, 'synthetic-delivery', 'synthetic-payload', '{}', 1, now())"""))
                connection.execute(sa.text("""UPDATE po_receipts
                    SET certificate_document_id = 1, delivery_batch_id = 1 WHERE id = 1"""))
                connection.execute(sa.text("""UPDATE purchase_orders SET supplier_confirmed_date = '2026-09-10',
                    supplier_acknowledged_by = 1, follow_up_owner_id = 1 WHERE id = 1"""))
                cohorts = {"/parts": 1, "/work-orders": 4, "/purchasing": 5, "/quality": 8}
                rows = [
                    {
                        "company_id": 1,
                        "metric_id": str(uuid4()),
                        "name": "LCP",
                        "route": route,
                        "device": "mobile",
                        "navigation": "document",
                        "release": "a" * 40,
                        "value": value * 100,
                        "sequence": 1,
                        "created_at": datetime.utcnow(),
                    }
                    for route, size in cohorts.items()
                    for value in range(1, size + 1)
                ]
                connection.execute(sa.insert(RuntimeMetricSample.__table__), rows)
                with Session(bind=connection) as session:
                    summary = summarize_runtime_metrics(session, 1, 7)
                    assert {row["route"]: row["p75"] for row in summary} == {
                        "/parts": 100,
                        "/work-orders": 300,
                        "/purchasing": 400,
                        "/quality": 600,
                    }, summary
                for migration in reversed(migrations):
                    migration.downgrade()
                remaining = set(sa.inspect(connection).get_table_names(schema=schema))
                assert not (new_tables & remaining)
                assert connection.execute(sa.text("SELECT COUNT(*) FROM purchase_orders")).scalar_one() == 1
                assert connection.execute(sa.text("SELECT COUNT(*) FROM po_receipts")).scalar_one() == 1
                # Recreate after a populated downgrade to catch leftover tables,
                # sequences and indexes that would break a second upgrade.
                for migration in migrations:
                    migration.upgrade()
                assert set(sa.inspect(connection).get_table_names(schema=schema)) == remaining | new_tables
                assert_private_objects(connection, schema, new_tables)
                for migration in reversed(migrations):
                    migration.downgrade()
                assert set(sa.inspect(connection).get_table_names(schema=schema)) == remaining
                assert not sa.inspect(connection).get_sequence_names(schema=schema)
                assert not sa.inspect(connection).get_indexes("work_orders", schema=schema)
            print(
                "PostgreSQL migrations 095–100 passed upgrade/downgrade twice, "
                "RLS and Data API table/sequence privilege checks, and p75 cohorts 1/4/5/8."
            )
        finally:
            transaction.rollback()
            engine.dispose()


if __name__ == "__main__":
    verify()
