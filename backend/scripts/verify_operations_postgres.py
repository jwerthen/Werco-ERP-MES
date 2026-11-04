"""Exercise migrations 091–103 and 113, and runtime p75, on CI's disposable PostgreSQL.

095–103 run in an isolated schema, then 101–103 again over tables the MODELS built (the
fresh bootstrap's real case). 091–094 and 113 run in a scratch database instead, because
093 qualifies its statements with ``public.``; that database is dropped afterwards. Every
check rolls back, and the script refuses remote/production DBs. Run before the E2E seed so
schema migration failures stop the browser suite early.
"""

import importlib.util
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.orm import Session

from alembic.migration import MigrationContext
from alembic.operations import Operations
from app.db.database import Base
from app.models.runtime_metric import RuntimeMetricSample
from app.services.runtime_metric_service import summarize_runtime_metrics
from scripts._pg_verify import (
    DATA_API_ROLES,
    create_missing_data_api_roles,
    existing_data_api_roles,
    grant_inherited_data_api_privileges,
    require_local_test_postgres,
    scratch_database,
    trigger_names,
)
from scripts.verify_nesting_runs_postgres import assert_nesting_run_races
from scripts.verify_nesting_sources_postgres import assert_nesting_source_races
from scripts.verify_nesting_spacing_postgres import assert_spacing_policy_races
from scripts.verify_stock_piece_postgres import assert_stock_piece_races

TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")
SEQUENCE_PRIVILEGES = ("USAGE", "SELECT", "UPDATE")
MIGRATIONS = (
    "095_kiosk_production_receipts",
    "096_working_calendars",
    "097_team_workspaces",
    "098_runtime_metrics",
    "099_recoverable_import_batches",
    "100_receiving_supplier_followup",
    "101_quote_nesting_drafts",
    "102_quote_nesting_runs",
    "103_quote_nesting_spacing_policies",
)
NESTING_MIGRATIONS = (
    "101_quote_nesting_drafts",
    "102_quote_nesting_runs",
    "103_quote_nesting_spacing_policies",
)
# Their models carry after_create listeners (RLS, REVOKEs, triggers) that run before 101–103 do.
NESTING_TABLES = (
    "quote_nesting_drafts",
    "quote_nesting_revisions",
    "quote_nesting_runs",
    "quote_nesting_run_checkpoints",
    "quote_nesting_spacing_policies",
    "quote_nesting_spacing_revisions",
    "quote_nesting_spacing_events",
)
REVOKE_PARITY_MIGRATIONS = (
    "091_user_workspaces",
    "092_operational_inbox_state",
    "093_mrp_supply_links",
    "094_document_deliveries",
    "113_data_api_revoke_parity",
)
REVOKE_PARITY_REFERENCES = ("companies", "users", "mrp_actions", "purchase_orders", "work_orders")
# Exactly what 091–094 create. Asserted, not derived: an empty or short set would let the
# privacy check below pass vacuously.
REVOKE_PARITY_TABLES = frozenset(
    {"user_workspace_records", "operational_inbox_states", "mrp_supply_links", "document_deliveries"}
)


def load_migrations(names):
    migrations = []
    for filename in names:
        path = Path(__file__).resolve().parents[1] / "alembic/versions" / (filename + ".py")
        spec = importlib.util.spec_from_file_location(filename, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        migrations.append(module)
    return migrations


def assert_nesting_revision_guards(connection):
    """Use real SQL against the new tables; ORM events cannot make these pass."""
    connection.execute(sa.text("""INSERT INTO quote_nesting_drafts
      (id, company_id, name, created_by, created_at, updated_at)
      VALUES (1, 1, 'Synthetic draft', 1, now(), now())"""))
    insert_revision = sa.text("""INSERT INTO quote_nesting_revisions
      (id, company_id, draft_id, revision_number, draft_version, name, estimate_json,
       content_sha256, payload_schema_version, payload_bytes, created_by, created_at,
       request_key, request_hash, review_issues_json)
      VALUES (:id, :company_id, 1, :revision, :revision, 'Synthetic revision', '{}',
       :content_hash, 6, 2, 1, now(), :request_key, :request_hash, '[]')""")
    first = {
        'id': 1,
        'company_id': 1,
        'revision': 1,
        'content_hash': 'a' * 64,
        'request_hash': 'b' * 64,
        'request_key': str(uuid4()),
    }
    connection.execute(insert_revision, first)

    def refused(statement, parameters, expected_code):
        savepoint = connection.begin_nested()
        try:
            connection.execute(statement, parameters)
        except sa.exc.DBAPIError as error:
            savepoint.rollback()
            assert error.orig.pgcode == expected_code, str(error.orig)
        else:
            savepoint.rollback()
            raise AssertionError('PostgreSQL accepted a prohibited nesting revision write')

    for statement in (
        "UPDATE quote_nesting_revisions SET name='Changed' WHERE id=1",
        'DELETE FROM quote_nesting_revisions WHERE id=1',
        'TRUNCATE quote_nesting_revisions, quote_nesting_runs, quote_nesting_run_checkpoints',
    ):
        refused(sa.text(statement), {}, '23514')
    connection.execute(sa.text('INSERT INTO companies (id) VALUES (2)'))
    refused(insert_revision, {**first, 'id': 2, 'company_id': 2, 'revision': 2, 'request_key': str(uuid4())}, '23503')
    refused(insert_revision, {**first, 'id': 2, 'revision': 2}, '23505')
    refused(insert_revision, {**first, 'id': 2, 'request_key': str(uuid4())}, '23505')
    refused(sa.text("UPDATE quote_nesting_drafts SET status='APPROVED' WHERE id=1"), {}, '23514')
    assert connection.execute(sa.text('SELECT name FROM quote_nesting_revisions')).scalar_one() == 'Synthetic revision'


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


def assert_model_built_nesting_tables(connection, migrations, migration_triggers):
    """Run 101–103 over tables the MODELS built, as the fresh bootstrap does.

    create_all's after_create listeners have already enabled RLS, revoked and built the
    triggers by the time the migrations run, so this catches model/migration drift (a
    differently named trigger would survive the migration's DROP IF EXISTS as a duplicate)
    that the migration-only pass above cannot see.
    """
    schema = "ux_model_check_" + uuid4().hex
    connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
    connection.execute(sa.text(f'SET LOCAL search_path TO "{schema}"'))
    grant_inherited_data_api_privileges(connection, schema)
    for table in ("companies", "users", "api_tokens"):
        connection.execute(sa.text(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)"))
    Base.metadata.create_all(connection, tables=[Base.metadata.tables[name] for name in NESTING_TABLES])
    assert (
        trigger_names(connection, schema, NESTING_TABLES) == migration_triggers
    ), "the quote_nesting models' after_create triggers differ from 101–103's"
    for migration in migrations:
        if migration.__name__ in NESTING_MIGRATIONS:
            migration.upgrade()
    assert (
        trigger_names(connection, schema, NESTING_TABLES) == migration_triggers
    ), "101–103 over model-built tables left a duplicate or missing trigger"
    assert_private_objects(connection, schema, NESTING_TABLES)
    connection.execute(sa.text("INSERT INTO companies (id) VALUES (1)"))
    connection.execute(sa.text("INSERT INTO users (id) VALUES (1)"))
    assert_nesting_revision_guards(connection)


def verify_data_api_revocations(url, migrations):
    """Run 091–094 and 113 in a scratch database, then drop it.

    Not an isolated schema: 093 names ``public.mrp_supply_links``, which only a real
    ``public`` schema can satisfy. Checks (i) they apply on a cluster WITHOUT the Supabase
    roles, where 093's once-bare REVOKE aborted, and (ii) their tables and sequences end
    private under inherited PUBLIC/anon/authenticated grants. ``has_*_privilege`` counts
    PUBLIC grants, so (ii) needs 113's FROM PUBLIC and 091's missing sequence revoke.
    """
    with scratch_database(url, "revoke_check_") as scratch_url:
        scratch = sa.create_engine(scratch_url)
        try:
            with scratch.connect() as connection:
                transaction = connection.begin()
                try:
                    for table in REVOKE_PARITY_REFERENCES:
                        connection.execute(sa.text(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)"))
                    present = existing_data_api_roles(connection)
                    with Operations.context(MigrationContext.configure(connection)):
                        if present:
                            no_role = f"SKIPPED the no-role check ({', '.join(present)} already exist in this cluster)"
                        else:
                            savepoint = connection.begin_nested()
                            for migration in migrations:
                                migration.upgrade()
                            savepoint.rollback()
                            no_role = "applied on a cluster without anon/authenticated"
                        create_missing_data_api_roles(connection)
                        grant_inherited_data_api_privileges(connection, "public")
                        for migration in (*migrations, *migrations):
                            migration.upgrade()
                        inspector = sa.inspect(connection)
                        tables = set(inspector.get_table_names(schema="public")) - set(REVOKE_PARITY_REFERENCES)
                        assert tables == REVOKE_PARITY_TABLES, f"091–094 created {sorted(tables)}"
                        # The reference stubs use plain INTEGER keys, so every sequence is a migration's.
                        sequences = set(inspector.get_sequence_names(schema="public"))
                        assert sequences == {f"{table}_id_seq" for table in REVOKE_PARITY_TABLES}, sorted(sequences)
                        assert_private_objects(connection, "public", tables)
                        # 113's downgrade is a deliberate no-op: it must never hand access back.
                        for migration in migrations:
                            if migration.__name__ == "113_data_api_revoke_parity":
                                migration.downgrade()
                        assert_private_objects(connection, "public", tables)
                finally:
                    transaction.rollback()
        finally:
            scratch.dispose()
    print(
        f"PostgreSQL migrations 091–094 and 113 (scratch database): {no_role}; their tables and "
        "sequences stayed private under inherited PUBLIC/anon/authenticated grants, re-run and after "
        "113's downgrade."
    )


def verify():
    url = require_local_test_postgres(os.environ["DATABASE_URL"])
    # Before the transaction below creates its temporary roles: the no-role check needs a cluster without them.
    verify_data_api_revocations(url, load_migrations(REVOKE_PARITY_MIGRATIONS))
    engine = sa.create_engine(url)
    schema = "ux_check_" + uuid4().hex
    migrations = load_migrations(MIGRATIONS)
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            # These temporary role definitions roll back with the schema below.
            create_missing_data_api_roles(connection)
            connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(sa.text(f'SET LOCAL search_path TO "{schema}"'))
            grant_inherited_data_api_privileges(connection, schema)
            # Existing references only; these migrations must not depend on seed data.
            for table in ("companies", "users", "api_tokens", "work_centers", "work_order_operations", "time_entries"):
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
                # Second pass over existing objects: the fresh bootstrap's case, which only the guards make a no-op.
                for migration in migrations:
                    if migration.__name__ in {
                        "095_kiosk_production_receipts",
                        "096_working_calendars",
                        "097_team_workspaces",
                        "098_runtime_metrics",
                    }:
                        migration.upgrade()
                inspector = sa.inspect(connection)
                new_tables = set(inspector.get_table_names(schema=schema)) - {
                    "companies",
                    "users",
                    "api_tokens",
                    "work_centers",
                    "work_order_operations",
                    "time_entries",
                    "work_orders",
                    "documents",
                    "purchase_orders",
                    "po_receipts",
                }
                assert_private_objects(connection, schema, new_tables)
                nesting_triggers = trigger_names(connection, schema, NESTING_TABLES)
                assert nesting_triggers, "101–103 created no quote_nesting triggers"
                connection.execute(sa.text("INSERT INTO companies (id) VALUES (1)"))
                connection.execute(sa.text("INSERT INTO users (id) VALUES (1)"))
                assert_nesting_revision_guards(connection)
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
                assert_model_built_nesting_tables(connection, migrations, nesting_triggers)
            print(
                "PostgreSQL migrations 095–103 passed upgrade/downgrade twice, "
                "RLS and Data API table/sequence privilege checks, and p75 cohorts 1/4/5/8; "
                "101–103 over model-built tables kept exactly their triggers, RLS, Data API denial "
                "and revision guards."
            )
        finally:
            transaction.rollback()
    try:
        assert_nesting_run_races(engine)
        assert_spacing_policy_races(engine)
        assert_stock_piece_races(engine)
        assert_nesting_source_races(engine)
        # The retired quote-nesting APIs have no runtime verifiers. Their historical
        # database guards remain above; the live inventory API is still checked.
        # A separate process prevents FastAPI/auth/queue test doubles or startup
        # state from leaking into other checks. This child repeats the local/test
        # database guard and owns a disposable schema, never the E2E seed tables.
        subprocess.run(
            [sys.executable, '-m', 'scripts.verify_stock_piece_api_postgres'],
            check=True,
            timeout=60,
            stdin=subprocess.DEVNULL,
        )
    finally:
        engine.dispose()


if __name__ == "__main__":
    verify()
