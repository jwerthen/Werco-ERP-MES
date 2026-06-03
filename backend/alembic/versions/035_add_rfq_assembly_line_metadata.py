"""Add RFQ assembly line metadata.

Revision ID: 035_add_rfq_assembly_line_metadata
Revises: 034_add_rfq_accuracy_settings
Create Date: 2026-06-03
"""

from alembic import op
import sqlalchemy as sa


revision = "035_add_rfq_assembly_line_metadata"
down_revision = "034_add_rfq_accuracy_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("quote_line_summaries", sa.Column("parent_part_number", sa.String(length=120), nullable=True))
    op.add_column("quote_line_summaries", sa.Column("line_type", sa.String(length=40), nullable=True))
    op.add_column("quote_line_summaries", sa.Column("item_type", sa.String(length=40), nullable=True))
    op.add_column("quote_line_summaries", sa.Column("bom_level", sa.Integer(), nullable=True, server_default="0"))
    op.add_column("quote_line_summaries", sa.Column("item_number", sa.String(length=40), nullable=True))
    op.add_column("quote_line_summaries", sa.Column("quantity_per_assembly", sa.Float(), nullable=True))
    op.add_column("quote_line_summaries", sa.Column("unit_of_measure", sa.String(length=20), nullable=True))
    op.create_index(
        "ix_quote_line_summaries_parent_part_number",
        "quote_line_summaries",
        ["parent_part_number"],
    )
    op.create_index("ix_quote_line_summaries_line_type", "quote_line_summaries", ["line_type"])


def downgrade() -> None:
    op.drop_index("ix_quote_line_summaries_line_type", table_name="quote_line_summaries")
    op.drop_index("ix_quote_line_summaries_parent_part_number", table_name="quote_line_summaries")
    op.drop_column("quote_line_summaries", "unit_of_measure")
    op.drop_column("quote_line_summaries", "quantity_per_assembly")
    op.drop_column("quote_line_summaries", "item_number")
    op.drop_column("quote_line_summaries", "bom_level")
    op.drop_column("quote_line_summaries", "item_type")
    op.drop_column("quote_line_summaries", "line_type")
    op.drop_column("quote_line_summaries", "parent_part_number")
