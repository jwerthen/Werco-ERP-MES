"""Create append-only reusable fabrication process profiles.

Revision ID: 107_fabrication_quote_profiles
Revises: 106_fabrication_quoting
"""

import sqlalchemy as sa

from alembic import op

revision = "107_fabrication_quote_profiles"
down_revision = "106_fabrication_quoting"
branch_labels = None
depends_on = None


def _security():
    table = "fabrication_quote_profiles"
    if op.get_bind().dialect.name == "postgresql":
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"REVOKE ALL ON TABLE {table} FROM PUBLIC")
        op.execute(f"REVOKE ALL ON SEQUENCE {table}_id_seq FROM PUBLIC")
        for role in ("anon", "authenticated"):
            op.execute(
                f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{role}') THEN REVOKE ALL ON TABLE {table} FROM {role}; REVOKE ALL ON SEQUENCE {table}_id_seq FROM {role}; END IF; END $$"
            )
        op.execute(
            f"CREATE OR REPLACE FUNCTION {table}_immutable() RETURNS TRIGGER LANGUAGE plpgsql SET search_path = '' AS $$ BEGIN RAISE EXCEPTION 'Process profiles are immutable' USING ERRCODE='23514'; RETURN NULL; END; $$"
        )
        for action in ("update", "delete", "truncate"):
            op.execute(
                f"CREATE TRIGGER tr_{table}_{action} BEFORE {action.upper()} ON {table} FOR EACH {'STATEMENT' if action == 'truncate' else 'ROW'} EXECUTE FUNCTION {table}_immutable()"
            )
    elif op.get_bind().dialect.name == "sqlite":
        for action in ("update", "delete"):
            op.execute(
                f"CREATE TRIGGER IF NOT EXISTS tr_{table}_{action} BEFORE {action.upper()} ON {table} BEGIN SELECT RAISE(ABORT, 'Process profiles are immutable'); END"
            )


def upgrade():
    op.create_table(
        "fabrication_quote_profiles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "company_id", sa.Integer(), sa.ForeignKey("companies.id"), nullable=False
        ),
        sa.Column("key", sa.String(36), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("process", sa.String(100), nullable=False),
        sa.Column("machine", sa.String(200)),
        sa.Column("material", sa.String(200)),
        sa.Column("thickness_mm", sa.Numeric(24, 9)),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("template_json", sa.JSON(), nullable=False),
        sa.Column("evidence_note", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column(
            "created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("company_id", "key", "revision", name="uq_fqp_revision"),
        sa.CheckConstraint("revision >= 1", name="ck_fqp_revision"),
        sa.CheckConstraint(
            "thickness_mm IS NULL OR thickness_mm > 0", name="ck_fqp_thickness"
        ),
    )
    op.create_index(
        "ix_fabrication_quote_profiles_company_id",
        "fabrication_quote_profiles",
        ["company_id"],
    )
    op.create_index(
        "ix_fqp_company_process",
        "fabrication_quote_profiles",
        ["company_id", "process", "name"],
    )
    _security()


def downgrade():
    op.drop_table("fabrication_quote_profiles")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION IF EXISTS fabrication_quote_profiles_immutable()")
