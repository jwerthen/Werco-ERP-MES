"""Add laser nest work orders.

Revision ID: 036_add_laser_nest_work_orders
Revises: 035_add_rfq_assembly_line_metadata
Create Date: 2026-06-04
"""

from alembic import op
import sqlalchemy as sa


revision = "036_add_laser_nest_work_orders"
down_revision = "035_add_rfq_assembly_line_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("work_orders", sa.Column("parent_work_order_id", sa.Integer(), nullable=True))
    op.add_column(
        "work_orders",
        sa.Column("work_order_type", sa.String(length=50), nullable=False, server_default="production"),
    )
    op.create_foreign_key(
        "fk_work_orders_parent_work_order_id",
        "work_orders",
        "work_orders",
        ["parent_work_order_id"],
        ["id"],
    )
    op.create_index("ix_work_orders_parent_work_order_id", "work_orders", ["parent_work_order_id"])
    op.create_index("ix_work_orders_work_order_type", "work_orders", ["work_order_type"])

    op.create_table(
        "laser_nest_packages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("parent_work_order_id", sa.Integer(), nullable=False),
        sa.Column("child_work_order_id", sa.Integer(), nullable=True),
        sa.Column("package_name", sa.String(length=255), nullable=False),
        sa.Column("source_path", sa.String(length=1000), nullable=True),
        sa.Column("import_status", sa.String(length=50), nullable=False, server_default="imported"),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["child_work_order_id"], ["work_orders.id"]),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["parent_work_order_id"], ["work_orders.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_laser_nest_packages_company_id", "laser_nest_packages", ["company_id"])
    op.create_index("ix_laser_nest_packages_parent_work_order_id", "laser_nest_packages", ["parent_work_order_id"])
    op.create_index("ix_laser_nest_packages_child_work_order_id", "laser_nest_packages", ["child_work_order_id"])
    op.create_index("ix_laser_nest_packages_import_status", "laser_nest_packages", ["import_status"])

    op.create_table(
        "laser_nests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("package_id", sa.Integer(), nullable=False),
        sa.Column("work_order_operation_id", sa.Integer(), nullable=True),
        sa.Column("nest_name", sa.String(length=255), nullable=False),
        sa.Column("cnc_file_name", sa.String(length=255), nullable=False),
        sa.Column("cnc_file_path", sa.String(length=1000), nullable=True),
        sa.Column("planned_runs", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("completed_runs", sa.Float(), nullable=False, server_default="0"),
        sa.Column("material", sa.String(length=100), nullable=True),
        sa.Column("thickness", sa.String(length=50), nullable=True),
        sa.Column("sheet_size", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.ForeignKeyConstraint(["package_id"], ["laser_nest_packages.id"]),
        sa.ForeignKeyConstraint(["work_order_operation_id"], ["work_order_operations.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("package_id", "nest_name", "cnc_file_name", name="uq_laser_nests_package_file"),
        sa.UniqueConstraint("work_order_operation_id", name="uq_laser_nests_operation"),
    )
    op.create_index("ix_laser_nests_company_id", "laser_nests", ["company_id"])
    op.create_index("ix_laser_nests_package_id", "laser_nests", ["package_id"])
    op.create_index("ix_laser_nests_work_order_operation_id", "laser_nests", ["work_order_operation_id"])

    op.alter_column("work_orders", "work_order_type", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_laser_nests_work_order_operation_id", table_name="laser_nests")
    op.drop_index("ix_laser_nests_package_id", table_name="laser_nests")
    op.drop_index("ix_laser_nests_company_id", table_name="laser_nests")
    op.drop_table("laser_nests")

    op.drop_index("ix_laser_nest_packages_import_status", table_name="laser_nest_packages")
    op.drop_index("ix_laser_nest_packages_child_work_order_id", table_name="laser_nest_packages")
    op.drop_index("ix_laser_nest_packages_parent_work_order_id", table_name="laser_nest_packages")
    op.drop_index("ix_laser_nest_packages_company_id", table_name="laser_nest_packages")
    op.drop_table("laser_nest_packages")

    op.drop_index("ix_work_orders_work_order_type", table_name="work_orders")
    op.drop_index("ix_work_orders_parent_work_order_id", table_name="work_orders")
    op.drop_constraint("fk_work_orders_parent_work_order_id", "work_orders", type_="foreignkey")
    op.drop_column("work_orders", "work_order_type")
    op.drop_column("work_orders", "parent_work_order_id")
