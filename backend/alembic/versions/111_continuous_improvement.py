"""Add tenant-scoped lean improvement suggestions and immutable activity.

Revision ID: 111_continuous_improvement
Revises: 110_hank_workflows
"""

import sqlalchemy as sa
from alembic import op

revision = '111_continuous_improvement'
down_revision = '110_hank_workflows'
branch_labels = None
depends_on = None


def _exists(table):
    return not op.get_context().as_sql and sa.inspect(op.get_bind()).has_table(table)


def _index(table, name, columns):
    if op.get_context().as_sql or name not in {row['name'] for row in sa.inspect(op.get_bind()).get_indexes(table)}:
        op.create_index(name, table, columns)


def _secure(table):
    if op.get_bind().dialect.name == 'postgresql':
        op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')
        op.execute(f'REVOKE ALL ON TABLE {table} FROM PUBLIC')
        op.execute(f'REVOKE ALL ON SEQUENCE {table}_id_seq FROM PUBLIC')
        op.execute(f"""DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
            REVOKE ALL ON TABLE {table} FROM anon;
            REVOKE ALL ON SEQUENCE {table}_id_seq FROM anon;
          END IF;
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
            REVOKE ALL ON TABLE {table} FROM authenticated;
            REVOKE ALL ON SEQUENCE {table}_id_seq FROM authenticated;
          END IF;
        END $$""")


def upgrade():
    if not _exists('improvement_suggestions'):
        op.create_table(
            'improvement_suggestions',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('company_id', sa.Integer(), sa.ForeignKey('companies.id'), nullable=False),
            sa.Column('title', sa.String(200), nullable=False),
            sa.Column('problem', sa.Text(), nullable=False),
            sa.Column('proposed_solution', sa.Text(), nullable=False),
            sa.Column('expected_benefit', sa.Text(), nullable=False),
            sa.Column('category', sa.String(40), nullable=False),
            sa.Column('priority', sa.String(10), nullable=False, server_default='medium'),
            sa.Column('area', sa.String(150), nullable=True),
            sa.Column('status', sa.String(30), nullable=False, server_default='new'),
            sa.Column('owner_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
            sa.Column('owner_name', sa.String(255), nullable=True),
            sa.Column('target_date', sa.Date(), nullable=True),
            sa.Column('implementation_notes', sa.Text(), nullable=True),
            sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
            sa.Column('created_by_name', sa.String(255), nullable=False),
            sa.Column('updated_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
            sa.Column('updated_by_name', sa.String(255), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('implemented_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
            sa.CheckConstraint('version >= 1', name='ck_improvement_version'),
            sa.CheckConstraint("status IN ('new','under_review','approved','in_progress','implemented','on_hold','declined')", name='ck_improvement_status'),
            sa.CheckConstraint("priority IN ('low','medium','high')", name='ck_improvement_priority'),
            sa.CheckConstraint("category IN ('poka_yoke','five_s','standard_work','flow_layout','quality','safety_ergonomics','setup_reduction','equipment','inventory','other')", name='ck_improvement_category'),
            sa.CheckConstraint('length(trim(title)) BETWEEN 1 AND 200', name='ck_improvement_title'),
            sa.CheckConstraint("status <> 'implemented' OR (implementation_notes IS NOT NULL AND length(trim(implementation_notes)) > 0 AND implemented_at IS NOT NULL)", name='ck_improvement_implementation'),
        )
    _index('improvement_suggestions', 'ix_improvement_suggestions_company_id', ['company_id'])
    _index('improvement_suggestions', 'ix_improvement_suggestions_owner_id', ['owner_id'])
    _index('improvement_suggestions', 'ix_improvement_suggestions_created_by', ['created_by'])
    _index('improvement_suggestions', 'ix_improvement_suggestions_updated_by', ['updated_by'])
    _index('improvement_suggestions', 'ix_improvement_company_status_created', ['company_id', 'status', 'created_at', 'id'])
    _index('improvement_suggestions', 'ix_improvement_company_category', ['company_id', 'category'])
    _secure('improvement_suggestions')
    if not _exists('improvement_activities'):
        op.create_table(
            'improvement_activities',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('company_id', sa.Integer(), sa.ForeignKey('companies.id'), nullable=False),
            sa.Column('suggestion_id', sa.Integer(), sa.ForeignKey('improvement_suggestions.id'), nullable=False),
            sa.Column('kind', sa.String(30), nullable=False),
            sa.Column('actor_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
            sa.Column('actor_name', sa.String(255), nullable=False),
            sa.Column('body', sa.Text(), nullable=True),
            sa.Column('changes', sa.JSON(none_as_null=True), nullable=False, server_default='{}'),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint("kind IN ('submitted','updated','status_changed','comment')", name='ck_improvement_activity_kind'),
        )
    _index('improvement_activities', 'ix_improvement_activities_company_id', ['company_id'])
    _index('improvement_activities', 'ix_improvement_activities_actor_id', ['actor_id'])
    _index('improvement_activities', 'ix_improvement_activity_suggestion_created', ['suggestion_id', 'created_at', 'id'])
    _secure('improvement_activities')
    if op.get_bind().dialect.name == 'postgresql':
        op.execute("""CREATE OR REPLACE FUNCTION prevent_improvement_activity_change() RETURNS trigger
          LANGUAGE plpgsql SET search_path = pg_catalog AS $$ BEGIN
            RAISE EXCEPTION 'Continuous improvement activity cannot be modified or deleted';
          END $$""")
        op.execute('REVOKE ALL ON FUNCTION prevent_improvement_activity_change() FROM PUBLIC')
        op.execute('DROP TRIGGER IF EXISTS tr_improvement_activity_immutable ON improvement_activities')
        op.execute("""CREATE TRIGGER tr_improvement_activity_immutable BEFORE UPDATE OR DELETE ON improvement_activities
          FOR EACH ROW EXECUTE FUNCTION prevent_improvement_activity_change()""")


def downgrade():
    for table in ('improvement_activities', 'improvement_suggestions'):
        if op.get_context().as_sql or _exists(table):
            op.drop_table(table)
    if op.get_bind().dialect.name == 'postgresql':
        op.execute('DROP FUNCTION IF EXISTS prevent_improvement_activity_change()')
