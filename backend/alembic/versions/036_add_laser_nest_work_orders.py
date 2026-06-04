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


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    return _inspector().has_table(table_name)


def _has_column(table_name: str, column_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(column["name"] == column_name for column in _inspector().get_columns(table_name))


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if not _has_column(table_name, column.name):
        op.add_column(table_name, column)


def _has_index(table_name: str, index_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(index["name"] == index_name for index in _inspector().get_indexes(table_name))


def _create_index_if_missing(index_name: str, table_name: str, columns: list[str]) -> None:
    if not _has_index(table_name, index_name):
        op.create_index(index_name, table_name, columns)


def _has_foreign_key(
    table_name: str,
    constraint_name: str,
    local_cols: list[str],
    referent_table: str,
    remote_cols: list[str],
) -> bool:
    if not _has_table(table_name):
        return False
    for fk in _inspector().get_foreign_keys(table_name):
        if fk["name"] == constraint_name:
            return True
        if (
            fk.get("constrained_columns") == local_cols
            and fk.get("referred_table") == referent_table
            and fk.get("referred_columns") == remote_cols
        ):
            return True
    return False


def _create_foreign_key_if_missing(
    constraint_name: str,
    source_table: str,
    referent_table: str,
    local_cols: list[str],
    remote_cols: list[str],
) -> None:
    if not _has_foreign_key(source_table, constraint_name, local_cols, referent_table, remote_cols):
        op.create_foreign_key(constraint_name, source_table, referent_table, local_cols, remote_cols)


def _has_unique_constraint(table_name: str, constraint_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(unique["name"] == constraint_name for unique in _inspector().get_unique_constraints(table_name))


def _create_unique_constraint_if_missing(constraint_name: str, table_name: str, columns: list[str]) -> None:
    if not _has_unique_constraint(table_name, constraint_name):
        op.create_unique_constraint(constraint_name, table_name, columns)


def upgrade() -> None:
    _add_column_if_missing("work_orders", sa.Column("parent_work_order_id", sa.Integer(), nullable=True))
    _add_column_if_missing(
        "work_orders",
        sa.Column("work_order_type", sa.String(length=50), nullable=False, server_default="production"),
    )
    _create_foreign_key_if_missing(
        "fk_work_orders_parent_work_order_id",
        "work_orders",
        "work_orders",
        ["parent_work_order_id"],
        ["id"],
    )
    _create_index_if_missing("ix_work_orders_parent_work_order_id", "work_orders", ["parent_work_order_id"])
    _create_index_if_missing("ix_work_orders_work_order_type", "work_orders", ["work_order_type"])

    if not _has_table("laser_nest_packages"):
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
    else:
        _add_column_if_missing("laser_nest_packages", sa.Column("company_id", sa.Integer(), nullable=False))
        _add_column_if_missing("laser_nest_packages", sa.Column("parent_work_order_id", sa.Integer(), nullable=False))
        _add_column_if_missing("laser_nest_packages", sa.Column("child_work_order_id", sa.Integer(), nullable=True))
        _add_column_if_missing("laser_nest_packages", sa.Column("package_name", sa.String(length=255), nullable=False))
        _add_column_if_missing("laser_nest_packages", sa.Column("source_path", sa.String(length=1000), nullable=True))
        _add_column_if_missing(
            "laser_nest_packages",
            sa.Column("import_status", sa.String(length=50), nullable=False, server_default="imported"),
        )
        _add_column_if_missing("laser_nest_packages", sa.Column("created_by", sa.Integer(), nullable=True))
        _add_column_if_missing("laser_nest_packages", sa.Column("created_at", sa.DateTime(), nullable=True))
        _add_column_if_missing("laser_nest_packages", sa.Column("updated_at", sa.DateTime(), nullable=True))

    _create_index_if_missing("ix_laser_nest_packages_company_id", "laser_nest_packages", ["company_id"])
    _create_index_if_missing(
        "ix_laser_nest_packages_parent_work_order_id",
        "laser_nest_packages",
        ["parent_work_order_id"],
    )
    _create_index_if_missing(
        "ix_laser_nest_packages_child_work_order_id",
        "laser_nest_packages",
        ["child_work_order_id"],
    )
    _create_index_if_missing("ix_laser_nest_packages_import_status", "laser_nest_packages", ["import_status"])

    if not _has_table("laser_nests"):
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
    else:
        _add_column_if_missing("laser_nests", sa.Column("company_id", sa.Integer(), nullable=False))
        _add_column_if_missing("laser_nests", sa.Column("package_id", sa.Integer(), nullable=False))
        _add_column_if_missing("laser_nests", sa.Column("work_order_operation_id", sa.Integer(), nullable=True))
        _add_column_if_missing("laser_nests", sa.Column("nest_name", sa.String(length=255), nullable=False))
        _add_column_if_missing("laser_nests", sa.Column("cnc_file_name", sa.String(length=255), nullable=False))
        _add_column_if_missing("laser_nests", sa.Column("cnc_file_path", sa.String(length=1000), nullable=True))
        _add_column_if_missing("laser_nests", sa.Column("planned_runs", sa.Integer(), nullable=False, server_default="1"))
        _add_column_if_missing("laser_nests", sa.Column("completed_runs", sa.Float(), nullable=False, server_default="0"))
        _add_column_if_missing("laser_nests", sa.Column("material", sa.String(length=100), nullable=True))
        _add_column_if_missing("laser_nests", sa.Column("thickness", sa.String(length=50), nullable=True))
        _add_column_if_missing("laser_nests", sa.Column("sheet_size", sa.String(length=100), nullable=True))
        _add_column_if_missing("laser_nests", sa.Column("created_at", sa.DateTime(), nullable=True))
        _add_column_if_missing("laser_nests", sa.Column("updated_at", sa.DateTime(), nullable=True))
        _create_unique_constraint_if_missing(
            "uq_laser_nests_package_file",
            "laser_nests",
            ["package_id", "nest_name", "cnc_file_name"],
        )
        _create_unique_constraint_if_missing(
            "uq_laser_nests_operation",
            "laser_nests",
            ["work_order_operation_id"],
        )

    _create_index_if_missing("ix_laser_nests_company_id", "laser_nests", ["company_id"])
    _create_index_if_missing("ix_laser_nests_package_id", "laser_nests", ["package_id"])
    _create_index_if_missing(
        "ix_laser_nests_work_order_operation_id",
        "laser_nests",
        ["work_order_operation_id"],
    )

    if _has_column("work_orders", "work_order_type"):
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
