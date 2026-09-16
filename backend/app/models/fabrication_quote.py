"""Replacement fabrication estimates with append-only evidence and revisions.

The operational ERP Quote is a separate handoff target. No historical quote is
repriced or migrated by these models.
"""

from datetime import datetime

from sqlalchemy import (
    DDL,
    JSON,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    event,
)

from app.db.database import Base
from app.db.mixins import TenantMixin


class FabricationQuote(Base, TenantMixin):
    __tablename__ = "fabrication_quotes"
    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_fq_company_id"),
        UniqueConstraint("company_id", "request_key", name="uq_fq_request"),
        CheckConstraint("revision >= 1", name="ck_fq_revision"),
        CheckConstraint("status IN ('draft', 'approved', 'handed_off')", name="ck_fq_status"),
        Index("ix_fq_company_updated", "company_id", "updated_at", "id"),
    )
    id = Column(Integer, primary_key=True)
    title = Column(String(200), nullable=False)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=True)
    status = Column(String(20), nullable=False, default="draft")
    revision = Column(Integer, nullable=False, default=1)
    plan_json = Column(JSON, nullable=False)
    calculation_json = Column(JSON, nullable=False)
    request_key = Column(String(64), nullable=True)
    request_hash = Column(String(64), nullable=True)
    approved_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    approved_revision = Column(Integer, nullable=True)
    erp_quote_id = Column(Integer, ForeignKey("quotes.id"), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class FabricationQuoteRevision(Base, TenantMixin):
    __tablename__ = "fabrication_quote_revisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["company_id", "quote_id"],
            ["fabrication_quotes.company_id", "fabrication_quotes.id"],
            name="fk_fqr_quote",
        ),
        UniqueConstraint("quote_id", "revision", name="uq_fqr_number"),
        CheckConstraint("revision >= 1", name="ck_fqr_revision"),
    )
    id = Column(Integer, primary_key=True)
    quote_id = Column(Integer, nullable=False)
    revision = Column(Integer, nullable=False)
    action = Column(String(30), nullable=False)
    snapshot_json = Column(JSON, nullable=False)
    content_sha256 = Column(String(64), nullable=False)
    note = Column(Text, nullable=False, default="")
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class FabricationQuoteFile(Base, TenantMixin):
    __tablename__ = "fabrication_quote_files"
    __table_args__ = (
        ForeignKeyConstraint(
            ["company_id", "quote_id"],
            ["fabrication_quotes.company_id", "fabrication_quotes.id"],
            name="fk_fqf_quote",
        ),
        UniqueConstraint("quote_id", "sha256", "units_override", name="uq_fqf_source"),
        CheckConstraint("byte_count > 0 AND byte_count <= 26214400", name="ck_fqf_bytes"),
    )
    id = Column(Integer, primary_key=True)
    quote_id = Column(Integer, nullable=False)
    file_name = Column(String(255), nullable=False)
    sha256 = Column(String(64), nullable=False)
    byte_count = Column(Integer, nullable=False)
    units_override = Column(String(10), nullable=False, default="")
    content_type = Column(String(100), nullable=False)
    content = Column(LargeBinary, nullable=False)
    analysis_json = Column(JSON, nullable=False)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class FabricationQuoteActual(Base, TenantMixin):
    __tablename__ = "fabrication_quote_actuals"
    __table_args__ = (
        ForeignKeyConstraint(
            ["quote_id", "quote_revision"],
            [
                "fabrication_quote_revisions.quote_id",
                "fabrication_quote_revisions.revision",
            ],
            name="fk_fqa_revision",
        ),
        ForeignKeyConstraint(
            ["company_id", "quote_id"],
            ["fabrication_quotes.company_id", "fabrication_quotes.id"],
            name="fk_fqa_quote",
        ),
        UniqueConstraint("company_id", "request_key", name="uq_fqa_request"),
    )
    id = Column(Integer, primary_key=True)
    quote_id = Column(Integer, nullable=False)
    quote_revision = Column(Integer, nullable=False)
    request_key = Column(String(64), nullable=False)
    observation_json = Column(JSON, nullable=False)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


_TABLES = (
    FabricationQuote,
    FabricationQuoteRevision,
    FabricationQuoteFile,
    FabricationQuoteActual,
)
_IMMUTABLE = (FabricationQuoteRevision, FabricationQuoteFile, FabricationQuoteActual)


def _deny_mutation(_mapper, _connection, _target):
    raise ValueError("Fabrication quote evidence is immutable; append a new revision.")


# Models and migration use the same defined DDL. The migration freezes a copy,
# avoiding application imports when upgrading an older installation.
def integrity_ddl(table_name: str, immutable: bool, dialect: str) -> list[str]:
    if table_name not in {model.__tablename__ for model in _TABLES}:
        raise ValueError("Unknown fabrication quote table")
    if dialect == "sqlite":
        if not immutable:
            return []
        return [
            f"CREATE TRIGGER IF NOT EXISTS tr_{table_name}_{action} BEFORE {action.upper()} ON {table_name} BEGIN SELECT RAISE(ABORT, 'Fabrication quote evidence is immutable'); END"
            for action in ("update", "delete")
        ]
    statements = [
        f"ALTER TABLE {table_name} ENABLE ROW LEVEL SECURITY",
        f"REVOKE ALL ON TABLE {table_name} FROM PUBLIC",
        f"REVOKE ALL ON SEQUENCE {table_name}_id_seq FROM PUBLIC",
    ]
    for role in ("anon", "authenticated"):
        statements.append(
            # Table is allowlisted above; role comes only from the fixed tuple.
            f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{role}') THEN REVOKE ALL ON TABLE {table_name} FROM {role}; REVOKE ALL ON SEQUENCE {table_name}_id_seq FROM {role}; END IF; END $$"  # nosec B608
        )
    if immutable:
        statements.append(
            f"CREATE OR REPLACE FUNCTION {table_name}_immutable() RETURNS TRIGGER LANGUAGE plpgsql SET search_path = '' AS $$ BEGIN RAISE EXCEPTION 'Fabrication quote evidence is immutable' USING ERRCODE='23514'; RETURN NULL; END; $$"
        )
        for action in ("update", "delete", "truncate"):
            statements.extend(
                [
                    f"DROP TRIGGER IF EXISTS tr_{table_name}_{action} ON {table_name}",
                    f"CREATE TRIGGER tr_{table_name}_{action} BEFORE {action.upper()} ON {table_name} FOR EACH {'STATEMENT' if action == 'truncate' else 'ROW'} EXECUTE FUNCTION {table_name}_immutable()",
                ]
            )
    return statements


for model in _TABLES:
    immutable = model in _IMMUTABLE
    if immutable:
        event.listen(model, "before_update", _deny_mutation)
        event.listen(model, "before_delete", _deny_mutation)
    for dialect in ("sqlite", "postgresql"):
        for statement in integrity_ddl(model.__tablename__, immutable, dialect):
            event.listen(
                model.__table__,
                "after_create",
                DDL(statement).execute_if(dialect=dialect),
            )
