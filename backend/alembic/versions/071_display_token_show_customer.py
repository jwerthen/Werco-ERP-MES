"""Add show_customer_names opt-in to display_tokens

Revision ID: 071_display_token_show_customer
Revises: 070_operation_last_report
Create Date: 2026-07-23

Context
-------
Adds ONE NOT-NULL boolean column to the existing ``display_tokens`` table
(app/models/display_token.py::DisplayToken):

- ``show_customer_names`` (Boolean, NOT NULL, server_default false) — whether a
  wallboard display is allowed to render the work order's customer name on the
  TV. Default False preserves the payload's long-standing "no customer names on
  a public screen" posture (CUI/AS9100D): every pre-existing display and every
  public shop-floor TV stays redacted; only an executive-office display
  explicitly provisioned with the flag reveals customer names. The gate is
  enforced server-side in ``build_wallboard_payload`` — a display token can
  never widen its own scope past this column.

The server default backfills every existing row to False in place, so no data
migration is needed and the tamper-evident ``audit_log`` table is untouched.

Numbering note
--------------
This revision chains off ``070_operation_last_report`` (the head on ``main``).
A concurrently-open branch (PR #149) introduces its own
``071_soft_delete_purchasing_ncr`` off the same ``070`` parent; whichever of the
two lands on ``main`` first takes ``071`` and the other must re-parent onto it —
this wallboard change is independent of #149 and merges on its own, so it takes
``071`` here. (Two migrations off one parent = two Alembic heads until the
second is re-parented; the migration-graph tests enforce a single head.)

Shape / compliance
------------------
``display_tokens`` is an EXISTING table: it already carries the TenantMixin
non-null ``company_id`` + index (from 050), and RLS is already enabled on it
(059 enabled ROW LEVEL SECURITY on every ``public`` table, and display_tokens
predates 059). So this additive migration needs no RLS statement of its own —
same rationale as 065, which added the setup-code columns to this table.

Idempotent and reversible
-------------------------
- Upgrade guards the ADD COLUMN with ``_has_column`` so the create_all -> stamp
  -> upgrade bootstrap path (where the model already built the column) no-ops
  rather than erroring (precedent 065).
- Downgrade drops the column, guarded. On SQLite the drop runs in batch mode
  (recreates the table without it; precedent 063/064/065); on Postgres it is a
  plain guarded ``DROP COLUMN``.

Locking / operations note
-------------------------
ADD COLUMN NOT NULL with a CONSTANT server default is metadata-only on
PostgreSQL 11+ (no table rewrite; brief ACCESS EXCLUSIVE lock), and
``display_tokens`` holds only a handful of rows per tenant. Deploy ordering: run
before app code that reads/writes the column; old code simply ignores it.

Revision id ``071_display_token_show_customer`` is 31 chars (<= 32) per the
create_all -> stamp -> upgrade bootstrap constraint (alembic_version.version_num
is varchar(32) on a freshly bootstrapped DB); see docs/DEVELOPMENT.md.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "071_display_token_show_customer"
down_revision = "070_operation_last_report"
branch_labels = None
depends_on = None

TABLE_NAME = "display_tokens"
COLUMN_NAME = "show_customer_names"


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    return _inspector().has_table(table_name)


def _has_column(table_name: str, column_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(col["name"] == column_name for col in _inspector().get_columns(table_name))


def upgrade() -> None:
    # NOT NULL with a constant server default: existing rows backfill to False
    # in place (metadata-only on PG 11+). Guarded so the create_all bootstrap
    # path (model already built the column) no-ops.
    if not _has_column(TABLE_NAME, COLUMN_NAME):
        op.add_column(
            TABLE_NAME,
            sa.Column(
                COLUMN_NAME,
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )


def downgrade() -> None:
    if not _has_column(TABLE_NAME, COLUMN_NAME):
        return

    conn = op.get_bind()
    if conn.dialect.name == "sqlite":
        # Batch mode recreates the table without the column (precedent 063/064/065)
        # rather than relying on SQLite's version-gated ALTER ... DROP COLUMN.
        with op.batch_alter_table(TABLE_NAME) as batch_op:
            batch_op.drop_column(COLUMN_NAME)
    else:
        op.drop_column(TABLE_NAME, COLUMN_NAME)
