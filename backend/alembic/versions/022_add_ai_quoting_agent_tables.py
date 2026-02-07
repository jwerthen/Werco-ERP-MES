"""Add AI quoting agent tables for RFQ package estimating

Revision ID: 022_add_ai_quoting_agent_tables
Revises: 021_work_center_type_to_varchar
Create Date: 2026-02-07
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "022_add_ai_quoting_agent_tables"
down_revision = "021_work_center_type_to_varchar"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rfq_packages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("rfq_number", sa.String(length=50), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=True),
        sa.Column("customer_name", sa.String(length=255), nullable=True),
        sa.Column("rfq_reference", sa.String(length=100), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=True),
        sa.Column("package_metadata", sa.JSON(), nullable=True),
        sa.Column("parsing_warnings", sa.JSON(), nullable=True),
        sa.Column("uploaded_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.ForeignKeyConstraint(["uploaded_by"], ["users.id"]),
    )
    op.create_index("ix_rfq_packages_id", "rfq_packages", ["id"])
    op.create_index("ix_rfq_packages_rfq_number", "rfq_packages", ["rfq_number"], unique=True)
    op.create_index("ix_rfq_packages_customer_id", "rfq_packages", ["customer_id"])
    op.create_index("ix_rfq_packages_rfq_reference", "rfq_packages", ["rfq_reference"])
    op.create_index("ix_rfq_packages_status", "rfq_packages", ["status"])
    op.create_index("ix_rfq_packages_created_at", "rfq_packages", ["created_at"])

    op.create_table(
        "rfq_package_files",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("rfq_package_id", sa.Integer(), nullable=False),
        sa.Column("file_name", sa.String(length=255), nullable=False),
        sa.Column("file_path", sa.String(length=500), nullable=False),
        sa.Column("file_ext", sa.String(length=20), nullable=False),
        sa.Column("mime_type", sa.String(length=120), nullable=True),
        sa.Column("file_size", sa.Integer(), nullable=False),
        sa.Column("parse_status", sa.String(length=50), nullable=True),
        sa.Column("parse_error", sa.Text(), nullable=True),
        sa.Column("extracted_summary", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["rfq_package_id"], ["rfq_packages.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_rfq_package_files_id", "rfq_package_files", ["id"])
    op.create_index("ix_rfq_package_files_rfq_package_id", "rfq_package_files", ["rfq_package_id"])
    op.create_index("ix_rfq_package_files_file_ext", "rfq_package_files", ["file_ext"])
    op.create_index("ix_rfq_package_files_parse_status", "rfq_package_files", ["parse_status"])

    op.create_table(
        "quote_estimates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("rfq_package_id", sa.Integer(), nullable=False),
        sa.Column("quote_id", sa.Integer(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=True),
        sa.Column("currency", sa.String(length=10), nullable=True),
        sa.Column("material_total", sa.Float(), nullable=True),
        sa.Column("hardware_consumables_total", sa.Float(), nullable=True),
        sa.Column("outside_services_total", sa.Float(), nullable=True),
        sa.Column("shop_labor_oh_total", sa.Float(), nullable=True),
        sa.Column("margin_total", sa.Float(), nullable=True),
        sa.Column("grand_total", sa.Float(), nullable=True),
        sa.Column("lead_time_min_days", sa.Integer(), nullable=True),
        sa.Column("lead_time_max_days", sa.Integer(), nullable=True),
        sa.Column("lead_time_confidence", sa.Float(), nullable=True),
        sa.Column("confidence_score", sa.Float(), nullable=True),
        sa.Column("confidence_detail", sa.JSON(), nullable=True),
        sa.Column("assumptions", sa.JSON(), nullable=True),
        sa.Column("missing_specs", sa.JSON(), nullable=True),
        sa.Column("source_attribution", sa.JSON(), nullable=True),
        sa.Column("internal_breakdown", sa.JSON(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["rfq_package_id"], ["rfq_packages.id"]),
        sa.ForeignKeyConstraint(["quote_id"], ["quotes.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
    )
    op.create_index("ix_quote_estimates_id", "quote_estimates", ["id"])
    op.create_index("ix_quote_estimates_rfq_package_id", "quote_estimates", ["rfq_package_id"])
    op.create_index("ix_quote_estimates_quote_id", "quote_estimates", ["quote_id"])
    op.create_index("ix_quote_estimates_created_at", "quote_estimates", ["created_at"])

    op.create_table(
        "quote_line_summaries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("quote_estimate_id", sa.Integer(), nullable=False),
        sa.Column("part_number", sa.String(length=120), nullable=True),
        sa.Column("part_name", sa.String(length=255), nullable=False),
        sa.Column("quantity", sa.Float(), nullable=True),
        sa.Column("material", sa.String(length=120), nullable=True),
        sa.Column("thickness", sa.String(length=60), nullable=True),
        sa.Column("flat_area", sa.Float(), nullable=True),
        sa.Column("cut_length", sa.Float(), nullable=True),
        sa.Column("bend_count", sa.Integer(), nullable=True),
        sa.Column("hole_count", sa.Integer(), nullable=True),
        sa.Column("finish", sa.String(length=120), nullable=True),
        sa.Column("weld_required", sa.Boolean(), nullable=True),
        sa.Column("assembly_required", sa.Boolean(), nullable=True),
        sa.Column("part_total", sa.Float(), nullable=True),
        sa.Column("confidence", sa.JSON(), nullable=True),
        sa.Column("sources", sa.JSON(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["quote_estimate_id"], ["quote_estimates.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_quote_line_summaries_id", "quote_line_summaries", ["id"])
    op.create_index("ix_quote_line_summaries_quote_estimate_id", "quote_line_summaries", ["quote_estimate_id"])
    op.create_index("ix_quote_line_summaries_part_number", "quote_line_summaries", ["part_number"])

    op.create_table(
        "price_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("quote_estimate_id", sa.Integer(), nullable=True),
        sa.Column("rfq_package_id", sa.Integer(), nullable=True),
        sa.Column("snapshot_scope", sa.String(length=40), nullable=True),
        sa.Column("price_type", sa.String(length=60), nullable=False),
        sa.Column("item_code", sa.String(length=120), nullable=True),
        sa.Column("material", sa.String(length=120), nullable=True),
        sa.Column("thickness", sa.String(length=60), nullable=True),
        sa.Column("unit", sa.String(length=30), nullable=True),
        sa.Column("unit_price", sa.Float(), nullable=False),
        sa.Column("currency", sa.String(length=10), nullable=True),
        sa.Column("supplier", sa.String(length=255), nullable=False),
        sa.Column("source_name", sa.String(length=255), nullable=False),
        sa.Column("source_url", sa.String(length=500), nullable=True),
        sa.Column("is_fallback", sa.Boolean(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("raw_data", sa.JSON(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["quote_estimate_id"], ["quote_estimates.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["rfq_package_id"], ["rfq_packages.id"]),
    )
    op.create_index("ix_price_snapshots_id", "price_snapshots", ["id"])
    op.create_index("ix_price_snapshots_quote_estimate_id", "price_snapshots", ["quote_estimate_id"])
    op.create_index("ix_price_snapshots_rfq_package_id", "price_snapshots", ["rfq_package_id"])
    op.create_index("ix_price_snapshots_snapshot_scope", "price_snapshots", ["snapshot_scope"])
    op.create_index("ix_price_snapshots_price_type", "price_snapshots", ["price_type"])
    op.create_index("ix_price_snapshots_item_code", "price_snapshots", ["item_code"])
    op.create_index("ix_price_snapshots_material", "price_snapshots", ["material"])
    op.create_index("ix_price_snapshots_thickness", "price_snapshots", ["thickness"])
    op.create_index("ix_price_snapshots_fetched_at", "price_snapshots", ["fetched_at"])
    op.create_index("ix_price_snapshots_expires_at", "price_snapshots", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_price_snapshots_expires_at", table_name="price_snapshots")
    op.drop_index("ix_price_snapshots_fetched_at", table_name="price_snapshots")
    op.drop_index("ix_price_snapshots_thickness", table_name="price_snapshots")
    op.drop_index("ix_price_snapshots_material", table_name="price_snapshots")
    op.drop_index("ix_price_snapshots_item_code", table_name="price_snapshots")
    op.drop_index("ix_price_snapshots_price_type", table_name="price_snapshots")
    op.drop_index("ix_price_snapshots_snapshot_scope", table_name="price_snapshots")
    op.drop_index("ix_price_snapshots_rfq_package_id", table_name="price_snapshots")
    op.drop_index("ix_price_snapshots_quote_estimate_id", table_name="price_snapshots")
    op.drop_index("ix_price_snapshots_id", table_name="price_snapshots")
    op.drop_table("price_snapshots")

    op.drop_index("ix_quote_line_summaries_part_number", table_name="quote_line_summaries")
    op.drop_index("ix_quote_line_summaries_quote_estimate_id", table_name="quote_line_summaries")
    op.drop_index("ix_quote_line_summaries_id", table_name="quote_line_summaries")
    op.drop_table("quote_line_summaries")

    op.drop_index("ix_quote_estimates_created_at", table_name="quote_estimates")
    op.drop_index("ix_quote_estimates_quote_id", table_name="quote_estimates")
    op.drop_index("ix_quote_estimates_rfq_package_id", table_name="quote_estimates")
    op.drop_index("ix_quote_estimates_id", table_name="quote_estimates")
    op.drop_table("quote_estimates")

    op.drop_index("ix_rfq_package_files_parse_status", table_name="rfq_package_files")
    op.drop_index("ix_rfq_package_files_file_ext", table_name="rfq_package_files")
    op.drop_index("ix_rfq_package_files_rfq_package_id", table_name="rfq_package_files")
    op.drop_index("ix_rfq_package_files_id", table_name="rfq_package_files")
    op.drop_table("rfq_package_files")

    op.drop_index("ix_rfq_packages_created_at", table_name="rfq_packages")
    op.drop_index("ix_rfq_packages_status", table_name="rfq_packages")
    op.drop_index("ix_rfq_packages_rfq_reference", table_name="rfq_packages")
    op.drop_index("ix_rfq_packages_customer_id", table_name="rfq_packages")
    op.drop_index("ix_rfq_packages_rfq_number", table_name="rfq_packages")
    op.drop_index("ix_rfq_packages_id", table_name="rfq_packages")
    op.drop_table("rfq_packages")
