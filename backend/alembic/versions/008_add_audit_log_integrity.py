"""Add audit log integrity fields and protection

Revision ID: 008_add_audit_log_integrity
Revises: 007_add_bom_line_type
Create Date: 2026-01-13

CMMC Level 2 Control: AU-3.3.8 - Protect Audit Information
- Adds sequence_number for gap detection
- Adds integrity_hash for tamper detection (SHA-256)
- Adds previous_hash for hash chain integrity
- Creates database triggers to prevent UPDATE/DELETE operations
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

revision = '008_add_audit_log_integrity'
down_revision = '007_add_bom_line_type'
branch_labels = None
depends_on = None


def column_exists(connection, table_name, column_name):
    """Check if a column exists in a table."""
    result = connection.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = :table AND column_name = :column"
    ), {"table": table_name, "column": column_name})
    return result.fetchone() is not None


def trigger_exists(connection, trigger_name):
    """Check if a trigger exists."""
    result = connection.execute(text(
        "SELECT 1 FROM pg_trigger WHERE tgname = :trigger_name"
    ), {"trigger_name": trigger_name})
    return result.fetchone() is not None


def function_exists(connection, function_name):
    """Check if a function exists."""
    result = connection.execute(text(
        "SELECT 1 FROM pg_proc WHERE proname = :function_name"
    ), {"function_name": function_name})
    return result.fetchone() is not None


def upgrade():
    conn = op.get_bind()
    
    # Add sequence_number column
    if not column_exists(conn, 'audit_logs', 'sequence_number'):
        # First add as nullable
        op.add_column('audit_logs',
            sa.Column('sequence_number', sa.BigInteger(), nullable=True))
        
        # Populate existing records with sequence numbers
        op.execute("""
            WITH numbered AS (
                SELECT id, ROW_NUMBER() OVER (ORDER BY timestamp, id) as seq
                FROM audit_logs
            )
            UPDATE audit_logs
            SET sequence_number = numbered.seq
            FROM numbered
            WHERE audit_logs.id = numbered.id
        """)
        
        # Make it not nullable and add unique constraint
        op.alter_column('audit_logs', 'sequence_number', nullable=False)
        op.create_index('ix_audit_logs_sequence_number', 'audit_logs', ['sequence_number'], unique=True)
    
    # Add integrity_hash column
    if not column_exists(conn, 'audit_logs', 'integrity_hash'):
        # First add as nullable
        op.add_column('audit_logs',
            sa.Column('integrity_hash', sa.String(64), nullable=True))
        
        # Set placeholder hash for existing records
        # (These won't be verifiable in the chain, but new records will be)
        op.execute("""
            UPDATE audit_logs
            SET integrity_hash = 'LEGACY_' || LPAD(id::text, 56, '0')
            WHERE integrity_hash IS NULL
        """)
        
        # Make it not nullable
        op.alter_column('audit_logs', 'integrity_hash', nullable=False)
    
    # Add previous_hash column
    if not column_exists(conn, 'audit_logs', 'previous_hash'):
        op.add_column('audit_logs',
            sa.Column('previous_hash', sa.String(64), nullable=True))
        
        # Set previous_hash for existing records to link them
        op.execute("""
            UPDATE audit_logs a
            SET previous_hash = (
                SELECT integrity_hash
                FROM audit_logs b
                WHERE b.sequence_number = a.sequence_number - 1
            )
            WHERE sequence_number > 1
        """)
    
    # Create composite index for integrity verification
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_audit_logs_integrity 
        ON audit_logs (sequence_number, integrity_hash)
    """)
    
    # Create function to prevent updates on audit_logs
    if not function_exists(conn, 'audit_log_immutable_update'):
        op.execute("""
            CREATE OR REPLACE FUNCTION audit_log_immutable_update()
            RETURNS TRIGGER AS $$
            BEGIN
                RAISE EXCEPTION 'CMMC AU-3.3.8 VIOLATION: Audit logs are immutable and cannot be updated. Record ID: %, Sequence: %', 
                    OLD.id, OLD.sequence_number;
                RETURN NULL;
            END;
            $$ LANGUAGE plpgsql;
        """)
    
    # Create function to prevent deletes on audit_logs
    if not function_exists(conn, 'audit_log_immutable_delete'):
        op.execute("""
            CREATE OR REPLACE FUNCTION audit_log_immutable_delete()
            RETURNS TRIGGER AS $$
            BEGIN
                RAISE EXCEPTION 'CMMC AU-3.3.8 VIOLATION: Audit logs are immutable and cannot be deleted. Record ID: %, Sequence: %', 
                    OLD.id, OLD.sequence_number;
                RETURN NULL;
            END;
            $$ LANGUAGE plpgsql;
        """)
    
    # Create trigger to prevent updates
    if not trigger_exists(conn, 'tr_audit_log_no_update'):
        op.execute("""
            CREATE TRIGGER tr_audit_log_no_update
            BEFORE UPDATE ON audit_logs
            FOR EACH ROW
            EXECUTE FUNCTION audit_log_immutable_update();
        """)
    
    # Create trigger to prevent deletes
    if not trigger_exists(conn, 'tr_audit_log_no_delete'):
        op.execute("""
            CREATE TRIGGER tr_audit_log_no_delete
            BEFORE DELETE ON audit_logs
            FOR EACH ROW
            EXECUTE FUNCTION audit_log_immutable_delete();
        """)
    
    # Add comment to table documenting the compliance requirement
    op.execute("""
        COMMENT ON TABLE audit_logs IS 
        'CMMC Level 2 AU-3.3.8 Compliant Audit Log. 
        This table is protected by database triggers that prevent UPDATE and DELETE operations.
        Integrity is verified via SHA-256 hash chain (integrity_hash, previous_hash).
        Sequence numbers enable gap detection for tamper evidence.';
    """)


def downgrade():
    conn = op.get_bind()
    
    # Drop triggers
    if trigger_exists(conn, 'tr_audit_log_no_delete'):
        op.execute("DROP TRIGGER IF EXISTS tr_audit_log_no_delete ON audit_logs")
    
    if trigger_exists(conn, 'tr_audit_log_no_update'):
        op.execute("DROP TRIGGER IF EXISTS tr_audit_log_no_update ON audit_logs")
    
    # Drop functions
    if function_exists(conn, 'audit_log_immutable_delete'):
        op.execute("DROP FUNCTION IF EXISTS audit_log_immutable_delete()")
    
    if function_exists(conn, 'audit_log_immutable_update'):
        op.execute("DROP FUNCTION IF EXISTS audit_log_immutable_update()")
    
    # Drop index
    op.execute("DROP INDEX IF EXISTS ix_audit_logs_integrity")
    
    # Drop columns
    if column_exists(conn, 'audit_logs', 'previous_hash'):
        op.drop_column('audit_logs', 'previous_hash')
    
    if column_exists(conn, 'audit_logs', 'integrity_hash'):
        op.drop_column('audit_logs', 'integrity_hash')
    
    if column_exists(conn, 'audit_logs', 'sequence_number'):
        op.drop_index('ix_audit_logs_sequence_number', 'audit_logs')
        op.drop_column('audit_logs', 'sequence_number')
    
    # Remove table comment
    op.execute("COMMENT ON TABLE audit_logs IS NULL")
