"""Alembic: estimate_job_actuals for Shop Data quoted-vs-actual (Phase 5).

Revision ID: 062_estimate_job_actuals
Revises: 061_estimate_workbench
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "062_estimate_job_actuals"
down_revision: Union[str, None] = "061_estimate_workbench"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    return _inspector().has_table(table_name)


def _has_index(table_name: str, index_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(ix["name"] == index_name for ix in _inspector().get_indexes(table_name))


def upgrade() -> None:
    if not _has_table("estimate_job_actuals"):
        op.create_table(
            "estimate_job_actuals",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("quote_estimate_id", sa.Integer(), nullable=True),
            sa.Column("work_order_id", sa.Integer(), nullable=True),
            sa.Column("job_label", sa.String(length=255), nullable=True),
            sa.Column("quoted_laser_hours", sa.Float(), nullable=False, server_default="0"),
            sa.Column("quoted_brake_hours", sa.Float(), nullable=False, server_default="0"),
            sa.Column("quoted_weld_hours", sa.Float(), nullable=False, server_default="0"),
            sa.Column("actual_laser_hours", sa.Float(), nullable=True),
            sa.Column("actual_brake_hours", sa.Float(), nullable=True),
            sa.Column("actual_weld_hours", sa.Float(), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("entered_by", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("company_id", sa.Integer(), nullable=False),
            sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("deleted_by", sa.Integer(), nullable=True),
            sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
            sa.ForeignKeyConstraint(["quote_estimate_id"], ["quote_estimates.id"]),
            sa.ForeignKeyConstraint(["work_order_id"], ["work_orders.id"]),
            sa.ForeignKeyConstraint(["entered_by"], ["users.id"]),
            sa.ForeignKeyConstraint(["deleted_by"], ["users.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "company_id",
                "quote_estimate_id",
                name="uq_estimate_job_actuals_company_estimate",
            ),
        )
    if not _has_index("estimate_job_actuals", "ix_estimate_job_actuals_id"):
        op.create_index("ix_estimate_job_actuals_id", "estimate_job_actuals", ["id"])
    if not _has_index("estimate_job_actuals", "ix_estimate_job_actuals_company_id"):
        op.create_index(
            "ix_estimate_job_actuals_company_id", "estimate_job_actuals", ["company_id"]
        )
    if not _has_index("estimate_job_actuals", "ix_estimate_job_actuals_quote_estimate_id"):
        op.create_index(
            "ix_estimate_job_actuals_quote_estimate_id",
            "estimate_job_actuals",
            ["quote_estimate_id"],
        )
    if not _has_index("estimate_job_actuals", "ix_estimate_job_actuals_work_order_id"):
        op.create_index(
            "ix_estimate_job_actuals_work_order_id",
            "estimate_job_actuals",
            ["work_order_id"],
        )


def downgrade() -> None:
    if _has_table("estimate_job_actuals"):
        op.drop_table("estimate_job_actuals")
