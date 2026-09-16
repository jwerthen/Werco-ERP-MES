"""Upgrade/downgrade and immutable evidence checks on disposable schemas."""

import importlib.util
from io import StringIO
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from alembic.migration import MigrationContext
from alembic.operations import Operations


def _migration():
    path = Path(__file__).parents[1] / "alembic/versions/106_fabrication_quoting.py"
    spec = importlib.util.spec_from_file_location("fabrication_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_preserves_existing_quote_and_enforces_revision_immutability():
    engine = create_engine("sqlite://")
    module = _migration()
    with engine.begin() as connection:
        for name in ("companies", "users", "customers", "quotes"):
            connection.execute(text(f"CREATE TABLE {name} (id INTEGER PRIMARY KEY)"))
        connection.execute(text("INSERT INTO quotes VALUES (42)"))
        module.op = Operations(MigrationContext.configure(connection))
        module.upgrade()
        connection.execute(
            text(
                "INSERT INTO fabrication_quotes (id,company_id,title,status,revision,plan_json,calculation_json,created_by,created_at,updated_at) VALUES (1,1,'Example','draft',1,'{}','{}',1,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO fabrication_quote_revisions (id,company_id,quote_id,revision,action,snapshot_json,content_sha256,note,created_by,created_at) VALUES (1,1,1,1,'create','{}','abc','',1,CURRENT_TIMESTAMP)"
            )
        )
        with pytest.raises(IntegrityError, match="immutable"):
            connection.execute(text("UPDATE fabrication_quote_revisions SET note='changed' WHERE id=1"))
        with pytest.raises(IntegrityError, match="immutable"):
            connection.execute(text("DELETE FROM fabrication_quote_revisions WHERE id=1"))
        assert connection.scalar(text("SELECT id FROM quotes")) == 42
        module.downgrade()
        assert connection.scalar(text("SELECT id FROM quotes")) == 42
        assert not connection.scalar(text("SELECT count(*) FROM sqlite_master WHERE name LIKE 'fabrication_%'"))


def test_postgres_migration_emits_tenant_and_immutable_controls():
    module = _migration()
    sql = StringIO()
    module.op = Operations(
        MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": sql})
    )
    module.upgrade()
    result = sql.getvalue()
    for table in (
        "fabrication_quotes",
        "fabrication_quote_revisions",
        "fabrication_quote_files",
        "fabrication_quote_actuals",
    ):
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in result
        assert f"REVOKE ALL ON TABLE {table} FROM PUBLIC" in result
    assert "BEFORE TRUNCATE ON fabrication_quote_revisions" in result
    assert "CONSTRAINT fk_fqa_revision FOREIGN KEY(quote_id, quote_revision)" in result
