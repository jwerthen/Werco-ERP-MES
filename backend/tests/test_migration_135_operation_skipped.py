"""Coverage for 135_operation_skipped: ``OperationStatus.SKIPPED`` + who / when / why columns.

The SQLite ``downgrade 088 -> upgrade head`` round trip in ``test_migration_fresh_bootstrap.py``
already diffs the rebuilt ``work_order_operations`` (FK names included) against ``create_all``.
This file pins what that cannot see:

1. **Script wiring** -- single head, the down_revision, an id that fits ``alembic_version``.
2. **The enum label is the member NAME** -- ``SKIPPED``, the form an unnamed ``SQLEnum`` without
   ``values_callable`` stores, never the lowercase wire value.
3. **Model / migration lock-step** -- types, nullability, the FK name and ``ON DELETE SET NULL``.
4. **The SQL PostgreSQL receives** -- offline rendering of both directions: the guarded
   ``ALTER TYPE ... ADD VALUE IF NOT EXISTS``, three nullable columns and one named FK, the lock
   timeout taken and handed back, no backfill and no RLS / role statement; the downgrade drops
   the FK and the columns and never touches the enum (PostgreSQL cannot drop a value).
"""

import importlib.util
import io
import os
import re

import pytest
from sqlalchemy.dialects import postgresql

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from app.models.work_order import OperationStatus, WorkOrderOperation

pytestmark = [pytest.mark.unit]

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MIGRATION_FILE = os.path.join(BACKEND_DIR, "alembic", "versions", "135_operation_skipped.py")
REVISION = "135_operation_skipped"
DOWN_REVISION = "134_part_customer_supplied"


def _module():
    spec = importlib.util.spec_from_file_location("_migtest_135", MIGRATION_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _render(function_name: str, dialect_name: str = "postgresql") -> str:
    module = _module()
    buffer = io.StringIO()
    context = MigrationContext.configure(dialect_name=dialect_name, opts={"as_sql": True, "output_buffer": buffer})
    module.op = Operations(context)
    with context.begin_transaction():
        getattr(module, function_name)()
    return " ".join(buffer.getvalue().split())


class TestScriptWiring:
    def test_single_head_and_down_revision(self):
        cfg = Config()
        cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))
        script = ScriptDirectory.from_config(cfg)
        assert script.get_heads() == [REVISION]
        assert script.get_revision(REVISION).down_revision == DOWN_REVISION
        assert len(REVISION) <= 32
        assert os.path.basename(MIGRATION_FILE) == f"{REVISION}.py"


class TestEnumLabel:
    def test_the_label_is_the_member_name(self):
        column_type = WorkOrderOperation.__table__.c.status.type
        assert column_type.values_callable is None  # stores NAMES
        assert "SKIPPED" in column_type.enums
        assert OperationStatus.SKIPPED.value == "skipped"
        assert _module().NEW_ENUM_VALUE == "SKIPPED"


class TestModelLockStep:
    def test_columns_match_the_model(self):
        table = WorkOrderOperation.__table__
        dialect = postgresql.dialect()
        expected = {"skipped_at": "TIMESTAMP WITHOUT TIME ZONE", "skipped_by": "INTEGER", "skip_reason": "VARCHAR(500)"}
        migration_columns = {column.name: column for column in _module()._columns()}
        for name, ddl in expected.items():
            assert table.c[name].nullable is True
            assert table.c[name].type.compile(dialect=dialect) == ddl
            assert migration_columns[name].type.compile(dialect=dialect) == ddl
            assert migration_columns[name].nullable is True
            assert table.c[name].server_default is None

    def test_the_fk_name_and_ondelete_match(self):
        fks = list(WorkOrderOperation.__table__.c.skipped_by.foreign_keys)
        assert len(fks) == 1
        assert fks[0].constraint.name == "fk_work_order_operations_skipped_by"
        assert fks[0].ondelete == "SET NULL"
        assert fks[0].column.table.name == "users"
        assert _module().SKIPPED_BY_FK == "fk_work_order_operations_skipped_by"


class TestOfflinePostgresSql:
    def test_upgrade(self):
        sql = _render("upgrade")
        assert "ALTER TYPE operationstatus ADD VALUE IF NOT EXISTS 'SKIPPED'" in sql
        for column in ("skipped_at TIMESTAMP WITHOUT TIME ZONE", "skipped_by INTEGER", "skip_reason VARCHAR(500)"):
            assert f"ADD COLUMN {column}" in sql
        assert re.search(
            r"ADD CONSTRAINT fk_work_order_operations_skipped_by FOREIGN KEY\(skipped_by\) REFERENCES users \(id\) "
            r"ON DELETE SET NULL",
            sql,
        )
        assert "SET LOCAL lock_timeout = '5s'" in sql and "SET LOCAL lock_timeout = DEFAULT" in sql
        assert not re.search(r"\b(UPDATE|INSERT|DELETE FROM)\b", sql)
        assert not re.search(
            r"ROW LEVEL SECURITY|REVOKE|GRANT|POLICY|pg_roles|\banon\b|authenticated|service_role", sql
        )
        assert "audit_log" not in sql

    def test_downgrade_drops_the_columns_and_leaves_the_enum(self):
        sql = _render("downgrade")
        assert "DROP CONSTRAINT fk_work_order_operations_skipped_by" in sql
        for column in ("skipped_at", "skipped_by", "skip_reason"):
            assert f"DROP COLUMN {column}" in sql
        assert "ALTER TYPE" not in sql
