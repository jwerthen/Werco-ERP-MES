"""Shared operational inbox triage, with source-specific recurrence.

Revision ID: 092_operational_inbox_state
Revises: 091_user_workspaces
"""

import sqlalchemy as sa

from alembic import op

revision = '092_operational_inbox_state'
down_revision = '091_user_workspaces'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'operational_inbox_states',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('company_id', sa.Integer(), sa.ForeignKey('companies.id'), nullable=False),
        sa.Column('source_kind', sa.String(30), nullable=False),
        sa.Column('source_id', sa.Integer(), nullable=False),
        sa.Column('owner_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('next_action', sa.String(500), nullable=False, server_default=''),
        sa.Column('acknowledged_occurrence', sa.String(64), nullable=True),
        sa.Column('snoozed_occurrence', sa.String(64), nullable=True),
        sa.Column('snoozed_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('updated_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('company_id', 'source_kind', 'source_id', name='uq_inbox_company_source'),
    )
    op.create_index('ix_operational_inbox_states_company_id', 'operational_inbox_states', ['company_id'])
    op.create_index('ix_operational_inbox_states_owner_id', 'operational_inbox_states', ['owner_id'])
    op.create_index('ix_operational_inbox_states_updated_by', 'operational_inbox_states', ['updated_by'])
    op.create_index('ix_inbox_company_owner', 'operational_inbox_states', ['company_id', 'owner_id'])
    if op.get_bind().dialect.name == 'postgresql':
        # The ERP authenticates custom JWTs on the server, not Supabase auth.uid().
        # Default-deny Data API access; the existing privileged server DB role owns access.
        op.execute('ALTER TABLE operational_inbox_states ENABLE ROW LEVEL SECURITY')
        op.execute('REVOKE ALL ON TABLE operational_inbox_states FROM PUBLIC')
        op.execute('REVOKE ALL ON SEQUENCE operational_inbox_states_id_seq FROM PUBLIC')
        op.execute("""DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
            REVOKE ALL ON TABLE operational_inbox_states FROM anon;
            REVOKE ALL ON SEQUENCE operational_inbox_states_id_seq FROM anon;
          END IF;
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
            REVOKE ALL ON TABLE operational_inbox_states FROM authenticated;
            REVOKE ALL ON SEQUENCE operational_inbox_states_id_seq FROM authenticated;
          END IF;
        END $$""")


def downgrade():
    op.drop_table('operational_inbox_states')
