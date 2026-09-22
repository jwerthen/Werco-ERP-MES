"""Add explicitly saved, tenant/user-bound Hank preferences.

Revision ID: 109_hank_preferences
Revises: 108_hank_tasks
"""

import sqlalchemy as sa

from alembic import op


revision = '109_hank_preferences'
down_revision = '108_hank_tasks'
branch_labels = None
depends_on = None


def _exists():
    return not op.get_context().as_sql and sa.inspect(op.get_bind()).has_table('hank_preferences')


def _index(name, columns):
    if op.get_context().as_sql or name not in {
        row['name'] for row in sa.inspect(op.get_bind()).get_indexes('hank_preferences')
    }:
        op.create_index(name, 'hank_preferences', columns)


def upgrade():
    if not _exists():
        op.create_table(
            'hank_preferences',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('company_id', sa.Integer(), sa.ForeignKey('companies.id'), nullable=False),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
            sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
            sa.Column('preferences_json', sa.JSON(none_as_null=True), nullable=False, server_default='{}'),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint('company_id', 'user_id', name='uq_hank_preference_company_user'),
            sa.CheckConstraint('version >= 1', name='ck_hank_preference_version'),
        )
    _index('ix_hank_preferences_company_id', ['company_id'])
    _index('ix_hank_preferences_user_id', ['user_id'])
    if op.get_bind().dialect.name == 'postgresql':
        op.execute('ALTER TABLE hank_preferences ENABLE ROW LEVEL SECURITY')
        op.execute('REVOKE ALL ON TABLE hank_preferences FROM PUBLIC')
        op.execute('REVOKE ALL ON SEQUENCE hank_preferences_id_seq FROM PUBLIC')
        op.execute("""DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
            REVOKE ALL ON TABLE hank_preferences FROM anon;
            REVOKE ALL ON SEQUENCE hank_preferences_id_seq FROM anon;
          END IF;
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
            REVOKE ALL ON TABLE hank_preferences FROM authenticated;
            REVOKE ALL ON SEQUENCE hank_preferences_id_seq FROM authenticated;
          END IF;
        END $$""")


def downgrade():
    if op.get_context().as_sql or _exists():
        op.drop_table('hank_preferences')
