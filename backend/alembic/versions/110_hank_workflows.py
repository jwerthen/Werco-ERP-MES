"""Add durable Hank document intake, handoffs and approved procedure runs.

Revision ID: 110_hank_workflows
Revises: 109_hank_preferences
"""

import sqlalchemy as sa
from alembic import op

revision = '110_hank_workflows'
down_revision = '109_hank_preferences'
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
    if not _exists('hank_intake_batches'):
        op.create_table(
            'hank_intake_batches',
            sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
            sa.Column('owner_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
            sa.Column('credential_key', sa.String(length=160), nullable=False),
            sa.Column('request_key', sa.String(length=36), nullable=False),
            sa.Column('request_hash', sa.String(length=64), nullable=False),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('company_id', sa.Integer(), sa.ForeignKey('companies.id'), nullable=False),
            sa.UniqueConstraint('company_id', 'request_key', name='uq_hank_intake_batch_request'),
        )
    _index('hank_intake_batches', 'ix_hank_intake_batch_owner', ['company_id', 'owner_id', 'id'])
    _index('hank_intake_batches', 'ix_hank_intake_batches_company_id', ['company_id'])
    _index('hank_intake_batches', 'ix_hank_intake_batches_owner_id', ['owner_id'])
    _secure('hank_intake_batches')
    if not _exists('hank_intake_files'):
        op.create_table(
            'hank_intake_files',
            sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
            sa.Column('batch_id', sa.Integer(), sa.ForeignKey('hank_intake_batches.id'), nullable=False),
            sa.Column('ordinal', sa.Integer(), nullable=False),
            sa.Column('filename', sa.String(length=255), nullable=False),
            sa.Column('content_sha256', sa.String(length=64), nullable=False),
            sa.Column('storage_ref', sa.String(length=500), nullable=False),
            sa.Column('file_size', sa.Integer(), nullable=False),
            sa.Column('page_count', sa.Integer(), nullable=True),
            sa.Column('status', sa.String(length=30), nullable=False, server_default='queued'),
            sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
            sa.Column('analysis_json', sa.JSON(none_as_null=True), nullable=False, server_default='{}'),
            sa.Column('plan_json', sa.JSON(none_as_null=True), nullable=True),
            sa.Column('result_json', sa.JSON(none_as_null=True), nullable=True),
            sa.Column('error_code', sa.String(length=64), nullable=True),
            sa.Column('error_message', sa.String(length=1200), nullable=True),
            sa.Column('processing_started_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('company_id', sa.Integer(), sa.ForeignKey('companies.id'), nullable=False),
            sa.CheckConstraint('ordinal BETWEEN 0 AND 4', name='ck_hank_intake_file_ordinal'),
            sa.CheckConstraint('file_size > 0 AND file_size <= 10485760', name='ck_hank_intake_file_size'),
            sa.CheckConstraint(
                "status IN ('queued','analyzing','awaiting_review','planned','completed','failed','cancelled')",
                name='ck_hank_intake_file_status',
            ),
            sa.CheckConstraint('version >= 1', name='ck_hank_intake_file_version'),
            sa.UniqueConstraint('batch_id', 'ordinal', name='uq_hank_intake_file_ordinal'),
        )
    _index('hank_intake_files', 'ix_hank_intake_file_batch', ['batch_id', 'id'])
    _index('hank_intake_files', 'ix_hank_intake_file_hash', ['company_id', 'content_sha256'])
    _index('hank_intake_files', 'ix_hank_intake_file_status', ['status', 'updated_at', 'id'])
    _index('hank_intake_files', 'ix_hank_intake_files_company_id', ['company_id'])
    _secure('hank_intake_files')
    if not _exists('hank_handoffs'):
        op.create_table(
            'hank_handoffs',
            sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
            sa.Column('sender_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
            sa.Column('recipient_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
            sa.Column('sender_name', sa.String(length=255), nullable=False),
            sa.Column('recipient_name', sa.String(length=255), nullable=False),
            sa.Column('work_order_id', sa.Integer(), sa.ForeignKey('work_orders.id'), nullable=False),
            sa.Column('work_order_number', sa.String(length=100), nullable=False),
            sa.Column('request_key', sa.String(length=36), nullable=False),
            sa.Column('request_hash', sa.String(length=64), nullable=False),
            sa.Column('status', sa.String(length=30), nullable=False, server_default='open'),
            sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
            sa.Column('content_json', sa.JSON(none_as_null=True), nullable=False, server_default='{}'),
            sa.Column('attachments_json', sa.JSON(none_as_null=True), nullable=False, server_default='[]'),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('acknowledged_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('company_id', sa.Integer(), sa.ForeignKey('companies.id'), nullable=False),
            sa.CheckConstraint('sender_id <> recipient_id', name='ck_hank_handoff_participants'),
            sa.CheckConstraint(
                "status IN ('open','acknowledged','completed','cancelled')", name='ck_hank_handoff_status'
            ),
            sa.CheckConstraint('version >= 1', name='ck_hank_handoff_version'),
            sa.UniqueConstraint('company_id', 'request_key', name='uq_hank_handoff_request'),
        )
    _index('hank_handoffs', 'ix_hank_handoff_recipient_status', ['company_id', 'recipient_id', 'status', 'id'])
    _index('hank_handoffs', 'ix_hank_handoff_sender_status', ['company_id', 'sender_id', 'status', 'id'])
    _index('hank_handoffs', 'ix_hank_handoffs_company_id', ['company_id'])
    _index('hank_handoffs', 'ix_hank_handoffs_recipient_id', ['recipient_id'])
    _index('hank_handoffs', 'ix_hank_handoffs_sender_id', ['sender_id'])
    _index('hank_handoffs', 'ix_hank_handoffs_work_order_id', ['work_order_id'])
    _secure('hank_handoffs')
    if not _exists('hank_routines'):
        op.create_table(
            'hank_routines',
            sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
            sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
            sa.Column('approved_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
            sa.Column('request_key', sa.String(length=36), nullable=False),
            sa.Column('request_hash', sa.String(length=64), nullable=False),
            sa.Column('title', sa.String(length=160), nullable=False),
            sa.Column('description', sa.String(length=2000), nullable=False, server_default=''),
            sa.Column('status', sa.String(length=30), nullable=False, server_default='draft'),
            sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
            sa.Column('steps_json', sa.JSON(none_as_null=True), nullable=False, server_default='[]'),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('approved_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('company_id', sa.Integer(), sa.ForeignKey('companies.id'), nullable=False),
            sa.CheckConstraint("status IN ('draft','approved','archived')", name='ck_hank_routine_status'),
            sa.CheckConstraint('version >= 1', name='ck_hank_routine_version'),
            sa.UniqueConstraint('company_id', 'request_key', name='uq_hank_routine_request'),
        )
    _index('hank_routines', 'ix_hank_routine_company_status', ['company_id', 'status', 'id'])
    _index('hank_routines', 'ix_hank_routines_approved_by', ['approved_by'])
    _index('hank_routines', 'ix_hank_routines_company_id', ['company_id'])
    _index('hank_routines', 'ix_hank_routines_created_by', ['created_by'])
    _secure('hank_routines')
    if not _exists('hank_routine_runs'):
        op.create_table(
            'hank_routine_runs',
            sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
            sa.Column('routine_id', sa.Integer(), sa.ForeignKey('hank_routines.id'), nullable=False),
            sa.Column('owner_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
            sa.Column('request_key', sa.String(length=36), nullable=False),
            sa.Column('request_hash', sa.String(length=64), nullable=False),
            sa.Column('status', sa.String(length=30), nullable=False, server_default='active'),
            sa.Column('version', sa.Integer(), nullable=False, server_default='1'),
            sa.Column('current_step', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('snapshot_json', sa.JSON(none_as_null=True), nullable=False, server_default='{}'),
            sa.Column('context_json', sa.JSON(none_as_null=True), nullable=False, server_default='{}'),
            sa.Column('results_json', sa.JSON(none_as_null=True), nullable=False, server_default='[]'),
            sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
            sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
            sa.Column('company_id', sa.Integer(), sa.ForeignKey('companies.id'), nullable=False),
            sa.CheckConstraint("status IN ('active','completed','cancelled')", name='ck_hank_routine_run_status'),
            sa.CheckConstraint('version >= 1 AND current_step >= 0', name='ck_hank_routine_run_version'),
            sa.UniqueConstraint('company_id', 'request_key', name='uq_hank_routine_run_request'),
        )
    _index('hank_routine_runs', 'ix_hank_routine_run_owner_status', ['company_id', 'owner_id', 'status', 'id'])
    _index('hank_routine_runs', 'ix_hank_routine_runs_company_id', ['company_id'])
    _index('hank_routine_runs', 'ix_hank_routine_runs_owner_id', ['owner_id'])
    _index('hank_routine_runs', 'ix_hank_routine_runs_routine_id', ['routine_id'])
    _secure('hank_routine_runs')


def downgrade():
    if op.get_context().as_sql or _exists('hank_routine_runs'):
        op.drop_table('hank_routine_runs')
    if op.get_context().as_sql or _exists('hank_routines'):
        op.drop_table('hank_routines')
    if op.get_context().as_sql or _exists('hank_handoffs'):
        op.drop_table('hank_handoffs')
    if op.get_context().as_sql or _exists('hank_intake_files'):
        op.drop_table('hank_intake_files')
    if op.get_context().as_sql or _exists('hank_intake_batches'):
        op.drop_table('hank_intake_batches')
