"""Add visitor_logs.entered_by_user_id (staff back-entry attribution)

Revision ID: 064_visitor_log_entered_by
Revises: 063_scrap_reason_codes_oee
Create Date: 2026-07-12

Context
-------
Adds one NULLABLE FK column ``entered_by_user_id -> users.id`` to the existing
``visitor_logs`` table (app/models/visitor_log.py::VisitorLog).

It attributes the ADMIN/MANAGER who back-enters an offline (paper-logged) visit
after a lobby-tablet outage, via ``POST /api/v1/visitor-logs/manual`` — a
staff-authenticated write that records the visit's ACTUAL past times (contrast
the live tablet ``/sign-in``, which stamps ``utcnow()``). Its presence is also
the positive "staff back-entry" flag: a live station capture leaves it NULL and
sets ``signin_station_id``; a live staff sign-in via the tablet endpoint leaves
BOTH NULL — so ``entered_by_user_id IS NOT NULL`` is what cleanly distinguishes a
back-dated entry from any live capture, and such a row never masquerades as live
lobby capture.

NULL for every pre-existing row and for all live captures — never backfilled or
guessed. No data is written; the tamper-evident ``audit_log`` table is untouched
(the back-entry itself is audited by the service layer via ``AuditService``).

Shape / compliance
------------------
Nullable Integer FK to ``users.id`` (mirrors the existing ``host_user_id`` FK on
the same table; not indexed — like ``host_user_id`` / ``signin_station_id``, it
is read off an already-fetched row, never a hot filter/join). ``visitor_logs``
already carries the TenantMixin ``company_id`` scope and (from 059/060) RLS is
already enabled on the table, so this additive column needs no RLS or tenancy
work of its own.

Idempotent and reversible
-------------------------
- Upgrade guards the ADD COLUMN with ``_has_column`` and the named FK with
  ``_has_fk_on_column`` (checked by constrained column so the create_all ->
  stamp -> upgrade bootstrap path, where the model's inline FK is auto-named
  ``visitor_logs_entered_by_user_id_fkey``, no-ops rather than duplicating it).
- The named FK is Postgres-only: SQLite cannot ADD CONSTRAINT after the fact and
  its create_all path already wires the model's inline FK (precedent 046/051/063).
- Downgrade drops any FK on the column (by reflected name — covers both the named
  and the auto-named variants) then the column on Postgres; on SQLite it uses
  batch mode to recreate the table without the FK-bearing column (precedent 063).

Locking / operations note
-------------------------
ADD COLUMN (nullable, no default) is metadata-only: a brief ACCESS EXCLUSIVE lock
and no table rewrite. ``visitor_logs`` is a low-write table. ADD CONSTRAINT ...
FOREIGN KEY takes SHARE ROW EXCLUSIVE and scans the referencing table to
validate, but the column is brand-new and all-NULL so validation is trivial.
Deploy ordering: run before app code that writes the column; old code ignores it.

Revision id ``064_visitor_log_entered_by`` is 26 chars (<= 32) per the
create_all -> stamp -> upgrade bootstrap constraint (alembic_version.version_num
is varchar(32) on a freshly bootstrapped DB); see docs/DEVELOPMENT.md.
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "064_visitor_log_entered_by"
down_revision = "063_scrap_reason_codes_oee"
branch_labels = None
depends_on = None

LOGS_TABLE = "visitor_logs"
COLUMN = "entered_by_user_id"
FK_NAME = "fk_visitor_logs_entered_by_user_id"


def _is_postgres(conn) -> bool:
    return conn.dialect.name == "postgresql"


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    return _inspector().has_table(table_name)


def _has_column(table_name: str, column_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(col["name"] == column_name for col in _inspector().get_columns(table_name))


def _has_fk_on_column(table_name: str, column_name: str) -> bool:
    """True if ANY foreign key constrains exactly this column.

    Checked by constrained column rather than name so the create_all-bootstrapped
    path (FK auto-named ``<table>_<col>_fkey`` by the model) idempotently no-ops.
    """
    if not _has_table(table_name):
        return False
    return any(fk.get("constrained_columns") == [column_name] for fk in _inspector().get_foreign_keys(table_name))


def _fk_names_on_column(table_name: str, column_name: str) -> list:
    if not _has_table(table_name):
        return []
    return [
        fk["name"]
        for fk in _inspector().get_foreign_keys(table_name)
        if fk.get("constrained_columns") == [column_name] and fk.get("name")
    ]


def upgrade() -> None:
    conn = op.get_bind()

    if not _has_column(LOGS_TABLE, COLUMN):
        op.add_column(LOGS_TABLE, sa.Column(COLUMN, sa.Integer(), nullable=True))

    # Named FK, Postgres-only: SQLite cannot ADD CONSTRAINT after the fact and its
    # create_all path already wires the model's inline FK (precedent 046/051/063).
    if _is_postgres(conn) and not _has_fk_on_column(LOGS_TABLE, COLUMN):
        op.create_foreign_key(FK_NAME, LOGS_TABLE, "users", [COLUMN], ["id"])


def downgrade() -> None:
    conn = op.get_bind()

    if _is_postgres(conn):
        # Drop by reflected name so both the named (migration path) and the
        # auto-named (create_all path) constraint variants are covered.
        for actual_fk_name in _fk_names_on_column(LOGS_TABLE, COLUMN):
            op.drop_constraint(actual_fk_name, LOGS_TABLE, type_="foreignkey")
        if _has_column(LOGS_TABLE, COLUMN):
            op.drop_column(LOGS_TABLE, COLUMN)
    elif _has_column(LOGS_TABLE, COLUMN):
        # SQLite cannot DROP a column named in an inline FK clause (the
        # create_all-built table carries REFERENCES users(id)), so batch mode
        # recreates the table without the column + FK.
        with op.batch_alter_table(LOGS_TABLE) as batch_op:
            batch_op.drop_column(COLUMN)
