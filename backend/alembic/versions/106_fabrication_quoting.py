"""Create replacement fabrication quoting storage without migrating old quotes.

Revision ID: 106_fabrication_quoting
Revises: 105_nesting_cad_sources
"""
import sqlalchemy as sa
from alembic import op

revision = "106_fabrication_quoting"
down_revision = "105_nesting_cad_sources"
branch_labels = None
depends_on = None


def _security(table, immutable):
    # Names are migration constants, never request input.
    if op.get_bind().dialect.name == "postgresql":
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL ON TABLE {table} FROM PUBLIC")
        op.execute(f"REVOKE ALL ON SEQUENCE {table}_id_seq FROM PUBLIC")
        for role in ("anon", "authenticated"):
            op.execute(f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{role}') THEN REVOKE ALL ON TABLE {table} FROM {role}; REVOKE ALL ON SEQUENCE {table}_id_seq FROM {role}; END IF; END $$")
        if immutable:
            op.execute(f"CREATE OR REPLACE FUNCTION {table}_immutable() RETURNS TRIGGER LANGUAGE plpgsql SET search_path = '' AS $$ BEGIN RAISE EXCEPTION 'Fabrication quote evidence is immutable' USING ERRCODE='23514'; RETURN NULL; END; $$")
            for action in ("update", "delete", "truncate"):
                op.execute(f"CREATE TRIGGER tr_{table}_{action} BEFORE {action.upper()} ON {table} FOR EACH {'STATEMENT' if action == 'truncate' else 'ROW'} EXECUTE FUNCTION {table}_immutable()")
    elif immutable:
        for action in ("update", "delete"):
            op.execute(f"CREATE TRIGGER IF NOT EXISTS tr_{table}_{action} BEFORE {action.upper()} ON {table} BEGIN SELECT RAISE(ABORT, 'Fabrication quote evidence is immutable'); END")


def upgrade():
    op.create_table("fabrication_quotes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id")),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("plan_json", sa.JSON(), nullable=False),
        sa.Column("calculation_json", sa.JSON(), nullable=False),
        sa.Column("request_key", sa.String(64)),
        sa.Column("request_hash", sa.String(64)),
        sa.Column("approved_by", sa.Integer(), sa.ForeignKey("users.id")),
        sa.Column("approved_at", sa.DateTime()),
        sa.Column("approved_revision", sa.Integer()),
        sa.Column("erp_quote_id", sa.Integer(), sa.ForeignKey("quotes.id")),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("company_id", "id", name="uq_fq_company_id"),
        sa.UniqueConstraint("company_id", "request_key", name="uq_fq_request"),
        sa.CheckConstraint("revision >= 1", name="ck_fq_revision"),
        sa.CheckConstraint("status IN ('draft', 'approved', 'handed_off')", name="ck_fq_status"),
    )
    op.create_index("ix_fabrication_quotes_company_id", "fabrication_quotes", ["company_id"])
    op.create_index("ix_fq_company_updated", "fabrication_quotes", ["company_id", "updated_at", "id"])
    op.create_table("fabrication_quote_revisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("quote_id", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(30), nullable=False),
        sa.Column("snapshot_json", sa.JSON(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["company_id", "quote_id"], ["fabrication_quotes.company_id", "fabrication_quotes.id"], name="fk_fqr_quote"),
        sa.UniqueConstraint("quote_id", "revision", name="uq_fqr_number"),
        sa.CheckConstraint("revision >= 1", name="ck_fqr_revision"),
    )
    op.create_table("fabrication_quote_files",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("quote_id", sa.Integer(), nullable=False),
        sa.Column("file_name", sa.String(255), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("byte_count", sa.Integer(), nullable=False),
        sa.Column("units_override", sa.String(10), nullable=False),
        sa.Column("content_type", sa.String(100), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("analysis_json", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["company_id", "quote_id"], ["fabrication_quotes.company_id", "fabrication_quotes.id"], name="fk_fqf_quote"),
        sa.UniqueConstraint("quote_id", "sha256", "units_override", name="uq_fqf_source"),
        sa.CheckConstraint("byte_count > 0 AND byte_count <= 26214400", name="ck_fqf_bytes"),
    )
    op.create_table("fabrication_quote_actuals",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False),
        sa.Column("quote_id", sa.Integer(), nullable=False),
        sa.Column("quote_revision", sa.Integer(), nullable=False),
        sa.Column("request_key", sa.String(64), nullable=False),
        sa.Column("observation_json", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["company_id", "quote_id"], ["fabrication_quotes.company_id", "fabrication_quotes.id"], name="fk_fqa_quote"),
        sa.ForeignKeyConstraint(["quote_id", "quote_revision"], ["fabrication_quote_revisions.quote_id", "fabrication_quote_revisions.revision"], name="fk_fqa_revision"),
        sa.UniqueConstraint("company_id", "request_key", name="uq_fqa_request"),
    )
    for table in ("fabrication_quote_revisions", "fabrication_quote_files", "fabrication_quote_actuals"):
        op.create_index(f"ix_{table}_company_id", table, ["company_id"])
        _security(table, True)
    _security("fabrication_quotes", False)


def downgrade():
    for table in ("fabrication_quote_actuals", "fabrication_quote_files", "fabrication_quote_revisions", "fabrication_quotes"):
        op.drop_table(table)
        if op.get_bind().dialect.name == "postgresql" and table != "fabrication_quotes":
            op.execute(f"DROP FUNCTION IF EXISTS {table}_immutable()")
