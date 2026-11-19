"""NCR disposition / concession / re-verification records and CAR effectiveness / 10.2.1 reviews

Revision ID: 127_ncr_car_records
Revises: 126_qms_company_id
Create Date: 2026-11-19

Context
-------
AS9100D 8.7.1 / 8.7.2 require an NCR to record the action taken on a nonconforming output, any
concession obtained and who authorized it, and the re-verification of a corrected output; 10.2.1 /
10.2.2 require a CAR to record the review of human factors and of similar nonconformities, and the
review of the corrective action's effectiveness. ``ncrs`` and ``cars``
(``app/models/quality.py``) carried none of these as records of their own. This revision adds,
all NULLABLE with no default, no server default and no backfill:

``ncrs`` (``NonConformanceReport``):

  - ``action_taken``             Text -- what was physically done with the nonconforming output;
  - ``concession_reference``     String(100) -- the customer's deviation / waiver / concession
    number;
  - ``concession_authorized_by`` String(255) -- who at the customer authorized it (a name, not a
    user: the authorizer is outside the company);
  - ``concession_date``          Date -- when it was authorized (the service refuses a date later
    than today, Central);
  - ``reverification_result``    String(20) + CHECK ``ck_ncrs_reverification_result`` holding it
    to ``conforming`` / ``nonconforming`` (a String + CHECK rather than a native PostgreSQL enum,
    precedent ``122``, so a third value later is a CHECK swap, not an ``ALTER TYPE``);
  - ``reverification_notes``     Text -- what the re-verification found;
  - ``reverified_by``            Integer, FK ``fk_ncrs_reverified_by`` -> users, and
    ``reverified_at`` DateTime (naive UTC, like ``cars.quality_approved_at``) -- server-stamped by
    ``POST /quality/ncr/{id}/reverification`` only;
  - ``dispositioned_by``         Integer, FK ``fk_ncrs_dispositioned_by`` -> users, and
    ``dispositioned_at`` DateTime -- server-stamped whenever the NCR's disposition is set to a
    non-pending value (cleared when it returns to pending), never accepted from a request body.

``cars`` (``CorrectiveActionRequest``):

  - ``effectiveness_result``     String(20) + CHECK ``ck_cars_effectiveness_result`` holding it to
    ``effective`` / ``not_effective`` (same String + CHECK pattern);
  - ``effectiveness_verified_by`` Integer, FK ``fk_cars_effectiveness_verified_by`` -> users, and
    ``effectiveness_verified_at`` DateTime -- server-stamped by
    ``POST /quality/car/{id}/effectiveness`` only;
  - ``effectiveness_due``        Date -- when the follow-up effectiveness check is due;
  - ``human_factors_review``     Text -- the 10.2.1 human factors review;
  - ``similar_nonconformities_review`` Text -- the 10.2.1 c) review of whether similar
    nonconformities exist or could occur.

The existing ``cars.effectiveness_check`` (Text) is reused unchanged as "evidence reviewed for
effectiveness"; this revision does not touch it.

No backfill, anywhere (deliberate, correct-forward)
---------------------------------------------------
Every pre-existing NCR and CAR reads NULL in every new column, which is the truthful state: none
of them ever recorded these values. In particular ``dispositioned_by`` / ``dispositioned_at`` are
NOT derived from an NCR's current ``disposition`` or its audit rows -- that would attribute a
decision to whoever last touched the row, and would write quality-record content with no
``AuditService`` row (invariant 2). ``audit_log`` is untouched. Never tightened to NOT NULL.

Shape / compliance
------------------
- ``ncrs`` and ``cars`` are EXISTING tenant tables: each already carries the TenantMixin non-null
  ``company_id`` + index, and RLS has been enabled on both since ``059``. This revision creates no
  table, so the ENABLE-RLS new-table convention does not apply, and it emits no RLS / ``REVOKE`` /
  role-naming statement. Existing grants cover new columns.
- Pure DDL: no row is read or written.
- ``ncrs`` is soft-deletable (``SoftDeleteMixin``); its ``is_deleted`` / ``deleted_at`` /
  ``deleted_by`` columns are untouched, and on SQLite the batch rebuild carries them (and every
  row) across. ``cars`` is not soft-deletable (``VOID`` is its tombstone). Nothing here deletes
  anything.

Lock-step with the models (the stamped-over hazard)
---------------------------------------------------
Every column, FK name and CHECK text is declared on ``NonConformanceReport`` /
``CorrectiveActionRequest`` in the same commit (``NCR_REVERIFICATION_RESULT_CHECK`` /
``CAR_EFFECTIVENESS_RESULT_CHECK``), so the ``create_all`` -> stamp -> upgrade bootstrap builds them
from the models and every guard here no-ops. ``tests/test_migration_127_ncr_car_records.py`` pins
model == migration (the CHECK text byte-identical), and ``tests/test_migration_fresh_bootstrap.py``'s
``downgrade 088`` -> ``upgrade head`` round-trip diffs the result against ``create_all``.

Idempotent and reversible
-------------------------
- Every object is guarded independently (088/089 style): each column by ``_has_column``, each FK
  by ``_has_foreign_key``, each CHECK by ``_has_check_constraint``. A re-run, the bootstrap path,
  or a partially built schema converges object by object.
- On SQLite each table change runs in batch mode (an FK or CHECK makes it recreate the table,
  carrying every row across); on PostgreSQL batch mode emits plain ``ALTER TABLE``.
- Downgrade drops exactly what upgrade added, in reverse: ``cars`` before ``ncrs``, and within each
  table the CHECK, then the FKs, then the columns. Each drop is guarded.
- ``--sql`` (offline) has no database to inspect, so it renders the canonical path.

Downgrade is LOSSY -- read before running it
--------------------------------------------
Dropping these columns discards every recorded NCR action taken, customer concession,
re-verification and disposition stamp, and every CAR effectiveness verification, human factors
review and similar-nonconformities review. Pre-127 code never reads them, so a code rollback does
NOT need this downgrade -- it needs a STAMP: from a checkout of the CURRENT release run
``alembic stamp 126_qms_company_id`` (this revision's ``down_revision``) before redeploying the
older image. The values stay, and the next forward upgrade re-runs this revision as a guarded no-op
that keeps them. The UPDATE / re-verification / effectiveness audit rows still record every value
written, but nothing re-derives the columns from them.

Locking / operations note
-------------------------
Nullable columns with no default are metadata-only ``ADD COLUMN`` on PostgreSQL 11+ (no rewrite),
the FKs validate against an all-NULL column (and take a brief SHARE ROW EXCLUSIVE lock on
``users``), and each CHECK scans its table once (every row NULL, which passes). Each table still
takes a brief ACCESS EXCLUSIVE lock -- ``ncrs`` first, then ``cars`` -- so
``SET LOCAL lock_timeout = '5s'`` bounds the wait when there is DDL to run and is handed back
(``SET LOCAL lock_timeout = DEFAULT``) as soon as it is done (precedent 114-117, 122).

Deploy ordering: run BEFORE (or with) the release that maps the columns -- the migration runs at
API boot (``backend/start.sh``). Old code is safe on the new schema (it never selects them). New
code on the un-migrated schema is NOT: every NCR / CAR load would select missing columns, so deploy
the API before or with the ARQ worker.

Revision id ``127_ncr_car_records`` is 19 chars (<= 32) per the create_all -> stamp -> upgrade
bootstrap constraint (``alembic_version.version_num`` is varchar(32)).
"""

from typing import Callable, List, NamedTuple, Tuple

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "127_ncr_car_records"
down_revision = "126_qms_company_id"
branch_labels = None
depends_on = None

NCRS = "ncrs"
CARS = "cars"

REVERIFICATION_CHECK = "ck_ncrs_reverification_result"
# Lock-step with app.models.quality.NCR_REVERIFICATION_RESULT_CHECK (the test pins the two equal).
REVERIFICATION_CHECK_SQL = "reverification_result IN ('conforming', 'nonconforming')"

EFFECTIVENESS_CHECK = "ck_cars_effectiveness_result"
# Lock-step with app.models.quality.CAR_EFFECTIVENESS_RESULT_CHECK (the test pins the two equal).
EFFECTIVENESS_CHECK_SQL = "effectiveness_result IN ('effective', 'not_effective')"

# (constraint name, referred table, local column)
NCR_FOREIGN_KEYS: Tuple[Tuple[str, str, str], ...] = (
    ("fk_ncrs_reverified_by", "users", "reverified_by"),
    ("fk_ncrs_dispositioned_by", "users", "dispositioned_by"),
)
CAR_FOREIGN_KEYS: Tuple[Tuple[str, str, str], ...] = (
    ("fk_cars_effectiveness_verified_by", "users", "effectiveness_verified_by"),
)

# PostgreSQL only, and LOCAL: bounds the wait for each ACCESS EXCLUSIVE lock (059/060/114-117/122).
LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = '5s'"
RESET_LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = DEFAULT"


def _ncr_columns() -> List[sa.Column]:
    """Fresh ``Column`` objects (a Column can belong to one table only), in model order."""
    return [
        sa.Column("action_taken", sa.Text(), nullable=True),
        sa.Column("concession_reference", sa.String(length=100), nullable=True),
        sa.Column("concession_authorized_by", sa.String(length=255), nullable=True),
        sa.Column("concession_date", sa.Date(), nullable=True),
        sa.Column("reverification_result", sa.String(length=20), nullable=True),
        sa.Column("reverification_notes", sa.Text(), nullable=True),
        sa.Column("reverified_by", sa.Integer(), nullable=True),
        sa.Column("reverified_at", sa.DateTime(), nullable=True),
        sa.Column("dispositioned_by", sa.Integer(), nullable=True),
        sa.Column("dispositioned_at", sa.DateTime(), nullable=True),
    ]


def _car_columns() -> List[sa.Column]:
    """Fresh ``Column`` objects (a Column can belong to one table only), in model order."""
    return [
        sa.Column("effectiveness_result", sa.String(length=20), nullable=True),
        sa.Column("effectiveness_verified_by", sa.Integer(), nullable=True),
        sa.Column("effectiveness_verified_at", sa.DateTime(), nullable=True),
        sa.Column("effectiveness_due", sa.Date(), nullable=True),
        sa.Column("human_factors_review", sa.Text(), nullable=True),
        sa.Column("similar_nonconformities_review", sa.Text(), nullable=True),
    ]


NCR_COLUMN_NAMES = tuple(column.name for column in _ncr_columns())
CAR_COLUMN_NAMES = tuple(column.name for column in _car_columns())


class _TableChange(NamedTuple):
    """Everything this revision adds to one table."""

    table: str
    columns: Callable[[], List[sa.Column]]
    foreign_keys: Tuple[Tuple[str, str, str], ...]
    check_name: str
    check_sql: str


# Upgrade order; downgrade walks it reversed.
TABLE_CHANGES: Tuple[_TableChange, ...] = (
    _TableChange(NCRS, _ncr_columns, NCR_FOREIGN_KEYS, REVERIFICATION_CHECK, REVERIFICATION_CHECK_SQL),
    _TableChange(CARS, _car_columns, CAR_FOREIGN_KEYS, EFFECTIVENESS_CHECK, EFFECTIVENESS_CHECK_SQL),
)


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


def _has_foreign_key(table_name: str, constraint_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(fk["name"] == constraint_name for fk in _inspector().get_foreign_keys(table_name))


def _has_check_constraint(table_name: str, constraint_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(ck["name"] == constraint_name for ck in _inspector().get_check_constraints(table_name))


class _Pending(NamedTuple):
    change: _TableChange
    columns: List[sa.Column]
    foreign_keys: List[Tuple[str, str, str]]
    check: bool


def _has_work(work: _Pending) -> bool:
    return bool(work.columns or work.foreign_keys or work.check)


def _missing(change: _TableChange, offline: bool) -> _Pending:
    table = change.table
    return _Pending(
        change,
        [column for column in change.columns() if offline or not _has_column(table, column.name)],
        [fk for fk in change.foreign_keys if offline or not _has_foreign_key(table, fk[0])],
        offline or not _has_check_constraint(table, change.check_name),
    )


def _present(change: _TableChange, offline: bool) -> _Pending:
    table = change.table
    return _Pending(
        change,
        [column for column in change.columns() if offline or _has_column(table, column.name)],
        [fk for fk in change.foreign_keys if offline or _has_foreign_key(table, fk[0])],
        offline or _has_check_constraint(table, change.check_name),
    )


def upgrade() -> None:
    offline = _offline()
    pending = [work for work in (_missing(change, offline) for change in TABLE_CHANGES) if _has_work(work)]
    if not pending:
        return  # the create_all bootstrap (or a re-run) already built every object

    postgres = _is_postgres()
    if postgres:
        op.execute(LOCK_TIMEOUT_SQL)
    for work in pending:
        with op.batch_alter_table(work.change.table) as batch:
            for column in work.columns:
                batch.add_column(column)
            for name, referred_table, local_column in work.foreign_keys:
                batch.create_foreign_key(name, referred_table, [local_column], ["id"])
            if work.check:
                batch.create_check_constraint(work.change.check_name, sa.text(work.change.check_sql))
    if postgres:
        op.execute(RESET_LOCK_TIMEOUT_SQL)


def downgrade() -> None:
    # LOSSY: discards every recorded NCR action / concession / re-verification / disposition stamp
    # and every CAR effectiveness verification and 10.2.1 review. See the module docstring -- a
    # code rollback needs a STAMP, not this.
    offline = _offline()
    pending = [work for work in (_present(change, offline) for change in reversed(TABLE_CHANGES)) if _has_work(work)]
    if not pending:
        return

    postgres = _is_postgres()
    if postgres:
        op.execute(LOCK_TIMEOUT_SQL)
    for work in pending:
        with op.batch_alter_table(work.change.table) as batch:
            if work.check:
                batch.drop_constraint(work.change.check_name, type_="check")
            for name, _referred_table, _local_column in reversed(work.foreign_keys):
                batch.drop_constraint(name, type_="foreignkey")
            for column in reversed(work.columns):
                batch.drop_column(column.name)
    if postgres:
        op.execute(RESET_LOCK_TIMEOUT_SQL)
