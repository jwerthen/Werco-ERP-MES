"""Add RFQ sheet-metal accuracy settings

Revision ID: 034_add_rfq_accuracy_settings
Revises: 033_add_operational_ai_gap_closure
Create Date: 2026-06-03
"""

from alembic import op
import sqlalchemy as sa


revision = "034_add_rfq_accuracy_settings"
down_revision = "033_add_operational_ai_gap_closure"
branch_labels = None
depends_on = None


SETTINGS = [
    ("rfq_scrap_factor", "0.10", "number", "Base sheet metal scrap factor"),
    ("rfq_laser_pierce_seconds", "0.8", "number", "Seconds per pierce for laser quoting"),
    ("rfq_laser_min_charge", "35", "number", "Minimum laser operation charge"),
    ("rfq_brake_min_charge", "25", "number", "Minimum press brake operation charge"),
    ("rfq_finish_min_charge", "0", "number", "Default finish minimum charge when no finish match exists"),
]


def upgrade() -> None:
    bind = op.get_bind()
    company_ids = [row[0] for row in bind.execute(sa.text("SELECT id FROM companies")).fetchall()]
    for company_id in company_ids:
        for key, value, setting_type, description in SETTINGS:
            exists = bind.execute(
                sa.text(
                    """
                    SELECT 1
                    FROM quote_settings
                    WHERE company_id = :company_id AND setting_key = :setting_key
                    """
                ),
                {"company_id": company_id, "setting_key": key},
            ).first()
            if exists:
                continue
            bind.execute(
                sa.text(
                    """
                    INSERT INTO quote_settings
                        (company_id, setting_key, setting_value, setting_type, description, updated_at)
                    VALUES
                        (:company_id, :setting_key, :setting_value, :setting_type, :description, CURRENT_TIMESTAMP)
                    """
                ),
                {
                    "company_id": company_id,
                    "setting_key": key,
                    "setting_value": value,
                    "setting_type": setting_type,
                    "description": description,
                },
            )


def downgrade() -> None:
    bind = op.get_bind()
    for key, _, _, _ in SETTINGS:
        bind.execute(sa.text("DELETE FROM quote_settings WHERE setting_key = :setting_key"), {"setting_key": key})
