"""Add durable Hank task previews and receipts.

Revision ID: 108_hank_tasks
Revises: 107_fabrication_quote_profiles
"""

import sqlalchemy as sa

from alembic import op

revision = '108_hank_tasks'
down_revision = '107_fabrication_quote_profiles'
branch_labels = None
depends_on = None


def _exists():
    return not op.get_context().as_sql and sa.inspect(op.get_bind()).has_table('hank_tasks')


def _index(name, columns):
    if op.get_context().as_sql or name not in {row['name'] for row in sa.inspect(op.get_bind()).get_indexes('hank_tasks')}:
        op.create_index(name, 'hank_tasks', columns)


def upgrade():
    if not _exists():
        op.create_table(
            'hank_tasks',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('company_id', sa.Integer(), sa.ForeignKey('companies.id'), nullable=False),
            sa.Column('owner_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
            sa.Column('credential_key', sa.String(160), nullable=False),
            sa.Column('request_key', sa.String(36), nullable=False),
            sa.Column('request_hash', sa.String(64), nullable=False),
            sa.Column('kind', sa.String(50), nullable=False),
            sa.Column('title', sa.String(300), nullable=False),
            sa.Column('status', sa.String(30), nullable=False, server_default='awaiting_review'),
            sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
            sa.Column('input_json', sa.JSON(none_as_null=True), nullable=False, server_default='{}'),
            sa.Column('preview_json', sa.JSON(none_as_null=True), nullable=False, server_default='{}'),
            sa.Column('source_versions_json', sa.JSON(none_as_null=True), nullable=False, server_default='{}'),
            sa.Column('result_json', sa.JSON(none_as_null=True), nullable=True),
            sa.Column('error_code', sa.String(64), nullable=True),
            sa.Column('error_message', sa.String(1200), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('last_checked_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('snoozed_until', sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint('company_id', 'request_key', name='uq_hank_task_request'),
            sa.CheckConstraint('version >= 1', name='ck_hank_task_version'),
            sa.CheckConstraint('length(request_key) = 36 AND length(request_hash) = 64', name='ck_hank_task_request_identity'),
            sa.CheckConstraint('length(trim(title)) BETWEEN 1 AND 300', name='ck_hank_task_title'),
            sa.CheckConstraint(
                "status IN ('awaiting_review', 'completed', 'cancelled', 'watching', 'needs_attention', 'snoozed')",
                name='ck_hank_task_status',
            ),
        )
    _index('ix_hank_tasks_company_id', ['company_id'])
    _index('ix_hank_tasks_owner_id', ['owner_id'])
    _index('ix_hank_task_company_owner_created', ['company_id', 'owner_id', 'created_at', 'id'])
    _index('ix_hank_task_status_checked', ['status', 'last_checked_at', 'id'])
    if op.get_bind().dialect.name == 'postgresql':
        op.execute('ALTER TABLE hank_tasks ENABLE ROW LEVEL SECURITY')
        op.execute('REVOKE ALL ON TABLE hank_tasks FROM PUBLIC')
        op.execute('REVOKE ALL ON SEQUENCE hank_tasks_id_seq FROM PUBLIC')
        op.execute("""DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
            REVOKE ALL ON TABLE hank_tasks FROM anon;
            REVOKE ALL ON SEQUENCE hank_tasks_id_seq FROM anon;
          END IF;
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
            REVOKE ALL ON TABLE hank_tasks FROM authenticated;
            REVOKE ALL ON SEQUENCE hank_tasks_id_seq FROM authenticated;
          END IF;
        END $$""")


def downgrade():
    if op.get_context().as_sql or _exists():
        op.drop_table('hank_tasks')
