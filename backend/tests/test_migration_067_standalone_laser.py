"""Coverage for 067_standalone_laser_nest_wo (standalone laser-nest work orders).

067 is ALTER-only over two existing tables:

- ``work_orders.part_id`` DROP NOT NULL + table CHECK
  ``ck_work_orders_part_required_unless_laser``
  ("part_id IS NOT NULL OR work_order_type = 'laser_cutting'"), and
- ``laser_nest_packages.parent_work_order_id`` DROP NOT NULL.

Two complementary layers, mirroring the migration-test idioms already in the
suite (tests/test_migration_064_visitor_entered_by.py):

1. Script wiring + source/model lock-step (unit) — single alembic head, the
   066->067 chain, the id fits alembic_version's varchar(32), the DDL is
   Postgres-only (both directions), each DROP NOT NULL is guarded by CURRENT
   reflected nullability, the CHECK add/drop is guarded via
   ``get_check_constraints``, the downgrade RAISES (not 053's print) when
   violating rows exist, and the model carries the byte-identical CHECK text so
   the create_all path and the migration converge on the same constraint.

2. A real upgrade -> downgrade -> upgrade round-trip (integration/slow) — the
   alembic CLI over a disposable SQLite file bootstrapped create_all ->
   stamp(066). On SQLite every direction is a guarded no-op (the dialect guard),
   so the assertions pin exactly that: the run never errors and the
   create_all-built schema (nullable columns) is untouched in both directions.
   The CHECK's behavioral enforcement (IntegrityError on a part-less production
   WO) is covered separately in tests/api/test_laser_nest_standalone.py.

Behavioral note pinned here for fixture authors: ``work_order_type`` defaults to
'production' Python-side, so a ``WorkOrder(part_id=None)`` that omits
``work_order_type`` violates the CHECK at flush — standalone-laser fixtures must
pass ``work_order_type='laser_cutting'`` explicitly.
"""

import importlib.util
import os
import subprocess
import sys

import pytest
import sqlalchemy as sa

from alembic.config import Config
from alembic.script import ScriptDirectory

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSIONS_DIR = os.path.join(BACKEND_DIR, "alembic", "versions")

REVISION_067 = "067_standalone_laser_nest_wo"
MIGRATION_FILE = "067_standalone_laser_nest_wo.py"
DOWN_REVISION = "066_inspection_not_required"

WO_TABLE = "work_orders"
PART_COLUMN = "part_id"
PKG_TABLE = "laser_nest_packages"
PARENT_COLUMN = "parent_work_order_id"
CHECK_NAME = "ck_work_orders_part_required_unless_laser"
CHECK_CONDITION = "part_id IS NOT NULL OR work_order_type = 'laser_cutting'"


def _script_directory() -> ScriptDirectory:
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))
    return ScriptDirectory.from_config(cfg)


def _load_module():
    path = os.path.join(VERSIONS_DIR, MIGRATION_FILE)
    spec = importlib.util.spec_from_file_location("_migtest_067", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source() -> str:
    with open(os.path.join(VERSIONS_DIR, MIGRATION_FILE)) as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# 1. Script wiring
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_single_head_and_revision_chain():
    scripts = _script_directory()
    heads = scripts.get_heads()
    assert len(heads) == 1, f"multiple alembic heads: {heads}"
    assert heads[0] == REVISION_067

    revision = scripts.get_revision(REVISION_067)
    assert revision.down_revision == DOWN_REVISION


@pytest.mark.unit
def test_revision_id_fits_alembic_version_varchar32():
    # A freshly bootstrapped prod DB has alembic_version.version_num varchar(32);
    # the create_all -> stamp -> upgrade bootstrap constraint (docs/DEVELOPMENT.md).
    assert len(REVISION_067) <= 32


@pytest.mark.unit
def test_module_loads_and_exposes_upgrade_downgrade():
    module = _load_module()
    assert module.revision == REVISION_067
    assert module.down_revision == DOWN_REVISION
    assert callable(module.upgrade)
    assert callable(module.downgrade)
    # Constants describe exactly the objects this migration owns.
    assert module.WORK_ORDERS == WO_TABLE
    assert module.PART_ID_COLUMN == PART_COLUMN
    assert module.PACKAGES_TABLE == PKG_TABLE
    assert module.PARENT_WO_COLUMN == PARENT_COLUMN
    assert module.CHECK_NAME == CHECK_NAME
    assert module.CHECK_CONDITION == CHECK_CONDITION


# ---------------------------------------------------------------------------
# 2. Source invariants (idempotency + dialect handling + downgrade rigor)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_all_ddl_is_postgres_only_in_both_directions():
    """On SQLite the create_all bootstrap already built the nullable columns and
    the model-level CHECK, so BOTH directions must early-return."""
    source = _source()
    assert source.count("if not _is_postgres(conn):\n        return") == 2


@pytest.mark.unit
def test_alters_are_guarded_by_current_nullability_and_check_by_inspector():
    """Idempotency on re-run and on the create_all -> stamp -> upgrade path:
    DROP NOT NULL only fires when the column is currently NOT NULL, the CHECK
    add only when the named constraint is absent (precedent 053's by-state
    guards)."""
    module = _load_module()
    assert callable(module._column)
    assert callable(module._has_check_constraint)
    source = _source()
    assert source.count('if col is not None and not col["nullable"]:') == 2  # upgrade guards
    assert source.count('if col is not None and col["nullable"]:') == 2  # downgrade guards
    assert "if not _has_check_constraint(WORK_ORDERS, CHECK_NAME):" in source
    assert "get_check_constraints" in source


@pytest.mark.unit
def test_downgrade_raises_on_violating_rows_instead_of_half_applying():
    """Reversibility with rigor: re-tightening NOT NULL over standalone-laser
    rows must RAISE (deliberate deviation from 053's print-and-continue), for
    BOTH columns, and the CHECK is dropped via a guarded drop_constraint."""
    source = _source()
    assert source.count("raise RuntimeError(") == 2
    assert 'op.drop_constraint(CHECK_NAME, WORK_ORDERS, type_="check")' in source
    # The probe SQL is built from the module constants (f-string), so the raw
    # source carries the placeholder names.
    assert "WHERE {PARENT_WO_COLUMN} IS NULL" in source
    assert "WHERE {PART_ID_COLUMN} IS NULL" in source


@pytest.mark.unit
def test_alter_only_no_new_table_so_no_rls_needed():
    """067 creates no table, so the ENABLE ROW LEVEL SECURITY new-table
    convention does not apply (the 059-gate test keys off op.create_table)."""
    source = _source()
    assert "op.create_table" not in source


# ---------------------------------------------------------------------------
# 3. Model / migration lock-step (the create_all path builds the same objects)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_work_order_model_carries_nullable_part_id_and_byte_identical_check():
    from app.models.work_order import WorkOrder

    col = WorkOrder.__table__.columns[PART_COLUMN]
    assert col.nullable is True
    assert isinstance(col.type, sa.Integer)

    checks = {c.name: c for c in WorkOrder.__table__.constraints if isinstance(c, sa.CheckConstraint)}
    assert CHECK_NAME in checks, f"model CHECK {CHECK_NAME} missing; found {sorted(checks)}"
    # Byte-identical condition text: the create_all path (model) and the
    # migration path (067) must converge on the same constraint.
    assert str(checks[CHECK_NAME].sqltext) == CHECK_CONDITION


@pytest.mark.unit
def test_work_order_type_is_plain_string_not_native_enum():
    """The CHECK compares against the stored 'laser_cutting' literal; that is
    only dialect-safe because work_order_type is a String(50) storing the
    .value, not a native enum storing the member NAME (contrast 066)."""
    from app.models.work_order import WorkOrder

    col = WorkOrder.__table__.columns["work_order_type"]
    assert isinstance(col.type, sa.String) and not isinstance(col.type, sa.Enum)


@pytest.mark.unit
def test_package_model_carries_nullable_parent_work_order_fk():
    from app.models.laser_nest import LaserNestPackage

    col = LaserNestPackage.__table__.columns[PARENT_COLUMN]
    assert col.nullable is True
    fks = list(col.foreign_keys)
    assert len(fks) == 1
    assert fks[0].column.table.name == WO_TABLE
    assert fks[0].column.name == "id"


# ---------------------------------------------------------------------------
# 4. Real round-trip: upgrade -> downgrade -> upgrade over a bootstrapped DB
# ---------------------------------------------------------------------------


def _alembic(db_url: str, *args: str) -> None:
    """Run the alembic CLI in a subprocess pointed at the scratch DB."""
    env = {**os.environ, "DATABASE_URL": db_url}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"alembic {' '.join(args)} failed rc={result.returncode}\n" f"{result.stdout}\n{result.stderr}"
    )


def _column_nullable(engine, table: str, column: str) -> bool:
    inspector = sa.inspect(engine)
    (col,) = [c for c in inspector.get_columns(table) if c["name"] == column]
    return col["nullable"]


@pytest.mark.integration
@pytest.mark.slow
def test_migration_067_upgrade_downgrade_upgrade_round_trip(tmp_path):
    db_path = tmp_path / "mig067.db"
    db_url = f"sqlite:///{db_path}"

    # Bootstrap exactly as production does on an empty DB: create_all -> stamp.
    import app.models  # noqa: F401  (registers every table on Base.metadata)
    from app.db.database import Base

    engine = sa.create_engine(db_url)
    try:
        Base.metadata.create_all(engine)
        # create_all must build the post-067 shape straight from the models.
        assert _column_nullable(engine, WO_TABLE, PART_COLUMN), "create_all did not build part_id nullable"
        assert _column_nullable(engine, PKG_TABLE, PARENT_COLUMN), "create_all did not build parent FK nullable"

        _alembic(db_url, "stamp", DOWN_REVISION)

        # 1. Upgrade over a bootstrapped schema: guarded/dialect no-op, no error.
        _alembic(db_url, "upgrade", REVISION_067)
        assert _column_nullable(engine, WO_TABLE, PART_COLUMN)

        # 2. Downgrade: on SQLite the dialect guard makes this a documented
        #    no-op (the model-level CHECK belongs to the create_all schema) --
        #    it must complete cleanly and leave the schema untouched.
        _alembic(db_url, "downgrade", "-1")
        assert _column_nullable(engine, WO_TABLE, PART_COLUMN)
        assert _column_nullable(engine, PKG_TABLE, PARENT_COLUMN)

        # 3. Re-upgrade: clean no-op again.
        _alembic(db_url, "upgrade", REVISION_067)
        assert _column_nullable(engine, WO_TABLE, PART_COLUMN)
    finally:
        engine.dispose()
