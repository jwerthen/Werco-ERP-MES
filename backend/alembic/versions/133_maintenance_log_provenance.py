"""Maintenance log provenance: which entries a standing API token filed, and an index for a work
order's log

Revision ID: 133_maintenance_log_provenance
Revises: 132_maintenance_pm_integrity
Create Date: 2026-11-20

Context
-------
Owner decision 2026-11-20: a standing API token may file maintenance log entries (``POST
/maintenance/log``). The entry's ``performed_by`` is the token's bound user, so on the record -- and
in the Maintenance page's log timeline -- a token's entry read exactly like the person's own; only the
audit row's ``extra_data.credential`` told them apart. A PM record must say who, or what, wrote it.

This revision, every object guarded independently (the 088/124/132 style):

``maintenance_logs``
  - ``api_token_id`` Integer NULL: the ``api_tokens`` row a standing API token filed the entry with,
    copied from the credential by ``POST /maintenance/log``. NULL for an entry a person (or the work
    order's own Start / Complete) wrote, and for every row before this revision -- never backfilled:
    the audit row's ``extra_data.credential`` stays the record for those. No foreign key: like the
    audit marker it is an identifier, and API tokens are revoked, never deleted.
  - index ``ix_maintenance_logs_maintenance_wo_id`` on ``maintenance_wo_id``: ``GET
    /maintenance/work-orders/{id}/logs`` reads one work order's entries.

No RLS / REVOKE: the table exists and ``059`` enabled RLS on every ``public`` table.

Downgrade
---------
Real: drops the index, then the column. LOSSY for the provenance of token-filed entries (the audit
rows keep it).
"""

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision = "133_maintenance_log_provenance"
down_revision = "132_maintenance_pm_integrity"
branch_labels = None
depends_on = None

TABLE = "maintenance_logs"
TOKEN_COLUMN = "api_token_id"
WO_INDEX = "ix_maintenance_logs_maintenance_wo_id"

# PostgreSQL only, and LOCAL (precedent 059/060/114/117/123/124/132).
LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = '5s'"
RESET_LOCK_TIMEOUT_SQL = "SET LOCAL lock_timeout = DEFAULT"


def _offline() -> bool:
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


def upgrade() -> None:
    if not _has_table(TABLE):
        return
    offline = _offline()
    add_column = offline or not _has_column(TABLE, TOKEN_COLUMN)
    add_index = offline or not _has_index(TABLE, WO_INDEX)
    if not (add_column or add_index):
        return
    postgres = _dialect() == "postgresql"
    if postgres:
        op.execute(LOCK_TIMEOUT_SQL)
    if add_column:
        # Nullable, no default: a catalog-only change on PostgreSQL, a plain ADD COLUMN on SQLite.
        op.add_column(TABLE, sa.Column(TOKEN_COLUMN, sa.Integer(), nullable=True))
    if add_index:
        op.create_index(WO_INDEX, TABLE, ["maintenance_wo_id"], unique=False)
    if postgres:
        op.execute(RESET_LOCK_TIMEOUT_SQL)


def downgrade() -> None:
    if not _has_table(TABLE):
        return
    offline = _offline()
    drop_index = offline or _has_index(TABLE, WO_INDEX)
    drop_column = offline or _has_column(TABLE, TOKEN_COLUMN)
    if not (drop_index or drop_column):
        return
    postgres = _dialect() == "postgresql"
    if postgres:
        op.execute(LOCK_TIMEOUT_SQL)
    if drop_index:
        op.drop_index(WO_INDEX, table_name=TABLE)
    if drop_column:
        with op.batch_alter_table(TABLE) as batch:
            batch.drop_column(TOKEN_COLUMN)
    if postgres:
        op.execute(RESET_LOCK_TIMEOUT_SQL)
