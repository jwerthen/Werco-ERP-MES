"""Add routing learning tables

Revision ID: 031_add_routing_learning_tables
Revises: 030_add_cui_governance_foundation
Create Date: 2026-06-02
"""

from alembic import op
import sqlalchemy as sa


revision = "031_add_routing_learning_tables"
down_revision = "030_add_cui_governance_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "routing_generation_sessions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("part_id", sa.Integer(), nullable=False),
        sa.Column("routing_id", sa.Integer(), nullable=True),
        sa.Column("file_name", sa.String(length=255), nullable=True),
        sa.Column("file_type", sa.String(length=20), nullable=True),
        sa.Column("file_size", sa.Integer(), nullable=True),
        sa.Column("file_path", sa.String(length=500), nullable=True),
        sa.Column("drawing_text", sa.Text(), nullable=True),
        sa.Column("geometry", sa.JSON(), nullable=True),
        sa.Column("drawing_info", sa.JSON(), nullable=True),
        sa.Column("proposed_operations", sa.JSON(), nullable=True),
        sa.Column("approved_operations", sa.JSON(), nullable=True),
        sa.Column("correction_summary", sa.JSON(), nullable=True),
        sa.Column("learned_context", sa.JSON(), nullable=True),
        sa.Column("warnings", sa.JSON(), nullable=True),
        sa.Column("extraction_confidence", sa.String(length=20), nullable=True),
        sa.Column("source_was_ocr", sa.Boolean(), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("approved_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.ForeignKeyConstraint(["part_id"], ["parts.id"]),
        sa.ForeignKeyConstraint(["routing_id"], ["routings.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["approved_by"], ["users.id"]),
    )
    op.create_index("ix_routing_generation_sessions_id", "routing_generation_sessions", ["id"])
    op.create_index("ix_routing_generation_sessions_company_id", "routing_generation_sessions", ["company_id"])
    op.create_index("ix_routing_generation_sessions_part_id", "routing_generation_sessions", ["part_id"])
    op.create_index("ix_routing_generation_sessions_routing_id", "routing_generation_sessions", ["routing_id"])
    op.create_index("ix_routing_generation_sessions_file_type", "routing_generation_sessions", ["file_type"])
    op.create_index("ix_routing_generation_sessions_status", "routing_generation_sessions", ["status"])

    op.create_table(
        "routing_learned_aliases",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("alias", sa.String(length=120), nullable=False),
        sa.Column("work_center_type", sa.String(length=80), nullable=False),
        sa.Column("source", sa.String(length=50), nullable=True),
        sa.Column("usage_count", sa.Integer(), nullable=True),
        sa.Column("confidence_score", sa.Float(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.UniqueConstraint("company_id", "alias", "work_center_type", name="uq_routing_alias_company_alias_type"),
    )
    op.create_index("ix_routing_learned_aliases_id", "routing_learned_aliases", ["id"])
    op.create_index("ix_routing_learned_aliases_company_id", "routing_learned_aliases", ["company_id"])
    op.create_index("ix_routing_alias_company_alias", "routing_learned_aliases", ["company_id", "alias"])
    op.create_index("ix_routing_alias_company_type", "routing_learned_aliases", ["company_id", "work_center_type"])

    op.create_table(
        "routing_work_center_preferences",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("feature_key", sa.String(length=255), nullable=False),
        sa.Column("part_type", sa.String(length=50), nullable=True),
        sa.Column("material", sa.String(length=120), nullable=True),
        sa.Column("thickness", sa.String(length=60), nullable=True),
        sa.Column("finish", sa.String(length=120), nullable=True),
        sa.Column("work_center_type", sa.String(length=80), nullable=False),
        sa.Column("work_center_id", sa.Integer(), nullable=False),
        sa.Column("usage_count", sa.Integer(), nullable=True),
        sa.Column("confidence_score", sa.Float(), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.ForeignKeyConstraint(["work_center_id"], ["work_centers.id"]),
        sa.UniqueConstraint(
            "company_id",
            "feature_key",
            "work_center_type",
            "work_center_id",
            name="uq_routing_wc_pref_company_feature_type_wc",
        ),
    )
    op.create_index("ix_routing_work_center_preferences_id", "routing_work_center_preferences", ["id"])
    op.create_index("ix_routing_work_center_preferences_company_id", "routing_work_center_preferences", ["company_id"])
    op.create_index(
        "ix_routing_wc_pref_company_feature", "routing_work_center_preferences", ["company_id", "feature_key"]
    )
    op.create_index(
        "ix_routing_wc_pref_company_type", "routing_work_center_preferences", ["company_id", "work_center_type"]
    )

    op.create_table(
        "routing_operation_patterns",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("pattern_key", sa.String(length=255), nullable=False),
        sa.Column("part_type", sa.String(length=50), nullable=True),
        sa.Column("material", sa.String(length=120), nullable=True),
        sa.Column("thickness", sa.String(length=60), nullable=True),
        sa.Column("finish", sa.String(length=120), nullable=True),
        sa.Column("feature_signature", sa.JSON(), nullable=True),
        sa.Column("operations", sa.JSON(), nullable=False),
        sa.Column("usage_count", sa.Integer(), nullable=True),
        sa.Column("confidence_score", sa.Float(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.UniqueConstraint("company_id", "pattern_key", name="uq_routing_pattern_company_key"),
    )
    op.create_index("ix_routing_operation_patterns_id", "routing_operation_patterns", ["id"])
    op.create_index("ix_routing_operation_patterns_company_id", "routing_operation_patterns", ["company_id"])
    op.create_index("ix_routing_pattern_company_key", "routing_operation_patterns", ["company_id", "pattern_key"])
    op.create_index(
        "ix_routing_pattern_company_part_type", "routing_operation_patterns", ["company_id", "part_type"]
    )


def downgrade() -> None:
    op.drop_index("ix_routing_pattern_company_part_type", table_name="routing_operation_patterns")
    op.drop_index("ix_routing_pattern_company_key", table_name="routing_operation_patterns")
    op.drop_index("ix_routing_operation_patterns_company_id", table_name="routing_operation_patterns")
    op.drop_index("ix_routing_operation_patterns_id", table_name="routing_operation_patterns")
    op.drop_table("routing_operation_patterns")

    op.drop_index("ix_routing_wc_pref_company_type", table_name="routing_work_center_preferences")
    op.drop_index("ix_routing_wc_pref_company_feature", table_name="routing_work_center_preferences")
    op.drop_index("ix_routing_work_center_preferences_company_id", table_name="routing_work_center_preferences")
    op.drop_index("ix_routing_work_center_preferences_id", table_name="routing_work_center_preferences")
    op.drop_table("routing_work_center_preferences")

    op.drop_index("ix_routing_alias_company_type", table_name="routing_learned_aliases")
    op.drop_index("ix_routing_alias_company_alias", table_name="routing_learned_aliases")
    op.drop_index("ix_routing_learned_aliases_company_id", table_name="routing_learned_aliases")
    op.drop_index("ix_routing_learned_aliases_id", table_name="routing_learned_aliases")
    op.drop_table("routing_learned_aliases")

    op.drop_index("ix_routing_generation_sessions_status", table_name="routing_generation_sessions")
    op.drop_index("ix_routing_generation_sessions_file_type", table_name="routing_generation_sessions")
    op.drop_index("ix_routing_generation_sessions_routing_id", table_name="routing_generation_sessions")
    op.drop_index("ix_routing_generation_sessions_part_id", table_name="routing_generation_sessions")
    op.drop_index("ix_routing_generation_sessions_company_id", table_name="routing_generation_sessions")
    op.drop_index("ix_routing_generation_sessions_id", table_name="routing_generation_sessions")
    op.drop_table("routing_generation_sessions")
