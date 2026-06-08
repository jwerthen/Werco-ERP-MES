"""Add backflush_components opt-in flag to parts (Batch 6 / FG receipt + backflush)

Revision ID: 040_add_part_backflush_flag
Revises: 039_uq_open_time_entry
Create Date: 2026-06-07

Context
-------
Batch 6 of the work-order-completion remediation (rank 9): on WO/operation
completion the system will ALWAYS receive finished goods into inventory (assign
an as-built lot + record genealogy) and OPTIONALLY backflush (auto-consume) the
part's BOM components.

Backflush must be OPT-IN PER PART, defaulting OFF, so it never double-counts
material a shop already issued manually. This migration adds the single flag the
backend logic keys off:

    parts.backflush_components  BOOLEAN NOT NULL DEFAULT FALSE

Shape / safety
--------------
- ``server_default='false'`` backfills every existing parts row to FALSE in the
  same ALTER (safe, non-null add against a populated table — the column is born
  with a value, so no separate backfill + not-null step is needed).
- The model (``app/models/part.py``) declares the matching
  ``server_default="false"`` so the ``create_all`` bootstrap path produces the
  identical column definition (NOT NULL DEFAULT false) and a stamped-baseline DB
  stays consistent with a migrated one.
- ``parts`` is a SoftDeleteMixin table; this migration only ADDS a column and
  never hard-deletes rows.

Idempotent and reversible
-------------------------
- Upgrade guards with an inspector column check (the precedent set by
  006/037), so re-running is a no-op.
- Downgrade drops the column, also guarded, so it round-trips cleanly.

Revision id is 27 chars (<= 32) per the create_all -> stamp -> upgrade bootstrap
constraint documented in docs/DEVELOPMENT.md (alembic_version.version_num is
varchar(32) on a freshly bootstrapped DB).

Locking note
------------
A nullable/defaulted column add with a constant ``server_default`` is a metadata-
only change on PostgreSQL 11+ (no full table rewrite), so this takes a brief
ACCESS EXCLUSIVE lock but does not scan/rewrite ``parts``. No backfill pass and
no deploy-ordering constraint relative to the backend rollout: the column simply
defaults to FALSE until the backflush feature is enabled per part.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector

# revision identifiers, used by Alembic.
revision = "040_add_part_backflush_flag"
down_revision = "039_uq_open_time_entry"
branch_labels = None
depends_on = None

TABLE_NAME = "parts"
COLUMN_NAME = "backflush_components"


def _table_has_column(conn, table_name: str, column_name: str) -> bool:
    inspector = Inspector.from_engine(conn)
    try:
        columns = [col["name"] for col in inspector.get_columns(table_name)]
    except Exception:
        return False
    return column_name in columns


def upgrade() -> None:
    conn = op.get_bind()

    if not _table_has_column(conn, TABLE_NAME, COLUMN_NAME):
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
    conn = op.get_bind()

    if _table_has_column(conn, TABLE_NAME, COLUMN_NAME):
        op.drop_column(TABLE_NAME, COLUMN_NAME)
