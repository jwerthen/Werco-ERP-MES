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

# Every object is guarded: the fresh bootstrap (create_all -> stamp 058 -> upgrade) runs
# this over a schema the models already built. RLS and the REVOKEs stay unconditional --
# create_all issues neither, and both are idempotent.


def _offline() -> bool:
    # ``--sql`` has no database to inspect: emit every statement, as before.
    return op.get_context().as_sql


def _inspector():
    return sa.inspect(op.get_bind())


def _has_table(table_name: str) -> bool:
    return _inspector().has_table(table_name)


def _has_index(table_name: str, index_name: str) -> bool:
    if not _has_table(table_name):
        return False
    return any(ix['name'] == index_name for ix in _inspector().get_indexes(table_name))


def upgrade():
    offline = _offline()
    if offline or not _has_table('operational_inbox_states'):
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
    for name, columns in [
        ('ix_operational_inbox_states_company_id', ['company_id']),
        ('ix_operational_inbox_states_owner_id', ['owner_id']),
        ('ix_operational_inbox_states_updated_by', ['updated_by']),
        ('ix_inbox_company_owner', ['company_id', 'owner_id']),
    ]:
        if offline or not _has_index('operational_inbox_states', name):
            op.create_index(name, 'operational_inbox_states', columns)
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
    if _offline() or _has_table('operational_inbox_states'):
        op.drop_table('operational_inbox_states')
