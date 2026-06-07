"""Backfill + harden optimistic-lock version columns (Batch 2 / SFI-2 / LOCK-1)

Revision ID: 038_optimistic_lock_backfill
Revises: 037_add_qms_soft_delete_columns
Create Date: 2026-06-07

Context
-------
Migration ``004_add_optimistic_locking`` already added a ``version`` column to
``work_order_operations`` and ``time_entries`` (server_default '1', NOT NULL).
However the ORM never mapped it, so optimistic locking was inert.

Batch 2 maps ``version_id_col`` on ``WorkOrderOperation`` and ``TimeEntry``
(app/models/work_order.py, app/models/time_entry.py). SQLAlchemy's native
version_id_col REQUIRES every row to carry a non-null managed version: an UPDATE
of a row whose ``version IS NULL`` would fail / break the version comparison.

004 added the column NOT NULL with server_default '1', so on a normally-migrated
DB no row should be NULL. This migration is a belt-and-suspenders guard for any
database that was partially migrated, restored from an older dump, or had the
column added without the default actually being backfilled: it

  1. backfills ``version = 1 WHERE version IS NULL`` (no data destroyed), then
  2. re-asserts the server_default '1' and NOT NULL constraint,

so the version_id_col mapping is provably safe before any locked write path is
exercised.

This revision is intentionally split from the partial-unique-index revision
(039) because that one uses CREATE INDEX CONCURRENTLY, which cannot run inside a
transaction; this backfill/ALTER work is plain transactional DDL/DML.

Idempotent (safe to re-run) and reversible. The downgrade is a no-op for the
backfill (you cannot "un-backfill" data) but documents that 004 still owns the
column lifecycle; it does not drop the column (004's downgrade does).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector

# revision identifiers, used by Alembic.
revision = "038_optimistic_lock_backfill"
down_revision = "037_add_qms_soft_delete_columns"
branch_labels = None
depends_on = None

# (table, column) pairs that gained an enforced version_id_col mapping in Batch 2.
VERSIONED_TABLES = ["work_order_operations", "time_entries"]


def _table_has_column(conn, table_name: str, column_name: str) -> bool:
    inspector = Inspector.from_engine(conn)
    try:
        columns = [col["name"] for col in inspector.get_columns(table_name)]
    except Exception:
        # Table does not exist (e.g. partial bootstrap) -> treat as absent.
        return False
    return column_name in columns


def upgrade() -> None:
    conn = op.get_bind()

    for table in VERSIONED_TABLES:
        if not _table_has_column(conn, table, "version"):
            # Defensive: if 004 was skipped for this table, add the column with
            # the same shape 004 would have created. This keeps the migration
            # self-sufficient for partially-migrated databases.
            op.add_column(
                table,
                sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            )
            # Freshly added with server_default -> all rows are 1; nothing to backfill.
            continue

        # 1. Backfill any NULL version values so version_id_col is safe.
        #    Parameter-free constant UPDATE; no rows are destroyed.
        op.execute(
            sa.text(f"UPDATE {table} SET version = 1 WHERE version IS NULL")  # nosec B608 - table from fixed allowlist
        )

        # 2. Re-assert server_default '1' and NOT NULL. alter_column is
        #    idempotent in effect (re-applying the same constraint is a no-op).
        op.alter_column(
            table,
            "version",
            existing_type=sa.Integer(),
            nullable=False,
            server_default="1",
        )


def downgrade() -> None:
    # The version column itself is owned by 004_add_optimistic_locking; this
    # revision only backfilled data and re-asserted constraints that 004 already
    # established, so there is nothing structural to reverse here. We intentionally
    # do NOT drop the column (that would orphan the version_id_col mapping and is
    # 004's responsibility). The backfilled data is correct under either mapping
    # state, so the downgrade is a safe no-op.
    pass
