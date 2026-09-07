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


def upgrade() -> None:
    # Optional replay protection leaves all historic and keyless quotes intact.
    with op.batch_alter_table("quotes") as batch:
        batch.add_column(sa.Column("request_key", sa.String(length=128), nullable=True))
        batch.add_column(sa.Column("request_hash", sa.String(length=64), nullable=True))
        batch.create_unique_constraint("uq_quotes_company_request_key", ["company_id", "request_key"])
    # Legacy quote-level links are retained; do not guess historic line mappings.
    with op.batch_alter_table("quote_lines") as batch:
        batch.add_column(sa.Column("work_order_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_quote_lines_work_order_id", "work_orders", ["work_order_id"], ["id"])
        batch.create_index("ix_quote_lines_work_order_id", ["work_order_id"])


def downgrade() -> None:
    with op.batch_alter_table("quotes") as batch:
        batch.drop_constraint("uq_quotes_company_request_key", type_="unique")
        batch.drop_column("request_hash")
        batch.drop_column("request_key")
    with op.batch_alter_table("quote_lines") as batch:
        batch.drop_index("ix_quote_lines_work_order_id")
        batch.drop_constraint("fk_quote_lines_work_order_id", type_="foreignkey")
        batch.drop_column("work_order_id")
