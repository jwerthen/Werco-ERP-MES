"""Change work_center_type to varchar for dynamic types

Revision ID: 021_work_center_type_to_varchar
Revises: 020_add_vendor_id_to_documents
Create Date: 2026-01-30
"""
from alembic import op

# revision identifiers, used by Alembic.
revision = '021_work_center_type_to_varchar'
down_revision = '020_add_vendor_id_to_documents'
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE work_centers "
        "ALTER COLUMN work_center_type TYPE VARCHAR(50) "
        "USING work_center_type::text"
    )
    op.execute("DROP TYPE IF EXISTS workcentertype")


def downgrade():
    op.execute(
        "CREATE TYPE workcentertype AS ENUM ("
        "'fabrication', 'cnc_machining', 'laser', 'press_brake', "
        "'paint', 'powder_coating', 'assembly', 'welding', "
        "'inspection', 'shipping'"
        ")"
    )
    op.execute(
        "ALTER TABLE work_centers "
        "ALTER COLUMN work_center_type TYPE workcentertype "
        "USING work_center_type::text::workcentertype"
    )
