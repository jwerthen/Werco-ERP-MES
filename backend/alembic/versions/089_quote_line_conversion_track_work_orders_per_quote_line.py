"""Track work orders per quote line

Revision ID: 089_quote_line_conversion
Revises: 088_api_tokens
Create Date: 2026-09-06 22:47:30.517880

"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = '089_quote_line_conversion'
down_revision: Union[str, None] = '088_api_tokens'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Every object is guarded: the fresh bootstrap (create_all -> stamp 058 -> upgrade) runs
# this over a schema the models already built. Every op inside a batch is guarded too: on
# SQLite a constraint or FK op makes the batch rebuild the table, and re-adding an existing
# column there is what failed.


def _offline() -> bool:
    # ``--sql`` has no database to inspect: emit every statement, as before.
    return op.get_context().as_sql


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


def _has_unique_constraint(table_name: str, constraint_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(uq["name"] == constraint_name for uq in _inspector().get_unique_constraints(table_name))


def _has_foreign_key(table_name: str, constraint_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(fk["name"] == constraint_name for fk in _inspector().get_foreign_keys(table_name))


def upgrade() -> None:
    offline = _offline()
    # Optional replay protection leaves all historic and keyless quotes intact.
    add_key = offline or not _has_column("quotes", "request_key")
    add_hash = offline or not _has_column("quotes", "request_hash")
    add_unique = offline or not _has_unique_constraint("quotes", "uq_quotes_company_request_key")
    if add_key or add_hash or add_unique:
        with op.batch_alter_table("quotes") as batch:
            if add_key:
                batch.add_column(sa.Column("request_key", sa.String(length=128), nullable=True))
            if add_hash:
                batch.add_column(sa.Column("request_hash", sa.String(length=64), nullable=True))
            if add_unique:
                batch.create_unique_constraint("uq_quotes_company_request_key", ["company_id", "request_key"])
    # Legacy quote-level links are retained; do not guess historic line mappings.
    add_column = offline or not _has_column("quote_lines", "work_order_id")
    add_fk = offline or not _has_foreign_key("quote_lines", "fk_quote_lines_work_order_id")
    add_index = offline or not _has_index("quote_lines", "ix_quote_lines_work_order_id")
    if add_column or add_fk or add_index:
        with op.batch_alter_table("quote_lines") as batch:
            if add_column:
                batch.add_column(sa.Column("work_order_id", sa.Integer(), nullable=True))
            if add_fk:
                batch.create_foreign_key("fk_quote_lines_work_order_id", "work_orders", ["work_order_id"], ["id"])
            if add_index:
                batch.create_index("ix_quote_lines_work_order_id", ["work_order_id"])


def downgrade() -> None:
    offline = _offline()
    drop_unique = offline or _has_unique_constraint("quotes", "uq_quotes_company_request_key")
    drop_hash = offline or _has_column("quotes", "request_hash")
    drop_key = offline or _has_column("quotes", "request_key")
    if drop_unique or drop_hash or drop_key:
        with op.batch_alter_table("quotes") as batch:
            if drop_unique:
                batch.drop_constraint("uq_quotes_company_request_key", type_="unique")
            if drop_hash:
                batch.drop_column("request_hash")
            if drop_key:
                batch.drop_column("request_key")
    drop_index = offline or _has_index("quote_lines", "ix_quote_lines_work_order_id")
    drop_fk = offline or _has_foreign_key("quote_lines", "fk_quote_lines_work_order_id")
    drop_column = offline or _has_column("quote_lines", "work_order_id")
    if drop_index or drop_fk or drop_column:
        with op.batch_alter_table("quote_lines") as batch:
            if drop_index:
                batch.drop_index("ix_quote_lines_work_order_id")
            if drop_fk:
                batch.drop_constraint("fk_quote_lines_work_order_id", type_="foreignkey")
            if drop_column:
                batch.drop_column("work_order_id")
