"""Estimate workbench tables: Cut/Bend shop data + fab/buyout/machined lines

Revision ID: 061_estimate_workbench
Revises: 060_audit_log_immutability
Create Date: 2026-07-08

Adds tenant-scoped tables for the Excel-replacement estimate workbench
(docs/ESTIMATE_WORKBENCH.md):

  * cut_bend_tables / cut_bend_rows — five editable shop-physics lookup tables
  * quote_assemblies — sub-jobs under QuoteEstimate
  * quote_fab_line_items — flat-pattern details (4 cost buckets)
  * quote_buyout_line_items — purchased hardware
  * quote_machined_line_items — turned/milled parts on an estimate

Idempotent create_all → stamp → upgrade path: every create_table / create_index
is guarded. No data seed here — use scripts/seed_cut_bend_defaults.py.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "061_estimate_workbench"
down_revision: Union[str, None] = "060_audit_log_immutability"
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
    if not _has_table("cut_bend_tables"):
        op.create_table(
            "cut_bend_tables",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("kind", sa.String(length=40), nullable=False),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("company_id", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("company_id", "kind", name="uq_cut_bend_tables_company_kind"),
        )
    if not _has_index("cut_bend_tables", "ix_cut_bend_tables_id"):
        op.create_index("ix_cut_bend_tables_id", "cut_bend_tables", ["id"])
    if not _has_index("cut_bend_tables", "ix_cut_bend_tables_kind"):
        op.create_index("ix_cut_bend_tables_kind", "cut_bend_tables", ["kind"])
    if not _has_index("cut_bend_tables", "ix_cut_bend_tables_company_id"):
        op.create_index("ix_cut_bend_tables_company_id", "cut_bend_tables", ["company_id"])

    if not _has_table("cut_bend_rows"):
        op.create_table(
            "cut_bend_rows",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("table_id", sa.Integer(), nullable=False),
            sa.Column("sort_order", sa.Integer(), nullable=False),
            sa.Column("thickness_in", sa.Float(), nullable=True),
            sa.Column("gauge", sa.Integer(), nullable=True),
            sa.Column("mild_steel", sa.Float(), nullable=True),
            sa.Column("stainless", sa.Float(), nullable=True),
            sa.Column("aluminum", sa.Float(), nullable=True),
            sa.Column("value", sa.Float(), nullable=True),
            sa.Column("fillet_leg_in", sa.Float(), nullable=True),
            sa.Column("arc_in_per_min", sa.Float(), nullable=True),
            sa.Column("min_per_in", sa.Float(), nullable=True),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("company_id", sa.Integer(), nullable=False),
            sa.ForeignKeyConstraint(["table_id"], ["cut_bend_tables.id"]),
            sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    if not _has_index("cut_bend_rows", "ix_cut_bend_rows_id"):
        op.create_index("ix_cut_bend_rows_id", "cut_bend_rows", ["id"])
    if not _has_index("cut_bend_rows", "ix_cut_bend_rows_table_id"):
        op.create_index("ix_cut_bend_rows_table_id", "cut_bend_rows", ["table_id"])
    if not _has_index("cut_bend_rows", "ix_cut_bend_rows_thickness_in"):
        op.create_index("ix_cut_bend_rows_thickness_in", "cut_bend_rows", ["thickness_in"])
    if not _has_index("cut_bend_rows", "ix_cut_bend_rows_company_id"):
        op.create_index("ix_cut_bend_rows_company_id", "cut_bend_rows", ["company_id"])

    if not _has_table("quote_assemblies"):
        op.create_table(
            "quote_assemblies",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("quote_estimate_id", sa.Integer(), nullable=False),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("sort_order", sa.Integer(), nullable=False),
            sa.Column("assembly_labor_hrs", sa.Float(), nullable=False),
            sa.Column("electrical_labor_hrs", sa.Float(), nullable=False),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("company_id", sa.Integer(), nullable=False),
            sa.Column("is_deleted", sa.Boolean(), server_default="false", nullable=False),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("deleted_by", sa.Integer(), nullable=True),
            sa.Column("version", sa.Integer(), server_default="1", nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default="now()", nullable=False),
            sa.ForeignKeyConstraint(["quote_estimate_id"], ["quote_estimates.id"]),
            sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    if not _has_index("quote_assemblies", "ix_quote_assemblies_id"):
        op.create_index("ix_quote_assemblies_id", "quote_assemblies", ["id"])
    if not _has_index("quote_assemblies", "ix_quote_assemblies_quote_estimate_id"):
        op.create_index(
            "ix_quote_assemblies_quote_estimate_id", "quote_assemblies", ["quote_estimate_id"]
        )
    if not _has_index("quote_assemblies", "ix_quote_assemblies_company_id"):
        op.create_index("ix_quote_assemblies_company_id", "quote_assemblies", ["company_id"])
    if not _has_index("quote_assemblies", "ix_quote_assemblies_is_deleted"):
        op.create_index("ix_quote_assemblies_is_deleted", "quote_assemblies", ["is_deleted"])

    if not _has_table("quote_fab_line_items"):
        op.create_table(
            "quote_fab_line_items",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("assembly_id", sa.Integer(), nullable=False),
            sa.Column("sort_order", sa.Integer(), nullable=False),
            sa.Column("part_number", sa.String(length=120), nullable=True),
            sa.Column("detail_name", sa.String(length=255), nullable=False),
            sa.Column("material", sa.String(length=120), nullable=False),
            sa.Column("material_family_override", sa.String(length=20), nullable=True),
            sa.Column("qty", sa.Integer(), nullable=False),
            sa.Column("thickness_in", sa.Float(), nullable=True),
            sa.Column("width_in", sa.Float(), nullable=True),
            sa.Column("length_in", sa.Float(), nullable=True),
            sa.Column("cut_length_in", sa.Float(), nullable=True),
            sa.Column("pierce_count", sa.Integer(), nullable=False),
            sa.Column("bend_count", sa.Integer(), nullable=False),
            sa.Column("weld_length_in", sa.Float(), nullable=True),
            sa.Column("weld_minutes_ea", sa.Float(), nullable=True),
            sa.Column("include_material", sa.Boolean(), nullable=False),
            sa.Column("include_laser", sa.Boolean(), nullable=False),
            sa.Column("include_brake", sa.Boolean(), nullable=False),
            sa.Column("include_weld", sa.Boolean(), nullable=False),
            sa.Column("weight_ea_lb", sa.Float(), nullable=True),
            sa.Column("material_cost", sa.Float(), nullable=False),
            sa.Column("laser_cost", sa.Float(), nullable=False),
            sa.Column("laser_hours", sa.Float(), nullable=False),
            sa.Column("brake_cost", sa.Float(), nullable=False),
            sa.Column("brake_hours", sa.Float(), nullable=False),
            sa.Column("weld_cost", sa.Float(), nullable=False),
            sa.Column("weld_hours", sa.Float(), nullable=False),
            sa.Column("line_total", sa.Float(), nullable=False),
            sa.Column("calc_warnings", sa.JSON(), nullable=True),
            sa.Column("calc_errors", sa.JSON(), nullable=True),
            sa.Column("confidence", sa.String(length=20), nullable=False),
            sa.Column("verification_note", sa.Text(), nullable=True),
            sa.Column("field_confidence", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("company_id", sa.Integer(), nullable=False),
            sa.Column("is_deleted", sa.Boolean(), server_default="false", nullable=False),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("deleted_by", sa.Integer(), nullable=True),
            sa.Column("version", sa.Integer(), server_default="1", nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default="now()", nullable=False),
            sa.ForeignKeyConstraint(["assembly_id"], ["quote_assemblies.id"]),
            sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    for idx, cols in (
        ("ix_quote_fab_line_items_id", ["id"]),
        ("ix_quote_fab_line_items_assembly_id", ["assembly_id"]),
        ("ix_quote_fab_line_items_part_number", ["part_number"]),
        ("ix_quote_fab_line_items_confidence", ["confidence"]),
        ("ix_quote_fab_line_items_company_id", ["company_id"]),
        ("ix_quote_fab_line_items_is_deleted", ["is_deleted"]),
    ):
        if not _has_index("quote_fab_line_items", idx):
            op.create_index(idx, "quote_fab_line_items", cols)

    if not _has_table("quote_buyout_line_items"):
        op.create_table(
            "quote_buyout_line_items",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("assembly_id", sa.Integer(), nullable=False),
            sa.Column("sort_order", sa.Integer(), nullable=False),
            sa.Column("category", sa.String(length=100), nullable=True),
            sa.Column("vendor", sa.String(length=255), nullable=True),
            sa.Column("part_number", sa.String(length=120), nullable=True),
            sa.Column("part_id", sa.Integer(), nullable=True),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("qty", sa.Float(), nullable=False),
            sa.Column("unit_cost", sa.Float(), nullable=False),
            sa.Column("extended_cost", sa.Float(), nullable=False),
            sa.Column("price_source", sa.Text(), nullable=True),
            sa.Column("confidence", sa.String(length=20), nullable=False),
            sa.Column("verification_note", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("company_id", sa.Integer(), nullable=False),
            sa.Column("is_deleted", sa.Boolean(), server_default="false", nullable=False),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("deleted_by", sa.Integer(), nullable=True),
            sa.Column("version", sa.Integer(), server_default="1", nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default="now()", nullable=False),
            sa.ForeignKeyConstraint(["assembly_id"], ["quote_assemblies.id"]),
            sa.ForeignKeyConstraint(["part_id"], ["parts.id"]),
            sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    for idx, cols in (
        ("ix_quote_buyout_line_items_id", ["id"]),
        ("ix_quote_buyout_line_items_assembly_id", ["assembly_id"]),
        ("ix_quote_buyout_line_items_part_number", ["part_number"]),
        ("ix_quote_buyout_line_items_part_id", ["part_id"]),
        ("ix_quote_buyout_line_items_confidence", ["confidence"]),
        ("ix_quote_buyout_line_items_company_id", ["company_id"]),
        ("ix_quote_buyout_line_items_is_deleted", ["is_deleted"]),
    ):
        if not _has_index("quote_buyout_line_items", idx):
            op.create_index(idx, "quote_buyout_line_items", cols)

    if not _has_table("quote_machined_line_items"):
        op.create_table(
            "quote_machined_line_items",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("quote_estimate_id", sa.Integer(), nullable=False),
            sa.Column("sort_order", sa.Integer(), nullable=False),
            sa.Column("part_number", sa.String(length=120), nullable=True),
            sa.Column("description", sa.String(length=255), nullable=False),
            sa.Column("material", sa.String(length=120), nullable=False),
            sa.Column("qty", sa.Integer(), nullable=False),
            sa.Column("stock_dia_in", sa.Float(), nullable=True),
            sa.Column("stock_length_in", sa.Float(), nullable=True),
            sa.Column("turning_minutes", sa.Float(), nullable=False),
            sa.Column("milling_minutes", sa.Float(), nullable=False),
            sa.Column("weight_ea_lb", sa.Float(), nullable=True),
            sa.Column("material_cost", sa.Float(), nullable=False),
            sa.Column("turning_cost", sa.Float(), nullable=False),
            sa.Column("turning_hours", sa.Float(), nullable=False),
            sa.Column("milling_cost", sa.Float(), nullable=False),
            sa.Column("milling_hours", sa.Float(), nullable=False),
            sa.Column("line_total", sa.Float(), nullable=False),
            sa.Column("confidence", sa.String(length=20), nullable=False),
            sa.Column("verification_note", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("company_id", sa.Integer(), nullable=False),
            sa.Column("is_deleted", sa.Boolean(), server_default="false", nullable=False),
            sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("deleted_by", sa.Integer(), nullable=True),
            sa.Column("version", sa.Integer(), server_default="1", nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default="now()", nullable=False),
            sa.ForeignKeyConstraint(["quote_estimate_id"], ["quote_estimates.id"]),
            sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
    for idx, cols in (
        ("ix_quote_machined_line_items_id", ["id"]),
        ("ix_quote_machined_line_items_quote_estimate_id", ["quote_estimate_id"]),
        ("ix_quote_machined_line_items_part_number", ["part_number"]),
        ("ix_quote_machined_line_items_confidence", ["confidence"]),
        ("ix_quote_machined_line_items_company_id", ["company_id"]),
        ("ix_quote_machined_line_items_is_deleted", ["is_deleted"]),
    ):
        if not _has_index("quote_machined_line_items", idx):
            op.create_index(idx, "quote_machined_line_items", cols)

    # Deny-by-default RLS posture (docs/SUPABASE_SECURITY.md new-table
    # convention): Postgres-only, like 059; app-layer tenancy stays the
    # enforcement. Idempotent catalog flag flip.
    if op.get_bind().dialect.name == "postgresql":
        for table in (
            "cut_bend_tables",
            "cut_bend_rows",
            "quote_assemblies",
            "quote_fab_line_items",
            "quote_buyout_line_items",
            "quote_machined_line_items",
        ):
            op.execute(f'ALTER TABLE public."{table}" ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    for table in (
        "quote_machined_line_items",
        "quote_buyout_line_items",
        "quote_fab_line_items",
        "quote_assemblies",
        "cut_bend_rows",
        "cut_bend_tables",
    ):
        if _has_table(table):
            op.drop_table(table)
