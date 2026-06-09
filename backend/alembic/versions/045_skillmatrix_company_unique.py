"""Make skill_matrix unique tenant-scoped (company_id, user_id, work_center_id)

Revision ID: 045_skillmatrix_company_uq
Revises: 044_certificate_of_conformance
Create Date: 2026-06-09

Context
-------
``SkillMatrix`` (``app/models/operator_certification.py``) is a ``TenantMixin``
table, so every row carries ``company_id``. Its unique constraint, however, was
left global: migration 024 (``024_add_missing_module_tables``) created
``skill_matrix`` with ``UniqueConstraint('user_id', 'work_center_id',
name='uq_user_work_center')``, and migration 026 (``026_add_multi_tenancy``)
added the ``company_id`` column to ``skill_matrix`` but -- unlike the seven
tables in its ``UNIQUE_CONSTRAINT_CHANGES`` list -- never widened this
constraint. Migration 027 widened five more tenant uniques but again skipped
``skill_matrix``. The live constraint is therefore still
``uq_user_work_center`` on ``(user_id, work_center_id)`` (confirmed against
024/026/027). That makes a (user, work_center) pair unique GLOBALLY across all
tenants, which is a tenant-isolation correctness gap: two companies cannot both
record the same user_id/work_center_id skill row, and uniqueness leaks across
the tenant boundary.

This migration replaces ``uq_user_work_center`` with
``uq_skill_matrix_company_user_wc`` on ``(company_id, user_id, work_center_id)``
so the pair is unique PER TENANT, matching the rest of the tenant tables.

No pre-flight dedup needed (verified)
-------------------------------------
The new constraint is strictly LOOSER than the old one: it adds ``company_id``
as a leading column to the same ``(user_id, work_center_id)`` tuple. Any set of
rows that satisfied unique ``(user_id, work_center_id)`` is necessarily still
unique under ``(company_id, user_id, work_center_id)`` -- adding a column to a
unique key can only ever relax it, never tighten it. So no existing row can
violate the new constraint and no pre-flight dedupe pass is required (unlike
``039_uq_open_time_entry``, which tightened a key and therefore had to dedupe).

SQLite / create_all path
------------------------
SQLite (local dev / pytest ``create_all`` path) cannot ``ALTER TABLE ... DROP
CONSTRAINT``, and it never needs to here: ``create_all`` builds ``skill_matrix``
directly from the model's updated ``__table_args__``, so a freshly bootstrapped
SQLite DB already has ``uq_skill_matrix_company_user_wc`` and no
``uq_user_work_center`` -- byte-for-byte the Postgres end state. The
drop/recreate is therefore Postgres-guarded (precedent: ``043``'s
``_is_postgres`` guard); on SQLite both ``upgrade`` and ``downgrade`` are no-ops.
The model ``__table_args__`` must stay in lock-step with the Postgres end state.

Idempotent and reversible
-------------------------
- Upgrade: ``DROP CONSTRAINT IF EXISTS uq_user_work_center`` then
  ``ADD CONSTRAINT IF NOT EXISTS``-style guarded create (inspector check) of
  ``uq_skill_matrix_company_user_wc`` -- a re-run is a clean no-op. Raw
  ``IF EXISTS`` DROP is used (precedent: ``027``) so a missing constraint does
  not abort Postgres' transactional DDL.
- Downgrade: drops ``uq_skill_matrix_company_user_wc`` (guarded) and restores
  ``uq_user_work_center`` on ``(user_id, work_center_id)``.

Locking / operations note
-------------------------
Dropping and adding a unique constraint takes a brief ACCESS EXCLUSIVE lock and
the ADD validates uniqueness with a full scan of ``skill_matrix`` (a small,
low-write table -- one row per operator/work-center pairing), so the lock window
is short. No backfill and no deploy-ordering constraint: the looser constraint
accepts every row the old one did, so it is safe to apply before or after the
model deploy.

Revision id is 26 chars (<= 32) per the create_all -> stamp -> upgrade bootstrap
constraint documented in docs/DEVELOPMENT.md (alembic_version.version_num is
varchar(32) on a freshly bootstrapped DB).
"""

from alembic import op
from sqlalchemy.engine.reflection import Inspector

# revision identifiers, used by Alembic.
revision = "045_skillmatrix_company_uq"
down_revision = "044_certificate_of_conformance"
branch_labels = None
depends_on = None

TABLE_NAME = "skill_matrix"
OLD_CONSTRAINT = "uq_user_work_center"
OLD_COLUMNS = ["user_id", "work_center_id"]
NEW_CONSTRAINT = "uq_skill_matrix_company_user_wc"
NEW_COLUMNS = ["company_id", "user_id", "work_center_id"]


def _is_postgres(conn) -> bool:
    return conn.dialect.name == "postgresql"


def _has_unique_constraint(conn, table_name: str, constraint_name: str) -> bool:
    inspector = Inspector.from_engine(conn)
    try:
        names = {uc["name"] for uc in inspector.get_unique_constraints(table_name)}
    except Exception:
        return False
    return constraint_name in names


def upgrade() -> None:
    conn = op.get_bind()
    if not _is_postgres(conn):
        # SQLite (create_all / pytest path) already builds the new constraint from
        # the model's __table_args__ and cannot ALTER ... DROP CONSTRAINT. No-op.
        return

    # Drop the old global constraint. Raw IF EXISTS (precedent: 027) so a missing
    # constraint cannot abort Postgres' transactional DDL on re-run.
    op.execute(f"ALTER TABLE {TABLE_NAME} DROP CONSTRAINT IF EXISTS {OLD_CONSTRAINT}")

    # Create the tenant-scoped constraint only if absent (idempotent re-run).
    if not _has_unique_constraint(conn, TABLE_NAME, NEW_CONSTRAINT):
        op.create_unique_constraint(NEW_CONSTRAINT, TABLE_NAME, NEW_COLUMNS)


def downgrade() -> None:
    conn = op.get_bind()
    if not _is_postgres(conn):
        return

    op.execute(f"ALTER TABLE {TABLE_NAME} DROP CONSTRAINT IF EXISTS {NEW_CONSTRAINT}")

    if not _has_unique_constraint(conn, TABLE_NAME, OLD_CONSTRAINT):
        op.create_unique_constraint(OLD_CONSTRAINT, TABLE_NAME, OLD_COLUMNS)
