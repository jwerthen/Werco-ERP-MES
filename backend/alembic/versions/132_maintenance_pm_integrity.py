"""PM record integrity: the checklist snapshot a maintenance work order is opened with, and one open
work order per PM schedule in the database

Revision ID: 132_maintenance_pm_integrity
Revises: 131_document_access
Create Date: 2026-11-20

Context
-------
A preventive-maintenance (PM) work order answers for its PM schedule's checklist: completion
refuses (409) a step left unticked without a written reason, and every work-order response serves
the steps as ``schedule_checklist_items``. Until now both read the schedule's CURRENT checklist, so a
schedule edit or a PM import rewrote what an open work order was judged on, and what a completed or
cancelled one -- a final record -- reports it was judged on. Owner decision 2026-11-20: the schedule's
checklist is SNAPSHOTTED when the work order is opened; edits and imports bind the next work order.

The rule "a PM schedule takes one open work order at a time" (PR #350) lived only in the app, under
the schedule's row lock. This revision adds the database backstop.

This revision, every object guarded independently (the 088/124 style):

``maintenance_work_orders``
  - ``schedule_checklist_snapshot`` Text NULL: a JSON array of the schedule's checklist items, written
    by ``POST /maintenance/work-orders`` under the schedule's row lock. NULL on a work order without a
    schedule and on every row that exists before this revision: those keep reading the live schedule
    -- an open one until completing or cancelling it writes the steps it answered for; one already
    completed or cancelled for good (documented as a known limitation). NEVER BACKFILLED here -- the list a legacy work order
    was opened with is unknowable, and an UPDATE outside ``AuditService`` would break invariant 2.
  - unique index ``uq_maintenance_work_orders_open_schedule`` on ``(company_id, schedule_id)`` WHERE
    ``schedule_id IS NOT NULL AND status IN ('SCHEDULED', 'OVERDUE', 'IN_PROGRESS', 'ON_HOLD')``.
    ``SQLEnum(MaintenanceStatus)`` persists member NAMES, so the predicate names them; it is
    byte-identical to ``app.models.maintenance.OPEN_SCHEDULE_WORK_ORDER_PREDICATE`` (a test pins it)
    and is declared for PostgreSQL AND SQLite, as the model declares it.

Pre-existing duplicates FAIL the upgrade (owner decision 2026-11-20)
--------------------------------------------------------------------
``CREATE UNIQUE INDEX`` cannot be built over data that already holds two open work orders on one
schedule. Rather than let PostgreSQL fail with a bare duplicate-key error -- or silently skip the
index -- the upgrade first runs the duplicate probe and RAISES naming every offending schedule and
its work-order numbers. Nothing is repaired here: which of two open work orders is the real one is a
person's call. Cancel (or complete) the extra one through the app, which writes the audit trail, and
re-run the deploy. The read-only check to run BEFORE deploying is in the PR body and in
``docs/DEPLOYMENT_RUNBOOK.md``.

No RLS / REVOKE: the table already exists and ``059`` enabled RLS on every ``public`` table.

Downgrade
---------
Real: drops the index, then the column. LOSSY for the snapshots themselves -- after a downgrade every
work order reads its schedule's live checklist again, which is the pre-132 behavior.
"""

from collections import OrderedDict

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "132_maintenance_pm_integrity"
down_revision = "131_document_access"
branch_labels = None
depends_on = None

TABLE = "maintenance_work_orders"
SNAPSHOT_COLUMN = "schedule_checklist_snapshot"
OPEN_SCHEDULE_INDEX = "uq_maintenance_work_orders_open_schedule"
# Byte-identical to app.models.maintenance.OPEN_SCHEDULE_WORK_ORDER_PREDICATE (a test pins it).
OPEN_SCHEDULE_PREDICATE = "schedule_id IS NOT NULL AND status IN ('SCHEDULED', 'OVERDUE', 'IN_PROGRESS', 'ON_HOLD')"

# PostgreSQL only, and LOCAL (precedent 059/060/114/117/123/124): bounds the wait for the index
# build's SHARE lock and the column add's ACCESS EXCLUSIVE lock, then hands the normal timeout back.
LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = '5s'"
RESET_LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = DEFAULT"


def _offline() -> bool:
    # ``--sql`` has no database to inspect: render the canonical statements.
    return bool(op.get_context().as_sql)


def _dialect() -> str:
    return op.get_context().dialect.name


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    if _offline():
        return True
    return _inspector().has_table(table_name)


def _has_column(table_name: str, column_name: str) -> bool:
    if _offline() or not _has_table(table_name):
        return False
    return any(col["name"] == column_name for col in _inspector().get_columns(table_name))


def _has_index(table_name: str, index_name: str) -> bool:
    if _offline() or not _has_table(table_name):
        return False
    return any(ix.get("name") == index_name for ix in _inspector().get_indexes(table_name))


def open_work_order_duplicates(bind) -> "OrderedDict[tuple, list]":
    """``{(company_id, schedule_id): [wo_number, ...]}`` for every PM schedule holding more than one
    OPEN maintenance work order -- exactly the rows the unique index would refuse. Read-only."""
    rows = bind.execute(
        sa.text(
            f"SELECT company_id, schedule_id, wo_number FROM {TABLE} "
            f"WHERE {OPEN_SCHEDULE_PREDICATE} ORDER BY company_id, schedule_id, id"
        )
    ).fetchall()
    grouped: "OrderedDict[tuple, list]" = OrderedDict()
    for company_id, schedule_id, wo_number in rows:
        grouped.setdefault((company_id, schedule_id), []).append(wo_number)
    return OrderedDict((key, numbers) for key, numbers in grouped.items() if len(numbers) > 1)


def _refuse_duplicates() -> None:
    """Raise, naming them, if any schedule already holds two open work orders (owner decision
    2026-11-20: fail and fix by hand, never skip the index or pick a winner)."""
    duplicates = open_work_order_duplicates(op.get_bind())
    if not duplicates:
        return
    described = "; ".join(
        f"company {company_id}, PM schedule {schedule_id}: {', '.join(numbers)}"
        for (company_id, schedule_id), numbers in duplicates.items()
    )
    raise RuntimeError(
        f"132_maintenance_pm_integrity: {len(duplicates)} PM schedule(s) already hold more than one open "
        f"maintenance work order ({described}). A PM schedule takes one open work order at a time. Cancel or "
        "complete the extra one(s) in the app (which writes the audit trail), then re-run the migration. "
        "Nothing was changed."
    )


def upgrade() -> None:
    if not _has_table(TABLE):
        return
    postgres = _dialect() == "postgresql"
    offline = _offline()
    add_column = offline or not _has_column(TABLE, SNAPSHOT_COLUMN)
    add_index = offline or not _has_index(TABLE, OPEN_SCHEDULE_INDEX)
    if not (add_column or add_index):
        return
    if add_index and not offline:
        # Before any DDL, so a refusal changes nothing.
        _refuse_duplicates()
    if postgres:
        op.execute(LOCK_TIMEOUT_SQL)
    if add_column:
        # Nullable, no default: PostgreSQL adds it without rewriting the table, and SQLite takes a
        # plain ADD COLUMN (no batch rebuild needed).
        op.add_column(TABLE, sa.Column(SNAPSHOT_COLUMN, sa.Text(), nullable=True))
    if add_index:
        op.create_index(
            OPEN_SCHEDULE_INDEX,
            TABLE,
            ["company_id", "schedule_id"],
            unique=True,
            postgresql_where=sa.text(OPEN_SCHEDULE_PREDICATE),
            sqlite_where=sa.text(OPEN_SCHEDULE_PREDICATE),
        )
    if postgres:
        op.execute(RESET_LOCK_TIMEOUT_SQL)


def downgrade() -> None:
    if not _has_table(TABLE):
        return
    postgres = _dialect() == "postgresql"
    offline = _offline()
    drop_index = offline or _has_index(TABLE, OPEN_SCHEDULE_INDEX)
    drop_column = offline or _has_column(TABLE, SNAPSHOT_COLUMN)
    if not (drop_index or drop_column):
        return
    if postgres:
        op.execute(LOCK_TIMEOUT_SQL)
    if drop_index:
        op.drop_index(OPEN_SCHEDULE_INDEX, table_name=TABLE)
    if drop_column:
        # batch: a table rebuild on SQLite (portable across SQLite versions), a plain DROP COLUMN on
        # PostgreSQL. The index is gone first, so the rebuild does not carry it.
        with op.batch_alter_table(TABLE) as batch:
            batch.drop_column(SNAPSHOT_COLUMN)
    if postgres:
        op.execute(RESET_LOCK_TIMEOUT_SQL)
