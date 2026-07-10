"""Regression coverage for 063_scrap_reason_codes_oee (Lean Phase 1, issue #88).

Follows the source-scan/wiring idiom of tests/test_migration_059_060_supabase_
hardening.py: the migration's live-Postgres behavior (FK adds, dedupe DELETE,
expression-index build) was rehearsed by its author; what can regress from
inside this SQLite-based suite is locked here:

1. Script-directory wiring (unit): single head; 063 revises
   ``062_estimate_job_actuals``; the revision id fits alembic_version's
   varchar(32) (create_all -> stamp -> upgrade bootstrap constraint); the module
   imports and exposes callable ``upgrade()``/``downgrade()``.
2. Source invariants (unit): the new-table RLS convention
   (``ENABLE ROW LEVEL SECURITY`` on scrap_reason_codes, docs/SUPABASE_SECURITY.md);
   the per-tenant unique (``company_id, code`` -- NOT a global unique, the
   DowntimeReasonCode defect deliberately not copied); the OEE unique key on
   ``COALESCE(shift, '')``; the pre-flight dedupe KEEPING the most recently
   updated row (ORDER BY updated_at DESC ... rn = 1 survives, rn > 1 deleted)
   inside the same transaction as the index build.
3. Model/migration lock-step (unit): the SQLite create_all path must build the
   SAME rules the migration builds on Postgres -- the model carries the
   ``uq_oee_company_wc_date_shift`` COALESCE expression index and the
   ``uq_scrap_reason_codes_company_code`` constraint, and ``ScrapReasonCode.code``
   is NOT globally unique. (The enforcement behavior itself -- 409s on the
   duplicate key, cross-tenant same-code creates -- is exercised end-to-end in
   tests/jobs/test_oee_service_and_cron.py and tests/api/test_scrap_reason_codes_crud.py.)
"""

import importlib.util
import os
import re

import pytest

from alembic.config import Config
from alembic.script import ScriptDirectory

pytestmark = [pytest.mark.unit]

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERSIONS_DIR = os.path.join(BACKEND_DIR, "alembic", "versions")

REVISION_063 = "063_scrap_reason_codes_oee"
MIGRATION_FILE = "063_scrap_reason_codes_oee.py"
DOWN_REVISION = "062_estimate_job_actuals"


def _script_directory() -> ScriptDirectory:
    cfg = Config()
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))
    return ScriptDirectory.from_config(cfg)


def _load_module():
    path = os.path.join(VERSIONS_DIR, MIGRATION_FILE)
    spec = importlib.util.spec_from_file_location("_migtest_063", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source() -> str:
    with open(os.path.join(VERSIONS_DIR, MIGRATION_FILE)) as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# 1. Script wiring
# ---------------------------------------------------------------------------


def test_single_head_and_revision_chain():
    scripts = _script_directory()
    heads = scripts.get_heads()
    assert len(heads) == 1, f"multiple alembic heads: {heads}"

    revision = scripts.get_revision(REVISION_063)
    assert revision.down_revision == DOWN_REVISION


def test_revision_id_fits_alembic_version_varchar32():
    # A freshly bootstrapped prod DB has alembic_version.version_num varchar(32).
    assert len(REVISION_063) <= 32


def test_module_loads_and_exposes_upgrade_downgrade():
    module = _load_module()
    assert module.revision == REVISION_063
    assert module.down_revision == DOWN_REVISION
    assert callable(module.upgrade)
    assert callable(module.downgrade)


# ---------------------------------------------------------------------------
# 2. Source invariants
# ---------------------------------------------------------------------------


def test_new_table_gets_row_level_security():
    """docs/SUPABASE_SECURITY.md convention: every new table ENABLEs RLS
    (deny-by-default posture; the Security Advisor lints rls_disabled_in_public)."""
    source = _source()
    assert re.search(r'ALTER TABLE public\."scrap_reason_codes" ENABLE ROW LEVEL SECURITY', source)


def test_scrap_code_uniqueness_is_per_tenant_not_global():
    source = _source()
    assert 'sa.UniqueConstraint("company_id", "code", name="uq_scrap_reason_codes_company_code")' in source
    # The known DowntimeReasonCode defect (a global unique on code) is not copied:
    # no bare unique=True on the code column definition.
    code_column = re.search(r'sa\.Column\("code",[^\n]*\)', source)
    assert code_column is not None
    assert "unique=True" not in code_column.group(0)


def test_oee_unique_index_uses_coalesced_shift_key():
    """NULL shift and '' shift must be the SAME record key (Postgres treats NULLs
    as distinct in plain unique constraints, hence the expression index)."""
    source = _source()
    assert "uq_oee_company_wc_date_shift" in source
    assert re.search(
        r'\["company_id",\s*"work_center_id",\s*"record_date",\s*sa\.text\("COALESCE\(shift, \'\'\)"\)\]',
        source,
    )


def test_oee_dedupe_keeps_the_most_recently_updated_row():
    """Pre-flight dedupe rule: rank per (company, wc, date, COALESCE(shift,''))
    by updated_at DESC (NULLS LAST), then created_at DESC, then id DESC; rn=1
    survives, rn>1 is deleted -- in the same transaction as the index build."""
    module = _load_module()
    ranked = module._OEE_RANKED_DUPES
    assert "PARTITION BY company_id, work_center_id, record_date, COALESCE(shift, '')" in ranked
    assert "ORDER BY updated_at DESC NULLS LAST, created_at DESC NULLS LAST, id DESC" in ranked

    source = _source()
    delete = re.search(
        r"DELETE FROM oee_records\s+WHERE id IN \(SELECT id FROM \(.*?\) ranked WHERE rn > 1\)", source, re.S
    )
    assert delete is not None, "the dedupe must delete only rn > 1 (the non-keepers)"


def test_fk_guard_checks_constrained_column_not_name():
    """Idempotency on the create_all-bootstrapped path: the FK guard must match
    ANY existing FK on scrap_reason_code_id (auto-named <table>_<col>_fkey), not
    just the migration's own constraint name."""
    module = _load_module()
    assert callable(module._has_fk_on_column)
    assert [t for t, _ in module.FK_TARGETS] == ["time_entries", "work_order_operations", "work_orders"]


# ---------------------------------------------------------------------------
# 3. Model/migration lock-step (the SQLite create_all path builds the same rules)
# ---------------------------------------------------------------------------


def test_oee_model_carries_the_same_unique_expression_index():
    from app.models.oee import OEERecord

    indexes = {index.name: index for index in OEERecord.__table__.indexes}
    assert "uq_oee_company_wc_date_shift" in indexes
    index = indexes["uq_oee_company_wc_date_shift"]
    assert index.unique is True
    rendered = [str(expr) for expr in index.expressions]
    assert rendered[:3] == ["oee_records.company_id", "oee_records.work_center_id", "oee_records.record_date"]
    assert "COALESCE(shift, '')" in rendered[3]


def test_scrap_reason_model_matches_migration_constraints():
    from sqlalchemy import UniqueConstraint

    from app.models.scrap_reason import ScrapReasonCode

    constraints = [c for c in ScrapReasonCode.__table__.constraints if isinstance(c, UniqueConstraint)]
    named = {c.name: [col.name for col in c.columns] for c in constraints}
    assert named.get("uq_scrap_reason_codes_company_code") == ["company_id", "code"]
    assert ScrapReasonCode.__table__.columns["code"].unique is not True


def test_new_columns_exist_on_the_models_with_safe_defaults():
    from app.models.oee import OEERecord
    from app.models.work_order import WorkOrderOperation

    calculation_source = OEERecord.__table__.columns["calculation_source"]
    assert calculation_source.nullable is False
    assert calculation_source.server_default is not None

    reworked = WorkOrderOperation.__table__.columns["quantity_reworked"]
    assert reworked.server_default is not None  # pre-existing rows read 0, not NULL
