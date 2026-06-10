"""Add time_entries.source (A0.1 adoption-telemetry channel tag)

Revision ID: 048_time_entry_source
Revises: 047_doc_type_shipping
Create Date: 2026-06-09

Context
-------
A0.1 adoption telemetry tags every labor record with the channel that produced
it so the adoption dashboard can compute clock-in coverage, digital completion
percentage, and backfill rate. The model (``app/models/time_entry.py``) gained::

    source = Column(String(20), nullable=True)

Deliberately a PLAIN ``VARCHAR(20)`` -- NOT a native enum and NOT a CHECK
constraint -- so adding a future channel never requires ``ALTER TYPE`` / a
constraint rewrite (contrast with the documenttype pain closed by 047). The
allowed values live only in the application-level ``TimeEntrySource`` enum
(kiosk | desktop | scanner | import | backfill); the DDL neither knows nor
cares. No server default: NULL means "channel unknown" (historical / paper-era
rows), never a guessed value. No index either -- a future analytics feature may
add a composite ``(company_id, source)`` index, but not here.

Idempotent and reversible
-------------------------
- Upgrade guards the ADD COLUMN with an inspector ``_has_column`` check
  (precedent: 046's ``_has_column`` guard on every shipments add_column, and
  006/036/040/043 before it). This matters because bootstrap is
  ``create_all() -> stamp -> upgrade`` (docs/DEVELOPMENT.md): a DB bootstrapped
  from the updated model already has the column when this migration runs over
  the stamp, and the guard makes that a clean no-op. Re-runs are likewise no-ops.
- Downgrade drops the column, guarded by the same check. Dialect-agnostic:
  plain ``op.drop_column`` works on Postgres and on the modern SQLite used for
  local dev / pytest (same as 043 / 046's downgrades).

Locking / operations note
-------------------------
Adding a NULLABLE column with NO default is a metadata-only change on
PostgreSQL: no table rewrite, no backfill, only a brief ACCESS EXCLUSIVE lock
to update the catalog (same note as 043). ``time_entries`` is a hot shop-floor
table, but this change does not scan or rewrite it. No deploy-ordering
constraint: old application code ignores the column; new code writes it only
when the client reports a channel, and reads tolerate NULL.

Revision id ``048_time_entry_source`` is 21 chars (<= 32) per the
create_all -> stamp -> upgrade bootstrap constraint (alembic_version.version_num
is varchar(32) on a freshly bootstrapped DB); see docs/DEVELOPMENT.md.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "048_time_entry_source"
down_revision = "047_doc_type_shipping"
branch_labels = None
depends_on = None

TABLE_NAME = "time_entries"
COLUMN_NAME = "source"


def _has_column(table_name: str, column_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(col["name"] == column_name for col in inspector.get_columns(table_name))


def upgrade() -> None:
    # Plain nullable VARCHAR(20), no default, no index, no constraint -- in
    # lock-step with app/models/time_entry.py::TimeEntry.source. Guarded so a
    # create_all-bootstrapped DB (column already present) and re-runs no-op.
    if not _has_column(TABLE_NAME, COLUMN_NAME):
        op.add_column(TABLE_NAME, sa.Column(COLUMN_NAME, sa.String(length=20), nullable=True))


def downgrade() -> None:
    if _has_column(TABLE_NAME, COLUMN_NAME):
        op.drop_column(TABLE_NAME, COLUMN_NAME)
