"""CAR form fields (Werco's controlled CAR form) and the drawing revision on quote lines

Revision ID: 122_car_form_quote_rev
Revises: 118_dashboard_layouts
Create Date: 2026-11-13

Context
-------
Two brief items share this revision because they ship together (P2.1 and P2.4 of the
Claude-plugin build brief):

* **P2.1 -- CAR in the ERP, matching Werco's form.** ``cars``
  (``app/models/quality.py::CorrectiveActionRequest``) carried the CAR number, due date,
  problem, root cause, corrective action and initiator, but nothing for the rest of
  Werco's controlled form ("AS9100 PROCESS - Corrective Action Report"). This revision
  adds, all NULLABLE with no default and no backfill:

  - ``initiation_reason``       String(40) + CHECK ``ck_cars_initiation_reason`` holding it
    to the eight form boxes (a String + CHECK rather than a native PostgreSQL enum, so a
    ninth reason later is a CHECK swap, not an ``ALTER TYPE``; precedent 111);
  - ``initiation_reason_other`` String(255) (required by the service when the reason is
    ``other``);
  - ``rma_number``              String(50), free text;
  - ``submitted_date``          Date ("Date Submitted"; the service defaults it to today in
    Central at create);
  - ``part_id``                 Integer, FK ``fk_cars_part_id`` -> parts, index
    ``ix_cars_part_id``;
  - ``customer_id``             Integer, FK ``fk_cars_customer_id`` -> customers, plus
    ``customer_name`` String(255) as the snapshot taken at selection;
  - ``vendor_id``               Integer, FK ``fk_cars_vendor_id`` -> vendors, plus
    ``vendor_po_number`` String(100);
  - ``cost_labor`` / ``cost_material`` / ``cost_outside_services`` / ``cost_shipping``
    Numeric(12, 2) -- blank is not zero, so each stays NULL until entered; the total is
    computed at read time and never stored;
  - ``management_action``       Text;
  - ``vendor_approval_name`` / ``vendor_approval_title`` String(255),
    ``vendor_approval_date`` Date;
  - ``customer_approval_name``  String(255), ``customer_approval_date`` Date;
  - ``quality_approved_by``     Integer, FK ``fk_cars_quality_approved_by`` -> users, and
    ``quality_approved_at`` DateTime -- server-stamped by
    ``POST /quality/car/{id}/quality-approval`` only.

* **P2.4 -- quotes always carry the drawing revision.** ``quote_lines.part_revision``
  String(20), NULLABLE (the same width as ``parts.revision`` and ``fais.part_revision``).
  The send gate (``app/services/quote_send_gate.py``) refuses to let a quote reach the
  customer while any line lacks one.

No backfill, anywhere (deliberate, correct-forward)
---------------------------------------------------
Every pre-existing CAR and quote line reads NULL, which is the truthful state: none of
them ever recorded these values. A CAR backfill would invent quality-record content with
no ``AuditService`` row (invariant 2). A quote-line backfill from ``parts.revision`` would
rewrite customer documents that were already issued -- and ``parts.revision`` defaults to
``'A'``, so it would turn "never recorded" into a plausible but unreviewed ``A``. A legacy
DRAFT/PENDING quote gets its revisions entered through ``PUT /quotes/{id}``
``line_revisions`` (audited) before it can be sent. Never tightened to NOT NULL.

Shape / compliance
------------------
- ``cars`` and ``quote_lines`` are EXISTING tenant tables: each already carries the
  TenantMixin non-null ``company_id`` + index, and RLS has been enabled on both since
  ``059``. This revision creates no table, so the ENABLE-RLS new-table convention does not
  apply, and it emits no RLS / ``REVOKE`` / role-naming statement. Existing grants cover new
  columns.
- Pure DDL: no row is read or written. ``audit_log`` is untouched.
- ``cars`` is not soft-deletable (``VOID`` is its tombstone) and ``quote_lines`` is not
  either; nothing here deletes anything.

Lock-step with the models (the stamped-over hazard)
---------------------------------------------------
Every column, FK name, the index and the CHECK text are declared on
``CorrectiveActionRequest`` / ``QuoteLine`` in the same commit, so the ``create_all`` ->
stamp -> upgrade bootstrap builds them from the models and every guard here no-ops.
``tests/test_migration_122_car_form_quote_rev.py`` pins model == migration, and
``tests/test_migration_fresh_bootstrap.py``'s ``downgrade 088`` -> ``upgrade head``
round-trip diffs the result against ``create_all``.

Idempotent and reversible
-------------------------
- Every object is guarded independently (088/089 style): each column by ``_has_column``,
  each FK by ``_has_foreign_key``, the index by ``_has_index``, the CHECK by
  ``_has_check_constraint``. A re-run, or the bootstrap path, is a clean no-op.
- On SQLite the ``cars`` change runs in batch mode (an FK or CHECK makes it recreate the
  table, carrying every row across); on PostgreSQL batch mode emits plain ``ALTER TABLE``.
- Downgrade drops exactly what upgrade added, each guarded, index and constraints first.
- ``--sql`` (offline) has no database to inspect, so it renders the canonical path.

Downgrade is LOSSY -- read before running it
--------------------------------------------
Dropping these columns discards every recorded CAR form field (reason, RMA, part,
customer, vendor, costs, approvals -- including the Quality Manager approval stamp) and
every entered quote-line drawing revision. Pre-122 code never reads them, so a code
rollback does NOT need this downgrade -- it needs a STAMP: from a checkout of the CURRENT
release run ``alembic stamp 118_dashboard_layouts`` (or whatever this revision's
``down_revision`` is after the release re-chain) before redeploying the older image. The
values stay, and the next forward upgrade re-runs this revision as a guarded no-op that
keeps them. The CREATE / UPDATE / APPROVE audit rows still record every value written,
but nothing re-derives the columns from them.

Locking / operations note
-------------------------
Nullable columns with no default are metadata-only ``ADD COLUMN`` on PostgreSQL 11+ (no
rewrite), the FKs validate against an all-NULL column, and the CHECK scans ``cars`` once
(every row NULL, which passes). Each still takes a brief ACCESS EXCLUSIVE lock, so
``SET LOCAL lock_timeout = '5s'`` bounds the wait when there is DDL to run and is handed
back (``SET LOCAL lock_timeout = DEFAULT``) as soon as it is done (precedent 114-117).

Deploy ordering: run BEFORE (or with) the release that maps the columns -- the migration
runs at API boot (``backend/start.sh``). Old code is safe on the new schema (it never
selects them). New code on the un-migrated schema is NOT: every CAR / quote-line load would
select missing columns, so deploy the API before or with the ARQ worker.

Revision id ``122_car_form_quote_rev`` is 22 chars (<= 32) per the create_all -> stamp ->
upgrade bootstrap constraint (``alembic_version.version_num`` is varchar(32)).
"""

from typing import List, Tuple

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "122_car_form_quote_rev"
down_revision = "118_dashboard_layouts"
branch_labels = None
depends_on = None

CARS = "cars"
QUOTE_LINES = "quote_lines"
QUOTE_LINE_REVISION = "part_revision"

PART_INDEX = "ix_cars_part_id"
REASON_CHECK = "ck_cars_initiation_reason"
# Lock-step with app.models.quality.CAR_INITIATION_REASON_CHECK (the test pins the two equal).
REASON_CHECK_SQL = (
    "initiation_reason IN ('management_review', 'nonconformance_trend', 'customer_feedback', "
    "'internal_audit', 'production_problems', 'external_audit', 'continuous_improvement', 'other')"
)

# (constraint name, referred table, local column)
CAR_FOREIGN_KEYS: Tuple[Tuple[str, str, str], ...] = (
    ("fk_cars_part_id", "parts", "part_id"),
    ("fk_cars_customer_id", "customers", "customer_id"),
    ("fk_cars_vendor_id", "vendors", "vendor_id"),
    ("fk_cars_quality_approved_by", "users", "quality_approved_by"),
)

# PostgreSQL only, and LOCAL: bounds the wait for each ACCESS EXCLUSIVE lock (059/060/114-117).
LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = '5s'"
RESET_LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = DEFAULT"


def _car_columns() -> List[sa.Column]:
    """Fresh ``Column`` objects (a Column can belong to one table only), in model order."""
    return [
        sa.Column("initiation_reason", sa.String(length=40), nullable=True),
        sa.Column("initiation_reason_other", sa.String(length=255), nullable=True),
        sa.Column("rma_number", sa.String(length=50), nullable=True),
        sa.Column("submitted_date", sa.Date(), nullable=True),
        sa.Column("part_id", sa.Integer(), nullable=True),
        sa.Column("customer_id", sa.Integer(), nullable=True),
        sa.Column("customer_name", sa.String(length=255), nullable=True),
        sa.Column("vendor_id", sa.Integer(), nullable=True),
        sa.Column("vendor_po_number", sa.String(length=100), nullable=True),
        sa.Column("cost_labor", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("cost_material", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("cost_outside_services", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("cost_shipping", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("management_action", sa.Text(), nullable=True),
        sa.Column("vendor_approval_name", sa.String(length=255), nullable=True),
        sa.Column("vendor_approval_title", sa.String(length=255), nullable=True),
        sa.Column("vendor_approval_date", sa.Date(), nullable=True),
        sa.Column("customer_approval_name", sa.String(length=255), nullable=True),
        sa.Column("customer_approval_date", sa.Date(), nullable=True),
        sa.Column("quality_approved_by", sa.Integer(), nullable=True),
        sa.Column("quality_approved_at", sa.DateTime(), nullable=True),
    ]


CAR_COLUMN_NAMES = tuple(column.name for column in _car_columns())


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


def _has_index(table_name: str, index_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(ix["name"] == index_name for ix in _inspector().get_indexes(table_name))


def _has_foreign_key(table_name: str, constraint_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(fk["name"] == constraint_name for fk in _inspector().get_foreign_keys(table_name))


def _has_check_constraint(table_name: str, constraint_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(ck["name"] == constraint_name for ck in _inspector().get_check_constraints(table_name))


def upgrade() -> None:
    offline = _offline()
    columns = [column for column in _car_columns() if offline or not _has_column(CARS, column.name)]
    foreign_keys = [fk for fk in CAR_FOREIGN_KEYS if offline or not _has_foreign_key(CARS, fk[0])]
    add_check = offline or not _has_check_constraint(CARS, REASON_CHECK)
    add_index = offline or not _has_index(CARS, PART_INDEX)
    add_revision = offline or not _has_column(QUOTE_LINES, QUOTE_LINE_REVISION)
    if not (columns or foreign_keys or add_check or add_index or add_revision):
        return  # the create_all bootstrap (or a re-run) already built every object

    postgres = _is_postgres()
    if postgres:
        op.execute(LOCK_TIMEOUT_SQL)
    if columns or foreign_keys or add_check or add_index:
        with op.batch_alter_table(CARS) as batch:
            for column in columns:
                batch.add_column(column)
            for name, referred_table, local_column in foreign_keys:
                batch.create_foreign_key(name, referred_table, [local_column], ["id"])
            if add_check:
                batch.create_check_constraint(REASON_CHECK, sa.text(REASON_CHECK_SQL))
            if add_index:
                batch.create_index(PART_INDEX, ["part_id"])
    if add_revision:
        with op.batch_alter_table(QUOTE_LINES) as batch:
            batch.add_column(sa.Column(QUOTE_LINE_REVISION, sa.String(length=20), nullable=True))
    if postgres:
        op.execute(RESET_LOCK_TIMEOUT_SQL)


def downgrade() -> None:
    # LOSSY: discards every recorded CAR form field and quote-line revision. See the module
    # docstring -- a code rollback needs a STAMP, not this.
    offline = _offline()
    drop_revision = offline or _has_column(QUOTE_LINES, QUOTE_LINE_REVISION)
    drop_index = offline or _has_index(CARS, PART_INDEX)
    drop_check = offline or _has_check_constraint(CARS, REASON_CHECK)
    foreign_keys = [fk for fk in CAR_FOREIGN_KEYS if offline or _has_foreign_key(CARS, fk[0])]
    columns = [name for name in CAR_COLUMN_NAMES if offline or _has_column(CARS, name)]
    if not (drop_revision or drop_index or drop_check or foreign_keys or columns):
        return

    postgres = _is_postgres()
    if postgres:
        op.execute(LOCK_TIMEOUT_SQL)
    if drop_revision:
        with op.batch_alter_table(QUOTE_LINES) as batch:
            batch.drop_column(QUOTE_LINE_REVISION)
    if drop_index or drop_check or foreign_keys or columns:
        with op.batch_alter_table(CARS) as batch:
            if drop_index:
                batch.drop_index(PART_INDEX)
            if drop_check:
                batch.drop_constraint(REASON_CHECK, type_="check")
            for name, _referred_table, _local_column in foreign_keys:
                batch.drop_constraint(name, type_="foreignkey")
            for name in reversed(columns):
                batch.drop_column(name)
    if postgres:
        op.execute(RESET_LOCK_TIMEOUT_SQL)
