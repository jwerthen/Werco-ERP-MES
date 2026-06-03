"""Add AI learning fabric tables

Revision ID: 032_add_ai_learning_fabric
Revises: 031_add_routing_learning_tables
Create Date: 2026-06-03
"""

from alembic import op
import sqlalchemy as sa


revision = "032_add_ai_learning_fabric"
down_revision = "031_add_routing_learning_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_recommendations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("source_module", sa.String(length=80), nullable=False),
        sa.Column("recommendation_type", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="pending"),
        sa.Column("priority", sa.String(length=20), nullable=False, server_default="medium"),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("target_entity_type", sa.String(length=80), nullable=True),
        sa.Column("target_entity_id", sa.Integer(), nullable=True),
        sa.Column("suggested_action", sa.JSON(), nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=True),
        sa.Column("impact", sa.JSON(), nullable=True),
        sa.Column("confidence_score", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("prompt_version", sa.String(length=120), nullable=True),
        sa.Column("model_version", sa.String(length=120), nullable=True),
        sa.Column("status_reason", sa.Text(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("accepted_by", sa.Integer(), nullable=True),
        sa.Column("dismissed_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("acted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["accepted_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["dismissed_by"], ["users.id"]),
    )
    op.create_index("ix_ai_recommendations_id", "ai_recommendations", ["id"])
    op.create_index("ix_ai_recommendations_company_id", "ai_recommendations", ["company_id"])
    op.create_index(
        "ix_ai_recommendations_company_status_priority",
        "ai_recommendations",
        ["company_id", "status", "priority"],
    )
    op.create_index(
        "ix_ai_recommendations_company_module_status",
        "ai_recommendations",
        ["company_id", "source_module", "status"],
    )
    op.create_index(
        "ix_ai_recommendations_company_target",
        "ai_recommendations",
        ["company_id", "target_entity_type", "target_entity_id"],
    )
    op.create_index(
        "ix_ai_recommendations_company_type",
        "ai_recommendations",
        ["company_id", "recommendation_type"],
    )
    op.create_index("ix_ai_recommendations_expires_at", "ai_recommendations", ["expires_at"])

    op.create_table(
        "ai_interaction_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=40), nullable=False),
        sa.Column("source_module", sa.String(length=80), nullable=False),
        sa.Column("ai_feature", sa.String(length=120), nullable=True),
        sa.Column("surface", sa.String(length=120), nullable=True),
        sa.Column("entity_type", sa.String(length=80), nullable=True),
        sa.Column("entity_id", sa.Integer(), nullable=True),
        sa.Column("recommendation_id", sa.Integer(), nullable=True),
        sa.Column("context_summary", sa.Text(), nullable=True),
        sa.Column("event_payload", sa.JSON(), nullable=True),
        sa.Column("confidence_score", sa.Float(), nullable=True),
        sa.Column("prompt_version", sa.String(length=120), nullable=True),
        sa.Column("model_version", sa.String(length=120), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.ForeignKeyConstraint(["recommendation_id"], ["ai_recommendations.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
    )
    op.create_index("ix_ai_interaction_events_id", "ai_interaction_events", ["id"])
    op.create_index("ix_ai_interaction_events_company_id", "ai_interaction_events", ["company_id"])
    op.create_index("ix_ai_interaction_events_event_type", "ai_interaction_events", ["event_type"])
    op.create_index("ix_ai_interaction_events_source_module", "ai_interaction_events", ["source_module"])
    op.create_index("ix_ai_interaction_events_entity_type", "ai_interaction_events", ["entity_type"])
    op.create_index("ix_ai_interaction_events_entity_id", "ai_interaction_events", ["entity_id"])
    op.create_index("ix_ai_interaction_events_recommendation_id", "ai_interaction_events", ["recommendation_id"])
    op.create_index("ix_ai_interaction_events_created_by", "ai_interaction_events", ["created_by"])
    op.create_index("ix_ai_interaction_events_created_at", "ai_interaction_events", ["created_at"])
    op.create_index(
        "ix_ai_events_company_module_created",
        "ai_interaction_events",
        ["company_id", "source_module", "created_at"],
    )
    op.create_index(
        "ix_ai_events_company_entity",
        "ai_interaction_events",
        ["company_id", "entity_type", "entity_id"],
    )
    op.create_index("ix_ai_events_company_type", "ai_interaction_events", ["company_id", "event_type"])

    op.create_table(
        "ai_corrections",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=True),
        sa.Column("recommendation_id", sa.Integer(), nullable=True),
        sa.Column("source_module", sa.String(length=80), nullable=False),
        sa.Column("entity_type", sa.String(length=80), nullable=True),
        sa.Column("entity_id", sa.Integer(), nullable=True),
        sa.Column("field_path", sa.String(length=255), nullable=False),
        sa.Column("proposed_value", sa.JSON(), nullable=True),
        sa.Column("final_value", sa.JSON(), nullable=True),
        sa.Column("correction_reason", sa.Text(), nullable=True),
        sa.Column("confidence_score", sa.Float(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.ForeignKeyConstraint(["event_id"], ["ai_interaction_events.id"]),
        sa.ForeignKeyConstraint(["recommendation_id"], ["ai_recommendations.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
    )
    op.create_index("ix_ai_corrections_id", "ai_corrections", ["id"])
    op.create_index("ix_ai_corrections_company_id", "ai_corrections", ["company_id"])
    op.create_index("ix_ai_corrections_event_id", "ai_corrections", ["event_id"])
    op.create_index("ix_ai_corrections_recommendation_id", "ai_corrections", ["recommendation_id"])
    op.create_index("ix_ai_corrections_source_module", "ai_corrections", ["source_module"])
    op.create_index("ix_ai_corrections_entity_type", "ai_corrections", ["entity_type"])
    op.create_index("ix_ai_corrections_entity_id", "ai_corrections", ["entity_id"])
    op.create_index("ix_ai_corrections_created_by", "ai_corrections", ["created_by"])
    op.create_index("ix_ai_corrections_created_at", "ai_corrections", ["created_at"])
    op.create_index(
        "ix_ai_corrections_company_module_created",
        "ai_corrections",
        ["company_id", "source_module", "created_at"],
    )
    op.create_index("ix_ai_corrections_company_field", "ai_corrections", ["company_id", "field_path"])
    op.create_index(
        "ix_ai_corrections_company_entity",
        "ai_corrections",
        ["company_id", "entity_type", "entity_id"],
    )

    op.create_table(
        "ai_outcomes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), nullable=False),
        sa.Column("recommendation_id", sa.Integer(), nullable=True),
        sa.Column("source_module", sa.String(length=80), nullable=False),
        sa.Column("outcome_type", sa.String(length=80), nullable=False),
        sa.Column("entity_type", sa.String(length=80), nullable=True),
        sa.Column("entity_id", sa.Integer(), nullable=True),
        sa.Column("metric_name", sa.String(length=120), nullable=True),
        sa.Column("metric_value", sa.Float(), nullable=True),
        sa.Column("baseline_value", sa.Float(), nullable=True),
        sa.Column("target_value", sa.Float(), nullable=True),
        sa.Column("outcome_payload", sa.JSON(), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["company_id"], ["companies.id"]),
        sa.ForeignKeyConstraint(["recommendation_id"], ["ai_recommendations.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
    )
    op.create_index("ix_ai_outcomes_id", "ai_outcomes", ["id"])
    op.create_index("ix_ai_outcomes_company_id", "ai_outcomes", ["company_id"])
    op.create_index("ix_ai_outcomes_recommendation_id", "ai_outcomes", ["recommendation_id"])
    op.create_index("ix_ai_outcomes_source_module", "ai_outcomes", ["source_module"])
    op.create_index("ix_ai_outcomes_outcome_type", "ai_outcomes", ["outcome_type"])
    op.create_index("ix_ai_outcomes_entity_type", "ai_outcomes", ["entity_type"])
    op.create_index("ix_ai_outcomes_entity_id", "ai_outcomes", ["entity_id"])
    op.create_index("ix_ai_outcomes_metric_name", "ai_outcomes", ["metric_name"])
    op.create_index("ix_ai_outcomes_observed_at", "ai_outcomes", ["observed_at"])
    op.create_index("ix_ai_outcomes_created_by", "ai_outcomes", ["created_by"])
    op.create_index("ix_ai_outcomes_created_at", "ai_outcomes", ["created_at"])
    op.create_index(
        "ix_ai_outcomes_company_module_observed",
        "ai_outcomes",
        ["company_id", "source_module", "observed_at"],
    )
    op.create_index("ix_ai_outcomes_company_entity", "ai_outcomes", ["company_id", "entity_type", "entity_id"])
    op.create_index("ix_ai_outcomes_company_metric", "ai_outcomes", ["company_id", "metric_name"])


def downgrade() -> None:
    op.drop_index("ix_ai_outcomes_company_metric", table_name="ai_outcomes")
    op.drop_index("ix_ai_outcomes_company_entity", table_name="ai_outcomes")
    op.drop_index("ix_ai_outcomes_company_module_observed", table_name="ai_outcomes")
    op.drop_index("ix_ai_outcomes_created_at", table_name="ai_outcomes")
    op.drop_index("ix_ai_outcomes_created_by", table_name="ai_outcomes")
    op.drop_index("ix_ai_outcomes_observed_at", table_name="ai_outcomes")
    op.drop_index("ix_ai_outcomes_metric_name", table_name="ai_outcomes")
    op.drop_index("ix_ai_outcomes_entity_id", table_name="ai_outcomes")
    op.drop_index("ix_ai_outcomes_entity_type", table_name="ai_outcomes")
    op.drop_index("ix_ai_outcomes_outcome_type", table_name="ai_outcomes")
    op.drop_index("ix_ai_outcomes_source_module", table_name="ai_outcomes")
    op.drop_index("ix_ai_outcomes_recommendation_id", table_name="ai_outcomes")
    op.drop_index("ix_ai_outcomes_company_id", table_name="ai_outcomes")
    op.drop_index("ix_ai_outcomes_id", table_name="ai_outcomes")
    op.drop_table("ai_outcomes")

    op.drop_index("ix_ai_corrections_company_entity", table_name="ai_corrections")
    op.drop_index("ix_ai_corrections_company_field", table_name="ai_corrections")
    op.drop_index("ix_ai_corrections_company_module_created", table_name="ai_corrections")
    op.drop_index("ix_ai_corrections_created_at", table_name="ai_corrections")
    op.drop_index("ix_ai_corrections_created_by", table_name="ai_corrections")
    op.drop_index("ix_ai_corrections_entity_id", table_name="ai_corrections")
    op.drop_index("ix_ai_corrections_entity_type", table_name="ai_corrections")
    op.drop_index("ix_ai_corrections_source_module", table_name="ai_corrections")
    op.drop_index("ix_ai_corrections_recommendation_id", table_name="ai_corrections")
    op.drop_index("ix_ai_corrections_event_id", table_name="ai_corrections")
    op.drop_index("ix_ai_corrections_company_id", table_name="ai_corrections")
    op.drop_index("ix_ai_corrections_id", table_name="ai_corrections")
    op.drop_table("ai_corrections")

    op.drop_index("ix_ai_events_company_type", table_name="ai_interaction_events")
    op.drop_index("ix_ai_events_company_entity", table_name="ai_interaction_events")
    op.drop_index("ix_ai_events_company_module_created", table_name="ai_interaction_events")
    op.drop_index("ix_ai_interaction_events_created_at", table_name="ai_interaction_events")
    op.drop_index("ix_ai_interaction_events_created_by", table_name="ai_interaction_events")
    op.drop_index("ix_ai_interaction_events_recommendation_id", table_name="ai_interaction_events")
    op.drop_index("ix_ai_interaction_events_entity_id", table_name="ai_interaction_events")
    op.drop_index("ix_ai_interaction_events_entity_type", table_name="ai_interaction_events")
    op.drop_index("ix_ai_interaction_events_source_module", table_name="ai_interaction_events")
    op.drop_index("ix_ai_interaction_events_event_type", table_name="ai_interaction_events")
    op.drop_index("ix_ai_interaction_events_company_id", table_name="ai_interaction_events")
    op.drop_index("ix_ai_interaction_events_id", table_name="ai_interaction_events")
    op.drop_table("ai_interaction_events")

    op.drop_index("ix_ai_recommendations_expires_at", table_name="ai_recommendations")
    op.drop_index("ix_ai_recommendations_company_type", table_name="ai_recommendations")
    op.drop_index("ix_ai_recommendations_company_target", table_name="ai_recommendations")
    op.drop_index("ix_ai_recommendations_company_module_status", table_name="ai_recommendations")
    op.drop_index("ix_ai_recommendations_company_status_priority", table_name="ai_recommendations")
    op.drop_index("ix_ai_recommendations_company_id", table_name="ai_recommendations")
    op.drop_index("ix_ai_recommendations_id", table_name="ai_recommendations")
    op.drop_table("ai_recommendations")
