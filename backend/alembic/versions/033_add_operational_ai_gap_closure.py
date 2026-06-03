"""Add operational events and work order blockers

Revision ID: 033_add_operational_ai_gap_closure
Revises: 032_add_ai_learning_fabric
Create Date: 2026-06-03
"""

from alembic import op
import sqlalchemy as sa


revision = "033_add_operational_ai_gap_closure"
down_revision = "032_add_ai_learning_fabric"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "operational_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=80), nullable=False),
        sa.Column("source_module", sa.String(length=80), nullable=False),
        sa.Column("entity_type", sa.String(length=80), nullable=True),
        sa.Column("entity_id", sa.Integer(), nullable=True),
        sa.Column("work_order_id", sa.Integer(), nullable=True),
        sa.Column("operation_id", sa.Integer(), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("severity", sa.String(length=20), nullable=False, server_default="info"),
        sa.Column("event_payload", sa.JSON(), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.ForeignKeyConstraint(["work_order_id"], ["work_orders.id"]),
        sa.ForeignKeyConstraint(["operation_id"], ["work_order_operations.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
    )
    op.create_index("ix_operational_events_id", "operational_events", ["id"])
    op.create_index("ix_operational_events_company_id", "operational_events", ["company_id"])
    op.create_index("ix_operational_events_event_type", "operational_events", ["event_type"])
    op.create_index("ix_operational_events_source_module", "operational_events", ["source_module"])
    op.create_index("ix_operational_events_entity_type", "operational_events", ["entity_type"])
    op.create_index("ix_operational_events_entity_id", "operational_events", ["entity_id"])
    op.create_index("ix_operational_events_work_order_id", "operational_events", ["work_order_id"])
    op.create_index("ix_operational_events_operation_id", "operational_events", ["operation_id"])
    op.create_index("ix_operational_events_user_id", "operational_events", ["user_id"])
    op.create_index("ix_operational_events_severity", "operational_events", ["severity"])
    op.create_index("ix_operational_events_occurred_at", "operational_events", ["occurred_at"])
    op.create_index(
        "ix_operational_events_company_module_time",
        "operational_events",
        ["company_id", "source_module", "occurred_at"],
    )
    op.create_index(
        "ix_operational_events_company_type_time",
        "operational_events",
        ["company_id", "event_type", "occurred_at"],
    )
    op.create_index(
        "ix_operational_events_company_entity",
        "operational_events",
        ["company_id", "entity_type", "entity_id"],
    )
    op.create_index(
        "ix_operational_events_company_work_order",
        "operational_events",
        ["company_id", "work_order_id", "occurred_at"],
    )

    op.create_table(
        "work_order_blockers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("work_order_id", sa.Integer(), nullable=False),
        sa.Column("operation_id", sa.Integer(), nullable=True),
        sa.Column("material_part_id", sa.Integer(), nullable=True),
        sa.Column("category", sa.String(length=40), nullable=False, server_default="other"),
        sa.Column("severity", sa.String(length=20), nullable=False, server_default="medium"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="open"),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
        sa.Column("reported_by", sa.Integer(), nullable=True),
        sa.Column("assigned_to", sa.Integer(), nullable=True),
        sa.Column("resolved_by", sa.Integer(), nullable=True),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.ForeignKeyConstraint(["work_order_id"], ["work_orders.id"]),
        sa.ForeignKeyConstraint(["operation_id"], ["work_order_operations.id"]),
        sa.ForeignKeyConstraint(["material_part_id"], ["parts.id"]),
        sa.ForeignKeyConstraint(["reported_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["assigned_to"], ["users.id"]),
        sa.ForeignKeyConstraint(["resolved_by"], ["users.id"]),
    )
    op.create_index("ix_work_order_blockers_id", "work_order_blockers", ["id"])
    op.create_index("ix_work_order_blockers_company_id", "work_order_blockers", ["company_id"])
    op.create_index("ix_work_order_blockers_work_order_id", "work_order_blockers", ["work_order_id"])
    op.create_index("ix_work_order_blockers_operation_id", "work_order_blockers", ["operation_id"])
    op.create_index("ix_work_order_blockers_material_part_id", "work_order_blockers", ["material_part_id"])
    op.create_index("ix_work_order_blockers_category", "work_order_blockers", ["category"])
    op.create_index("ix_work_order_blockers_severity", "work_order_blockers", ["severity"])
    op.create_index("ix_work_order_blockers_status", "work_order_blockers", ["status"])
    op.create_index("ix_work_order_blockers_reported_by", "work_order_blockers", ["reported_by"])
    op.create_index("ix_work_order_blockers_assigned_to", "work_order_blockers", ["assigned_to"])
    op.create_index("ix_work_order_blockers_reported_at", "work_order_blockers", ["reported_at"])
    op.create_index(
        "ix_work_order_blockers_company_status",
        "work_order_blockers",
        ["company_id", "status", "severity"],
    )
    op.create_index(
        "ix_work_order_blockers_company_category",
        "work_order_blockers",
        ["company_id", "category", "status"],
    )
    op.create_index(
        "ix_work_order_blockers_company_work_order",
        "work_order_blockers",
        ["company_id", "work_order_id", "status"],
    )
    op.create_index(
        "ix_work_order_blockers_company_operation",
        "work_order_blockers",
        ["company_id", "operation_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_work_order_blockers_company_operation", table_name="work_order_blockers")
    op.drop_index("ix_work_order_blockers_company_work_order", table_name="work_order_blockers")
    op.drop_index("ix_work_order_blockers_company_category", table_name="work_order_blockers")
    op.drop_index("ix_work_order_blockers_company_status", table_name="work_order_blockers")
    op.drop_index("ix_work_order_blockers_reported_at", table_name="work_order_blockers")
    op.drop_index("ix_work_order_blockers_assigned_to", table_name="work_order_blockers")
    op.drop_index("ix_work_order_blockers_reported_by", table_name="work_order_blockers")
    op.drop_index("ix_work_order_blockers_status", table_name="work_order_blockers")
    op.drop_index("ix_work_order_blockers_severity", table_name="work_order_blockers")
    op.drop_index("ix_work_order_blockers_category", table_name="work_order_blockers")
    op.drop_index("ix_work_order_blockers_material_part_id", table_name="work_order_blockers")
    op.drop_index("ix_work_order_blockers_operation_id", table_name="work_order_blockers")
    op.drop_index("ix_work_order_blockers_work_order_id", table_name="work_order_blockers")
    op.drop_index("ix_work_order_blockers_company_id", table_name="work_order_blockers")
    op.drop_index("ix_work_order_blockers_id", table_name="work_order_blockers")
    op.drop_table("work_order_blockers")

    op.drop_index("ix_operational_events_company_work_order", table_name="operational_events")
    op.drop_index("ix_operational_events_company_entity", table_name="operational_events")
    op.drop_index("ix_operational_events_company_type_time", table_name="operational_events")
    op.drop_index("ix_operational_events_company_module_time", table_name="operational_events")
    op.drop_index("ix_operational_events_occurred_at", table_name="operational_events")
    op.drop_index("ix_operational_events_severity", table_name="operational_events")
    op.drop_index("ix_operational_events_user_id", table_name="operational_events")
    op.drop_index("ix_operational_events_operation_id", table_name="operational_events")
    op.drop_index("ix_operational_events_work_order_id", table_name="operational_events")
    op.drop_index("ix_operational_events_entity_id", table_name="operational_events")
    op.drop_index("ix_operational_events_entity_type", table_name="operational_events")
    op.drop_index("ix_operational_events_source_module", table_name="operational_events")
    op.drop_index("ix_operational_events_event_type", table_name="operational_events")
    op.drop_index("ix_operational_events_company_id", table_name="operational_events")
    op.drop_index("ix_operational_events_id", table_name="operational_events")
    op.drop_table("operational_events")
