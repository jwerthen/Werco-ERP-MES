"""Add TV-pairing setup-code columns to display_tokens

Revision ID: 065_display_token_setup_code
Revises: 064_visitor_log_entered_by
Create Date: 2026-07-15

Context
-------
Adds four NULLABLE columns to the existing ``display_tokens`` table
(app/models/display_token.py::DisplayToken) for the one-time TV-pairing
"setup code" flow:

- ``setup_code_hash`` (String(64), indexed) — SHA-256 hex of the NORMALIZED
  one-time setup code. The plaintext code is NEVER stored; the claim endpoint
  hashes what the TV types and looks the row up by this column.
- ``setup_code_expires_at`` (DateTime) — the code's own expiry, ~15 minutes,
  independent of the token's ``expires_at``. Naive UTC, like ``expires_at``.
- ``setup_code_used_at`` (DateTime) — single-use marker, set on first
  successful claim.
- ``dept`` (String(50)) — optional per-TV work-center-type preset the claim
  response hands back so the person at the TV never types a query param.

All four are NULL for every pre-existing row and are never backfilled. No data
is written; the tamper-evident ``audit_log`` table is untouched.

Shape / compliance
------------------
``display_tokens`` is an EXISTING table: it already carries the TenantMixin
non-null ``company_id`` + index from 050, and RLS is already enabled on it —
059 (``059_supabase_rls_hardening``) enabled ROW LEVEL SECURITY dynamically on
EVERY ``public`` table, and ``display_tokens`` was created in 050, before 059
ran (confirmed against 059's ``_public_tables_without_rls`` loop). So this
additive migration needs no RLS statement of its own.

The lookup index ``ix_display_tokens_setup_code_hash`` is NON-unique and uses
the model's ``index=True`` default name so the create_all bootstrap path and
this migration converge on the same object (precedent 050). Single-use is
enforced by the claim flow via ``setup_code_used_at``, not by a uniqueness
constraint.

Idempotent and reversible
-------------------------
- Upgrade guards each ADD COLUMN with ``_has_column`` and guards the index by
  CONSTRAINED COLUMN (``_index_names_on_column``), not just by name, so the
  create_all -> stamp -> upgrade bootstrap path (where the model's
  ``index=True`` already built the identically-named index) no-ops rather
  than erroring (precedent: 064's by-column FK guard).
- Downgrade drops any index on ``setup_code_hash`` by REFLECTED name (covers
  both the migration-built and create_all-built variants), then drops the four
  columns, each guarded. On SQLite the column drops run in batch mode, which
  recreates the table without them (precedent 063/064); on Postgres they are
  plain guarded ``DROP COLUMN``s.

Locking / operations note
-------------------------
ADD COLUMN (nullable, no default) is metadata-only: a brief ACCESS EXCLUSIVE
lock and no table rewrite. CREATE INDEX (non-CONCURRENT) takes a SHARE lock on
``display_tokens`` for the build, but the table holds a handful of rows per
tenant (one per wall-mounted TV), so the build is instantaneous. Deploy
ordering: run before app code that writes these columns; old code ignores them.

Revision id ``065_display_token_setup_code`` is 28 chars (<= 32) per the
create_all -> stamp -> upgrade bootstrap constraint (alembic_version.version_num
is varchar(32) on a freshly bootstrapped DB); see docs/DEVELOPMENT.md.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "065_display_token_setup_code"
down_revision = "064_visitor_log_entered_by"
branch_labels = None
depends_on = None

TABLE_NAME = "display_tokens"
HASH_COLUMN = "setup_code_hash"
# Matches the model's index=True default naming (ix_%(column_0_label)s) so the
# create_all bootstrap path and this migration converge on one index.
INDEX_NAME = "ix_display_tokens_setup_code_hash"

# Kept in lock-step with app/models/display_token.py::DisplayToken. All
# nullable, no server defaults -- no backfill, no table rewrite.
COLUMN_NAMES = [HASH_COLUMN, "setup_code_expires_at", "setup_code_used_at", "dept"]


def _new_columns() -> list:
    """Fresh Column objects per call (a Column instance binds to one table)."""
    return [
        # SHA-256 hex of the normalized one-time TV setup code -- NEVER the
        # plaintext code.
        sa.Column(HASH_COLUMN, sa.String(length=64), nullable=True),
        # Setup-code expiry (~15 minutes). Naive UTC, like expires_at.
        sa.Column("setup_code_expires_at", sa.DateTime(), nullable=True),
        # Single-use marker -- set on first successful claim.
        sa.Column("setup_code_used_at", sa.DateTime(), nullable=True),
        # Optional per-TV work-center-type preset returned by the claim.
        sa.Column("dept", sa.String(length=50), nullable=True),
    ]


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    return _inspector().has_table(table_name)


def _has_column(table_name: str, column_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(col["name"] == column_name for col in _inspector().get_columns(table_name))


def _index_names_on_column(table_name: str, column_name: str) -> list:
    """Names of indexes covering exactly this column.

    Checked by covered column rather than name so both the migration-built and
    the create_all-bootstrapped index (identically named here, but the guard
    does not depend on that) are found (precedent: 064's by-column FK guard).
    """
    if not _has_table(table_name):
        return []
    return [
        ix["name"]
        for ix in _inspector().get_indexes(table_name)
        if ix.get("column_names") == [column_name] and ix.get("name")
    ]


def upgrade() -> None:
    for column in _new_columns():
        if not _has_column(TABLE_NAME, column.name):
            op.add_column(TABLE_NAME, column)

    # Non-unique lookup index on the code hash. No-ops when the create_all
    # bootstrap path already built it via the model's index=True.
    if not _index_names_on_column(TABLE_NAME, HASH_COLUMN):
        op.create_index(INDEX_NAME, TABLE_NAME, [HASH_COLUMN], unique=False)


def downgrade() -> None:
    conn = op.get_bind()

    # Drop the index first (by reflected name -- covers the migration-built and
    # any differently-named create_all-built variant). SQLite also cannot drop
    # a column that is still named in an index.
    for actual_index_name in _index_names_on_column(TABLE_NAME, HASH_COLUMN):
        op.drop_index(actual_index_name, table_name=TABLE_NAME)

    existing = [name for name in COLUMN_NAMES if _has_column(TABLE_NAME, name)]
    if not existing:
        return

    if conn.dialect.name == "sqlite":
        # Batch mode recreates the table without the columns (precedent 063/064)
        # rather than relying on SQLite's version-gated ALTER ... DROP COLUMN.
        with op.batch_alter_table(TABLE_NAME) as batch_op:
            for name in existing:
                batch_op.drop_column(name)
    else:
        for name in existing:
            op.drop_column(TABLE_NAME, name)
