"""Append-only, tenant-scoped process inputs for estimator reuse.

A profile preserves its parameter basis; it does not certify a part's routing,
geometry, machine feasibility, or quote approval.
"""

from datetime import datetime

from sqlalchemy import (
    DDL,
    JSON,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    event,
)

from app.db.database import Base
from app.db.mixins import TenantMixin


class FabricationQuoteProfile(Base, TenantMixin):
    __tablename__ = "fabrication_quote_profiles"
    __table_args__ = (
        UniqueConstraint("company_id", "key", "revision", name="uq_fqp_revision"),
        CheckConstraint("revision >= 1", name="ck_fqp_revision"),
        CheckConstraint("thickness_mm IS NULL OR thickness_mm > 0", name="ck_fqp_thickness"),
        Index("ix_fqp_company_process", "company_id", "process", "name"),
    )

    id = Column(Integer, primary_key=True)
    key = Column(String(36), nullable=False)
    revision = Column(Integer, nullable=False)
    name = Column(String(200), nullable=False)
    process = Column(String(100), nullable=False)
    machine = Column(String(200), nullable=True)
    material = Column(String(200), nullable=True)
    thickness_mm = Column(Numeric(24, 9), nullable=True)
    currency = Column(String(3), nullable=False)
    template_json = Column(JSON, nullable=False)
    evidence_note = Column(Text, nullable=False)
    content_sha256 = Column(String(64), nullable=False)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


def _deny_mutation(_mapper, _connection, _target):
    raise ValueError("Process profiles are immutable; append a new revision.")


def integrity_ddl(dialect: str) -> list[str]:
    table = "fabrication_quote_profiles"
    if dialect == "sqlite":
        return [
            f"CREATE TRIGGER IF NOT EXISTS tr_{table}_{action} BEFORE {action.upper()} ON {table} BEGIN SELECT RAISE(ABORT, 'Process profiles are immutable'); END"
            for action in ("update", "delete")
        ]
    if dialect != "postgresql":
        raise ValueError("Unsupported profile storage dialect")
    statements = [
        f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY",
        f"REVOKE ALL ON TABLE {table} FROM PUBLIC",
        f"REVOKE ALL ON SEQUENCE {table}_id_seq FROM PUBLIC",
    ]
    for role in ("anon", "authenticated"):
        statements.append(
            f"DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{role}') THEN REVOKE ALL ON TABLE {table} FROM {role}; REVOKE ALL ON SEQUENCE {table}_id_seq FROM {role}; END IF; END $$"  # nosec B608
        )
    statements.append(
        f"CREATE OR REPLACE FUNCTION {table}_immutable() RETURNS TRIGGER LANGUAGE plpgsql SET search_path = '' AS $$ BEGIN RAISE EXCEPTION 'Process profiles are immutable' USING ERRCODE='23514'; RETURN NULL; END; $$"
    )
    for action in ("update", "delete", "truncate"):
        statements.extend(
            [
                f"DROP TRIGGER IF EXISTS tr_{table}_{action} ON {table}",
                f"CREATE TRIGGER tr_{table}_{action} BEFORE {action.upper()} ON {table} FOR EACH {'STATEMENT' if action == 'truncate' else 'ROW'} EXECUTE FUNCTION {table}_immutable()",
            ]
        )
    return statements


event.listen(FabricationQuoteProfile, "before_update", _deny_mutation)
event.listen(FabricationQuoteProfile, "before_delete", _deny_mutation)
for dialect in ("sqlite", "postgresql"):
    for statement in integrity_ddl(dialect):
        event.listen(
            FabricationQuoteProfile.__table__,
            "after_create",
            DDL(statement).execute_if(dialect=dialect),
        )
