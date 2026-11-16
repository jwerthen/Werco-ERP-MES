"""Approved Supplier List on the vendor record, plus parts.supply_scope

Revision ID: 123_vendor_asl
Revises: 122_car_form_quote_rev
Create Date: 2026-11-13

Context
-------
Werco keeps its Approved Supplier List (AS9100D 8.4, form QL 7.4) in a spreadsheet. The
ERP's ``vendors`` row knew only ``is_approved`` / ``approval_date``; the ASL's status,
approved scope of supply, approval basis, re-evaluation due date and last performance
evaluation lived in free-text ``notes``. This revision adds them as columns on the EXISTING
``vendors`` table, and adds ``parts.supply_scope`` so a purchase order line can be checked
against the vendor's approved scopes:

``vendors``
  - ``approval_status``              String(40) NULL. NULL = not on the ASL.
  - ``approved_scopes``              JSON NULL (a list of scope slugs).
  - ``approval_bases``               JSON NULL (a list of basis slugs).
  - ``reevaluation_due``             Date NULL.
  - ``last_performance_evaluation``  Date NULL.
  - ``approval_notes``               Text NULL (the ASL's own NOTES column).
  - CHECK ``ck_vendors_approval_status``: NULL or one of approved / conditional /
    disqualified / approved_customer_directed.
  - INDEX ``ix_vendors_company_reevaluation_due`` (company_id, reevaluation_due) -- the
    overdue re-evaluation list.

``parts``
  - ``supply_scope``                 String(60) NULL (an ASL scope slug).

Every object mirrors ``app/models/purchasing.py::Vendor`` / ``app/models/part.py::Part``
(the create_all bootstrap builds them from the models), and every object is guarded
independently so the bootstrap path and any re-run no-op object by object.

No backfill, no default (deliberate)
------------------------------------
Every existing vendor reads NULL ("not on the ASL") until the ASL import or the approval verb
records a status; ``is_approved`` is untouched. The July go-live text in ``notes`` stays as
historical prose. Inventing a status here would fabricate supplier-approval state in a
controlled quality record. Pure DDL: nothing here can touch ``audit_log``.

No new table, so no RLS statement is needed (the columns inherit ``vendors`` / ``parts``
posture).

Downgrade is LOSSY: it drops the ASL columns and every value in them. Export the vendors'
ASL fields first if they must survive.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "123_vendor_asl"
down_revision = "122_car_form_quote_rev"
branch_labels = None
depends_on = None

VENDORS = "vendors"
PARTS = "parts"

# Byte-identical to app.models.purchasing.VENDOR_APPROVAL_STATUS_CHECK (a test pins it).
CHECK_NAME = "ck_vendors_approval_status"
CHECK_CONDITION = (
    "approval_status IS NULL OR approval_status IN "
    "('approved', 'conditional', 'disqualified', 'approved_customer_directed')"
)
INDEX_NAME = "ix_vendors_company_reevaluation_due"
INDEX_COLUMNS = ["company_id", "reevaluation_due"]

# Built fresh per call: a Column object can belong to one Table only.
VENDOR_COLUMNS = (
    ("approval_status", lambda: sa.String(length=40)),
    ("approved_scopes", lambda: sa.JSON(none_as_null=True)),
    ("approval_bases", lambda: sa.JSON(none_as_null=True)),
    ("reevaluation_due", lambda: sa.Date()),
    ("last_performance_evaluation", lambda: sa.Date()),
    ("approval_notes", lambda: sa.Text()),
)
PART_COLUMN = ("supply_scope", lambda: sa.String(length=60))

# PostgreSQL only, and LOCAL (precedent 059/060/114/117): bounds the wait for the ACCESS
# EXCLUSIVE lock on vendors/parts, then hands the normal timeout back.
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
    if _offline():
        return False
    if not _has_table(table_name):
        return False
    return any(col["name"] == column_name for col in _inspector().get_columns(table_name))


def _has_index(table_name: str, index_name: str) -> bool:
    if _offline():
        return False
    if not _has_table(table_name):
        return False
    return any(ix.get("name") == index_name for ix in _inspector().get_indexes(table_name))


def _has_check(table_name: str, constraint_name: str) -> bool:
    """Name membership is sufficient: the name is explicit on the migration path and on the
    model's CheckConstraint (the create_all path), so the two cannot diverge on it."""
    if _offline():
        return False
    if not _has_table(table_name):
        return False
    return any(ck.get("name") == constraint_name for ck in _inspector().get_check_constraints(table_name))


def upgrade() -> None:
    postgres = _dialect() == "postgresql"
    if postgres:
        op.execute(LOCK_TIMEOUT_SQL)

    if _has_table(VENDORS):
        # Nullable, no default -> metadata-only ADD COLUMN on PostgreSQL, no rewrite.
        for name, type_factory in VENDOR_COLUMNS:
            if not _has_column(VENDORS, name):
                op.add_column(VENDORS, sa.Column(name, type_factory(), nullable=True))
        if not _has_check(VENDORS, CHECK_NAME):
            if _dialect() == "sqlite":
                # SQLite cannot ALTER TABLE ... ADD CONSTRAINT: batch mode rebuilds the table
                # with the CHECK (precedent: every SQLite constraint change in this chain).
                with op.batch_alter_table(VENDORS) as batch_op:
                    batch_op.create_check_constraint(CHECK_NAME, sa.text(CHECK_CONDITION))
            else:
                # Every existing row is NULL, so validation passes over live data.
                op.create_check_constraint(CHECK_NAME, VENDORS, CHECK_CONDITION)
        if not _has_index(VENDORS, INDEX_NAME):
            op.create_index(INDEX_NAME, VENDORS, INDEX_COLUMNS, unique=False)

    if _has_table(PARTS):
        name, type_factory = PART_COLUMN
        if not _has_column(PARTS, name):
            op.add_column(PARTS, sa.Column(name, type_factory(), nullable=True))

    if postgres:
        op.execute(RESET_LOCK_TIMEOUT_SQL)


def _drop_columns(table_name: str, names, *, native_on_sqlite: bool = False) -> None:
    present = [name for name in names if _offline() or _has_column(table_name, name)]
    if not present:
        return
    if _dialect() == "sqlite" and not native_on_sqlite:
        # Batch mode recreates the table without the columns (precedent 082/086/114/117)
        # rather than relying on SQLite's version-gated ALTER TABLE ... DROP COLUMN.
        with op.batch_alter_table(table_name) as batch_op:
            for name in present:
                batch_op.drop_column(name)
    else:
        # PostgreSQL -- and SQLite for ``parts``: the stock-observation triggers (model
        # ``after_create`` DDL, app/models/stock_piece.py) reference ``parts``, and a batch
        # rebuild's drop + rename cannot survive a trigger naming the table ("no such table:
        # main.parts"). ``supply_scope`` is a plain column (no index, constraint or trigger
        # names it), which is exactly what SQLite's native DROP COLUMN (3.35+) accepts.
        for name in present:
            op.drop_column(table_name, name)


def _drop_check_if_present() -> None:
    if not (_offline() or _has_check(VENDORS, CHECK_NAME)):
        return
    if _dialect() == "sqlite":
        with op.batch_alter_table(VENDORS) as batch_op:
            batch_op.drop_constraint(CHECK_NAME, type_="check")
    else:
        op.drop_constraint(CHECK_NAME, VENDORS, type_="check")


def downgrade() -> None:
    # LOSSY: discards every ASL value and every part supply scope. See the module docstring.
    postgres = _dialect() == "postgresql"
    if postgres:
        op.execute(LOCK_TIMEOUT_SQL)

    if _has_table(PARTS):
        _drop_columns(PARTS, [PART_COLUMN[0]], native_on_sqlite=True)

    if _has_table(VENDORS):
        if _offline() or _has_index(VENDORS, INDEX_NAME):
            op.drop_index(INDEX_NAME, table_name=VENDORS)
        _drop_check_if_present()
        _drop_columns(VENDORS, [name for name, _ in VENDOR_COLUMNS])

    if postgres:
        op.execute(RESET_LOCK_TIMEOUT_SQL)
