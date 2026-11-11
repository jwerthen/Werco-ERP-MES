"""Add each person's saved, tenant/user-bound dashboard layout.

Revision ID: 118_dashboard_layouts
Revises: 117_operation_short_declared
"""

import sqlalchemy as sa

from alembic import op

revision = '118_dashboard_layouts'
down_revision = '117_operation_short_declared'
branch_labels = None
depends_on = None

TABLE = 'dashboard_layouts'


def _exists():
    return not op.get_context().as_sql and sa.inspect(op.get_bind()).has_table(TABLE)


def _index(name, columns):
    if op.get_context().as_sql or name not in {row['name'] for row in sa.inspect(op.get_bind()).get_indexes(TABLE)}:
        op.create_index(name, TABLE, columns)


def upgrade():
    # create_all already builds this shape on a fresh install. On an existing installation
    # this is additive: an empty table, no rows seeded -- everyone keeps the stock default
    # dashboard until they save a layout of their own.
    if not _exists():
        op.create_table(
            TABLE,
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('company_id', sa.Integer(), sa.ForeignKey('companies.id'), nullable=False),
            sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
            sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
            sa.Column('layout_json', sa.JSON(none_as_null=True), nullable=False, server_default='{}'),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
            sa.UniqueConstraint('company_id', 'user_id', name='uq_dashboard_layout_company_user'),
            sa.CheckConstraint('version >= 1', name='ck_dashboard_layout_version'),
        )
    _index('ix_dashboard_layouts_company_id', ['company_id'])
    _index('ix_dashboard_layouts_user_id', ['user_id'])
    # RLS / REVOKE run under the dialect check only -- never under the table guard -- because
    # create_all can build this table before this revision runs (Postgres-only; idempotent).
    if op.get_bind().dialect.name == 'postgresql':
        op.execute('ALTER TABLE dashboard_layouts ENABLE ROW LEVEL SECURITY')
        op.execute('REVOKE ALL ON TABLE dashboard_layouts FROM PUBLIC')
        op.execute('REVOKE ALL ON SEQUENCE dashboard_layouts_id_seq FROM PUBLIC')
        op.execute("""DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
            REVOKE ALL ON TABLE dashboard_layouts FROM anon;
            REVOKE ALL ON SEQUENCE dashboard_layouts_id_seq FROM anon;
          END IF;
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN
            REVOKE ALL ON TABLE dashboard_layouts FROM authenticated;
            REVOKE ALL ON SEQUENCE dashboard_layouts_id_seq FROM authenticated;
          END IF;
        END $$""")


def downgrade():
    if op.get_context().as_sql or _exists():
        op.drop_table(TABLE)
