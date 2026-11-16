"""Job cost subcontract element: ``CostEntryType.SUBCONTRACT`` + ``job_costs.actual_subcontract_cost``

Revision ID: 121_job_cost_subcontract
Revises: 120_production_calendar
Create Date: 2026-11-13

Context
-------
The work-order read now reports a closed job's cost by element (material / labor /
burden / subcontract) from its ``JobCost``. Subcontract (outside processing: plating,
heat treat, ...) could not be derived: ``CostEntryType`` was MATERIAL / LABOR / OVERHEAD /
OTHER, ``recalculate_totals`` folded OTHER into ``actual_overhead_cost``, and no PO line or
receipt carries a work-order link. So this revision adds the two schema halves of the new
element (``app/models/job_costing.py``):

1. ``job_costs.actual_subcontract_cost`` -- Float, NOT NULL, server default ``'0'``,
   Python default ``0.0``, no index. The denormalized total of a job's SUBCONTRACT
   entries, written by ``recalculate_totals`` beside the other ``actual_*`` columns so a
   ``JobCost`` stays self-consistent (``actual_total_cost`` never includes an element no
   column shows). The server default makes every pre-121 row read ``0`` -- which is TRUE,
   not a guess: no SUBCONTRACT entry could exist before this revision, so there is nothing
   to backfill. With a constant default the ADD COLUMN is metadata-only on PostgreSQL 11+.
2. The ``SUBCONTRACT`` label on the native PostgreSQL enum ``costentrytype`` that backs
   ``cost_entries.entry_type`` -- when that type exists.

The enum label is the UPPERCASE NAME ``SUBCONTRACT`` (load-bearing)
------------------------------------------------------------------
``CostEntry.entry_type`` is ``SQLEnum(CostEntryType)`` with no ``values_callable``, so
SQLAlchemy binds the member NAME: ``CostEntry.__table__.c.entry_type.type.enums`` is
``['MATERIAL', 'LABOR', 'OVERHEAD', 'OTHER', 'SUBCONTRACT']``. ``'subcontract'`` is only the
``.value`` the API serializes and must NOT be added to the type (precedent 066).

Two bootstrap paths, two column types -- hence "when the type exists"
---------------------------------------------------------------------
- ``create_all`` on PostgreSQL (the fresh-bootstrap path) renders ``entry_type
  costentrytype``: a native enum, which needs the label.
- Migration ``024`` created ``cost_entries.entry_type`` as ``VARCHAR(50)``. A database
  built through that path has NO ``costentrytype`` type, and an unconditional ``ALTER
  TYPE`` would abort the upgrade there (066 never had to consider this; its type always
  exists). A VARCHAR column takes ``'SUBCONTRACT'`` as it is.

So the ALTER runs inside a ``DO`` block that first resolves the type with ``to_regtype`` --
the same unqualified, ``search_path`` resolution the ``ALTER TYPE`` itself would use -- and
is a no-op when it does not resolve. Because the guard lives in the database, the statement
is identical online and under ``--sql`` (no inspection, nothing to stub), and ``IF NOT
EXISTS`` keeps it idempotent on a database ``create_all`` built from the updated model.
``ALTER TYPE ... ADD VALUE`` may run inside a transaction block, and so inside ``DO``, on
PostgreSQL 12+; production (Supabase) and every CI / compose Postgres is 15.

Transaction handling
--------------------
The ``DO`` block runs inside ``op.get_context().autocommit_block()`` (precedent 047 / 052 /
066): the label is committed on its own, so it is usable the moment the app that writes it
starts, and the PostgreSQL rule that a value added inside a transaction cannot be used until
that transaction commits never applies. It runs FIRST, before the column DDL: the autocommit
block commits whatever the run has done so far and re-opens a transaction, so the column's
``SET LOCAL lock_timeout`` is taken in the transaction that does the ``ADD COLUMN``.

Dialect handling
----------------
- PostgreSQL: the ``DO`` block, then the guarded ADD COLUMN under a LOCAL ``lock_timeout``
  (precedent 114-117) that is handed back right after the DDL.
- SQLite (pytest / local ``create_all``): no native enum and no CHECK on ``entry_type``
  (``SQLEnum`` does not create one by default), so only the column half applies.

Shape / compliance
------------------
- ``job_costs`` is an EXISTING tenant table (TenantMixin ``company_id`` NOT NULL + index;
  RLS enabled since ``059``). No table is created, so the ENABLE-RLS convention does not
  apply and no RLS / ``REVOKE`` / role-naming statement is emitted (precedent 117).
- Pure DDL: no row is read or written, nothing touches ``audit_log``.
- ``actual_subcontract_cost`` is on no request schema: it is computed from entries.

Idempotent and reversible
-------------------------
- Upgrade guards the ADD COLUMN with ``_has_column`` (088 style); the bootstrapped path and
  any re-run are clean no-ops. ``--sql`` renders the canonical path.
- Downgrade drops the column, guarded (SQLite batch mode, precedent 117). It leaves the enum
  label in place: PostgreSQL cannot drop an enum value without recreating the type and
  rewriting the column (the 002 / 014 / 017 / 026 / 047 / 052 / 066 stance), and an unused
  label is harmless.

Before rolling the code back past this revision
-----------------------------------------------
The column is a denormalized total; ``recalculate_totals`` rebuilds it from the entries, so
dropping it loses nothing the entries do not hold. The ENTRIES are the hazard: pre-121 code
cannot load a ``cost_entries`` row whose ``entry_type`` is ``SUBCONTRACT`` (SQLAlchemy has
no member for that name), so every read of that job cost would fail. Before redeploying a
pre-121 image -- with or without this downgrade -- list them::

    SELECT id, company_id, job_cost_id, total_cost, description
    FROM cost_entries
    WHERE entry_type = 'SUBCONTRACT';

and re-key them to ``OTHER`` through the API (delete + re-add, both audited) -- never by an
out-of-band UPDATE. Under pre-121 code they then fold into overhead, which is how that code
would have recorded them.

Deploy ordering: run before (or with) the release that writes SUBCONTRACT. The migration
runs at API boot (``backend/start.sh``). Old code on the new schema is safe (it never
selects the column and writes no SUBCONTRACT entry). New code on the un-migrated schema is
not: every ``JobCost`` load selects the missing column -- deploy the API before or with the
ARQ worker, never the worker first.

Revision id ``121_job_cost_subcontract`` is 24 chars (<= 32) per the create_all -> stamp ->
upgrade bootstrap constraint (``alembic_version.version_num`` is varchar(32)).
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "121_job_cost_subcontract"
down_revision = "120_production_calendar"
branch_labels = None
depends_on = None

TABLE_NAME = "job_costs"
COLUMN_NAME = "actual_subcontract_cost"

# Native Postgres enum behind cost_entries.entry_type on a create_all-built database. The
# label is the member NAME (no values_callable) -- mirrors CostEntryType.SUBCONTRACT.
ENUM_TYPE_NAME = "costentrytype"
NEW_ENUM_LABEL = "SUBCONTRACT"

# One statement, guarded IN the database: a no-op where 024 built entry_type as VARCHAR and
# no costentrytype type exists; IF NOT EXISTS where create_all already has the label.
ADD_ENUM_LABEL_SQL = (
    "DO $$ BEGIN "
    f"IF to_regtype('{ENUM_TYPE_NAME}') IS NOT NULL THEN "
    f"ALTER TYPE {ENUM_TYPE_NAME} ADD VALUE IF NOT EXISTS '{NEW_ENUM_LABEL}'; "
    "END IF; "
    "END $$"
)

# PostgreSQL only, and LOCAL: bounds the wait for the ACCESS EXCLUSIVE lock the ADD / DROP
# COLUMN takes on job_costs, then handed back so it never leaks into a later revision
# (precedent 114-117).
LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = '5s'"
RESET_LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = DEFAULT"


def _offline() -> bool:
    # ``--sql`` has no database to inspect: render the canonical statements.
    return bool(op.get_context().as_sql)


def _is_postgres() -> bool:
    return op.get_context().dialect.name == "postgresql"


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    return _inspector().has_table(table_name)


def _has_column(table_name: str, column_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(col["name"] == column_name for col in _inspector().get_columns(table_name))


def upgrade() -> None:
    postgres = _is_postgres()

    # 1. The enum label, committed on its own (see the module docstring).
    if postgres:
        with op.get_context().autocommit_block():
            op.execute(ADD_ENUM_LABEL_SQL)

    # 2. The column. Guarded so the create_all bootstrap path and any re-run are no-ops.
    if not _offline() and _has_column(TABLE_NAME, COLUMN_NAME):
        return
    if postgres:
        op.execute(LOCK_TIMEOUT_SQL)
    op.add_column(
        TABLE_NAME,
        sa.Column(COLUMN_NAME, sa.Float(), nullable=False, server_default="0"),
    )
    if postgres:
        op.execute(RESET_LOCK_TIMEOUT_SQL)


def downgrade() -> None:
    # The enum label stays (PostgreSQL cannot drop one). Read "Before rolling the code back
    # past this revision" in the module docstring first.
    if not _offline() and not _has_column(TABLE_NAME, COLUMN_NAME):
        return

    postgres = _is_postgres()
    if postgres:
        op.execute(LOCK_TIMEOUT_SQL)
    if op.get_context().dialect.name == "sqlite":
        # Batch mode recreates the table without the column (precedent 117).
        with op.batch_alter_table(TABLE_NAME) as batch_op:
            batch_op.drop_column(COLUMN_NAME)
    else:
        op.drop_column(TABLE_NAME, COLUMN_NAME)
    if postgres:
        op.execute(RESET_LOCK_TIMEOUT_SQL)
