"""Partial unique index on open time entries (Batch 2 / SFI-3)

Revision ID: 039_uq_open_time_entry
Revises: 038_optimistic_lock_backfill
Create Date: 2026-06-07

Context
-------
SFI-3: clock-in / start-operation create an open ``TimeEntry`` after a
check-then-insert guard (``WHERE user_id=? AND operation_id=? AND clock_out IS
NULL``) with NO database constraint backing it. Under a rapid double-submit (or
two operators racing) the check can pass twice and create two open rows for the
same (user_id, operation_id), enabling lost-update double counting on the
production read-modify-write.

This migration adds the missing invariant as a PARTIAL UNIQUE index:

    uq_open_time_entry ON time_entries (user_id, operation_id)
        WHERE clock_out IS NULL

so at most one OPEN entry can exist per (user, operation). Closed entries
(clock_out IS NOT NULL) are unconstrained, which is correct: a user may legitimately
clock the same operation many times over its life — only one can be open at once.

Pre-flight dedupe
-----------------
Existing production data may already contain duplicate OPEN rows that would make
the unique index creation FAIL. Before creating the index we resolve duplicates
WITHOUT destroying data:

    Rule: within each (user_id, operation_id) group that has >1 row with
    clock_out IS NULL, KEEP the most recent open row (greatest clock_in, ties
    broken by greatest id) as the single open entry. CLOSE every older open row
    in the group by setting clock_out = clock_in and duration_hours = 0.

Closing the stale rows with clock_out = clock_in yields a zero-length entry
(duration_hours = 0), so the dedupe contributes no spurious labor hours.

``quantity_produced`` is intentionally PRESERVED, not zeroed, on the closed
duplicate rows: if a duplicate open entry had already accumulated production,
that quantity stays recorded on the now-closed row so no production is lost.
The trade-off is precise: we zero only the *time* (duration_hours = 0) because
a duplicate open entry represents double-counted elapsed labor, but we keep the
*production* because the parts were really made and that count is real. The rows
are preserved (not deleted) for the audit trail. NOTE: ``time_entries`` is not a
SoftDeleteMixin table, so there is no is_deleted flag to set; closing the entry
is the non-destructive resolution.

Traceability of the dedupe (AS9100D labor records)
--------------------------------------------------
This dedupe is a DBA-driven mutation of labor records, so before the UPDATE we
SELECT the rows that will be closed and emit their identifying fields (id,
user_id, operation_id, clock_in, quantity_produced) to the migration's runtime
log via ``print`` (the convention used by the other migrations in this
directory, e.g. 003/004/005/010). The closed-row ids therefore appear in the
deploy/migration output, timestamped by the deploy, so an auditor can
reconstruct exactly which labor entries were altered and when. We deliberately
do NOT write to ``audit_log``: it is a tamper-evident hash chain and CLAUDE.md
forbids backfilling it out of band.

Locking / operations note
--------------------------
``time_entries`` is a high-write table, so the index is created with
``CREATE INDEX CONCURRENTLY`` inside an autocommit block to avoid taking an
ACCESS EXCLUSIVE lock that would block clock-in/out during deploy. CONCURRENTLY
cannot run inside a transaction, so the dedupe DML runs first in its own
(transactional) phase, and the index build runs after in an autocommit block.

Idempotent (IF NOT EXISTS / inspector guard) and reversible (downgrade drops the
index, also CONCURRENTLY so the drop doesn't block writers either).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.engine.reflection import Inspector

# revision identifiers, used by Alembic.
revision = "039_uq_open_time_entry"
down_revision = "038_optimistic_lock_backfill"
branch_labels = None
depends_on = None

INDEX_NAME = "uq_open_time_entry"
TABLE_NAME = "time_entries"


def _index_exists(conn, index_name: str) -> bool:
    inspector = Inspector.from_engine(conn)
    try:
        existing = {ix["name"] for ix in inspector.get_indexes(TABLE_NAME)}
    except Exception:
        return False
    # get_indexes does not list UNIQUE constraints separately here, but partial
    # unique indexes created via CREATE UNIQUE INDEX do show up as indexes.
    if index_name in existing:
        return True
    # Belt-and-suspenders: query pg_indexes directly (covers edge cases where
    # the reflection cache misses a concurrently-built index).
    row = conn.execute(
        sa.text("SELECT 1 FROM pg_indexes WHERE indexname = :name"),
        {"name": index_name},
    ).fetchone()
    return row is not None


def _is_postgres(conn) -> bool:
    return conn.dialect.name == "postgresql"


def upgrade() -> None:
    conn = op.get_bind()

    if not _is_postgres(conn):
        # Partial indexes + CONCURRENTLY are Postgres features. On SQLite (local
        # dev create_all path) we skip; the application-level guard still applies
        # and SQLite is not a concurrent multi-writer target.
        return

    # ---- Phase 1: pre-flight dedupe (transactional DML) ------------------
    # Close older open duplicates so the unique index can be built. Keep the most
    # recent open row per (user_id, operation_id); rank by clock_in then id.
    # operation_id IS NULL rows are excluded: a NULL operation_id is not part of
    # the (user_id, operation_id) uniqueness target and Postgres treats NULLs as
    # distinct in unique indexes anyway.

    # Traceability: identify the rows that WILL be closed and emit them to the
    # migration runtime log BEFORE mutating them, so the deploy output is a
    # standing record of which labor entries this DBA-driven dedupe altered (and,
    # via the deploy timestamp, when). This is NOT written to audit_log, which is
    # a tamper-evident hash chain that must not be backfilled out of band.
    rows_to_close = conn.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT
                    id,
                    user_id,
                    operation_id,
                    clock_in,
                    quantity_produced,
                    ROW_NUMBER() OVER (
                        PARTITION BY user_id, operation_id
                        ORDER BY clock_in DESC, id DESC
                    ) AS rn
                FROM time_entries
                WHERE clock_out IS NULL
                  AND operation_id IS NOT NULL
            )
            SELECT id, user_id, operation_id, clock_in, quantity_produced
            FROM ranked
            WHERE rn > 1
            ORDER BY user_id, operation_id, id
            """
        )
    ).fetchall()

    if rows_to_close:
        print(
            f"[039_uq_open_time_entry] Closing {len(rows_to_close)} duplicate OPEN "
            f"time_entries rows (setting clock_out=clock_in, duration_hours=0; "
            f"quantity_produced preserved). Closed-row ids for AS9100D traceability:"
        )
        for r in rows_to_close:
            print(
                f"[039_uq_open_time_entry]   closed time_entry id={r.id} "
                f"user_id={r.user_id} operation_id={r.operation_id} "
                f"clock_in={r.clock_in!s} quantity_produced={r.quantity_produced}"
            )
    else:
        print(
            "[039_uq_open_time_entry] No duplicate OPEN time_entries found; "
            "no rows closed."
        )

    op.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT
                    id,
                    clock_in,
                    ROW_NUMBER() OVER (
                        PARTITION BY user_id, operation_id
                        ORDER BY clock_in DESC, id DESC
                    ) AS rn
                FROM time_entries
                WHERE clock_out IS NULL
                  AND operation_id IS NOT NULL
            )
            UPDATE time_entries te
            SET clock_out = te.clock_in,
                duration_hours = 0
            FROM ranked
            WHERE te.id = ranked.id
              AND ranked.rn > 1
            """
        )
    )

    # ---- Phase 2: build the partial unique index (autocommit) ------------
    if _index_exists(conn, INDEX_NAME):
        return

    with op.get_context().autocommit_block():
        op.create_index(
            INDEX_NAME,
            TABLE_NAME,
            ["user_id", "operation_id"],
            unique=True,
            postgresql_concurrently=True,
            postgresql_where=sa.text("clock_out IS NULL"),
            if_not_exists=True,
        )


def downgrade() -> None:
    conn = op.get_bind()

    if not _is_postgres(conn):
        return

    if not _index_exists(conn, INDEX_NAME):
        return

    # Drop CONCURRENTLY as well so the rollback doesn't take ACCESS EXCLUSIVE on
    # the high-write table. Must run outside a transaction.
    with op.get_context().autocommit_block():
        op.drop_index(
            INDEX_NAME,
            table_name=TABLE_NAME,
            postgresql_concurrently=True,
            if_exists=True,
        )
