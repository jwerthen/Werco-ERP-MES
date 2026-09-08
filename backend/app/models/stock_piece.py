"""Advisory physical-piece observations; no availability or inventory balance."""

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
    String,
    Text,
    UniqueConstraint,
    event,
)

from app.db.database import Base
from app.db.mixins import TenantMixin


class StockPiece(Base, TenantMixin):
    __tablename__ = "stock_pieces"
    __table_args__ = (
        UniqueConstraint("company_id", "id", name="uq_stock_piece_company_id"),
        UniqueConstraint("company_id", "label", name="uq_stock_piece_company_label"),
        CheckConstraint(
            "version >= 1 AND version = latest_observation_number",
            name="ck_stock_piece_version",
        ),
        CheckConstraint("length(trim(label)) BETWEEN 1 AND 120", name="ck_stock_piece_label"),
        Index("ix_stock_piece_company_updated", "company_id", "updated_at", "id"),
    )
    id = Column(Integer, primary_key=True)
    label = Column(String(120), nullable=False)
    version = Column(Integer, nullable=False, default=1, server_default="1")
    latest_observation_number = Column(Integer, nullable=False, default=1, server_default="1")
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class StockPieceObservation(Base, TenantMixin):
    __tablename__ = "stock_piece_observations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["company_id", "stock_piece_id"],
            ["stock_pieces.company_id", "stock_pieces.id"],
            name="fk_stock_observation_tenant_piece",
        ),
        UniqueConstraint("company_id", "request_key", name="uq_stock_observation_request"),
        UniqueConstraint("stock_piece_id", "observation_number", name="uq_stock_observation_number"),
        CheckConstraint("observation_number >= 1", name="ck_stock_observation_number"),
        CheckConstraint("state IN ('RECORDED', 'WITHDRAWN')", name="ck_stock_observation_state"),
        CheckConstraint(
            "payload_schema_version = 1 AND payload_bytes BETWEEN 1 AND 131072",
            name="ck_stock_observation_payload",
        ),
        CheckConstraint(
            "length(payload_sha256) = 64 AND length(source_sha256) = 64 AND length(request_hash) = 64",
            name="ck_stock_observation_hashes",
        ),
        CheckConstraint("length(request_key) = 36", name="ck_stock_observation_request_key"),
        CheckConstraint(
            "length(trim(reason)) BETWEEN 1 AND 1000",
            name="ck_stock_observation_reason",
        ),
        CheckConstraint(
            "length(trim(observer_name)) BETWEEN 1 AND 120",
            name="ck_stock_observation_observer",
        ),
        CheckConstraint(
            "source_inventory_item_id > 0 AND source_part_id > 0",
            name="ck_stock_observation_source_ids",
        ),
        Index(
            "ix_stock_observation_company_piece",
            "company_id",
            "stock_piece_id",
            "observation_number",
        ),
    )
    id = Column(Integer, primary_key=True)
    stock_piece_id = Column(Integer, nullable=False)
    observation_number = Column(Integer, nullable=False)
    state = Column(String(16), nullable=False)
    reason = Column(Text, nullable=False)
    observed_at = Column(DateTime(timezone=True), nullable=False)
    observer_name = Column(String(120), nullable=False)
    payload_schema_version = Column(Integer, nullable=False)
    payload_json = Column(JSON, nullable=False)
    payload_sha256 = Column(String(64), nullable=False)
    payload_bytes = Column(Integer, nullable=False)
    # Evidence locators intentionally have no operational FK. Removing a source
    # must not erase history or prevent an honest withdrawal of its observation.
    source_inventory_item_id = Column(Integer, nullable=False)
    source_part_id = Column(Integer, nullable=False)
    source_snapshot_json = Column(JSON, nullable=False)
    source_sha256 = Column(String(64), nullable=False)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    submitted_api_token_id = Column(Integer, ForeignKey("api_tokens.id"), nullable=True)
    request_key = Column(String(36), nullable=False)
    request_hash = Column(String(64), nullable=False)


# Lock-step with migration104: these are database integrity controls, not material eligibility.
POSTGRES_DDL = {
    "stock_pieces": (
        "ALTER TABLE stock_pieces ENABLE ROW LEVEL SECURITY",
        "REVOKE ALL ON TABLE stock_pieces FROM PUBLIC",
        "REVOKE ALL ON SEQUENCE stock_pieces_id_seq FROM PUBLIC",
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN\n"
        "REVOKE ALL ON TABLE stock_pieces FROM anon; REVOKE ALL ON SEQUENCE stock_pieces_id_seq "
        "FROM anon;\n"
        "END IF; END $$",
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'authenticated') THEN\n"
        "REVOKE ALL ON TABLE stock_pieces FROM authenticated; REVOKE ALL ON SEQUENCE "
        "stock_pieces_id_seq FROM authenticated;\n"
        "END IF; END $$",
        "CREATE OR REPLACE FUNCTION stock_piece_identity_guard() RETURNS TRIGGER\n"
        "LANGUAGE plpgsql SET search_path = '' AS $$ DECLARE previous_exists boolean; BEGIN\n"
        "IF TG_OP IN ('DELETE', 'TRUNCATE') THEN\n"
        " RAISE EXCEPTION 'Stock piece identity is immutable; append a withdrawal.' USING "
        "ERRCODE='23514';\n"
        "END IF;\n"
        "IF TG_OP='INSERT' THEN\n"
        " IF NEW.version <> 1 OR NEW.latest_observation_number <> 1 THEN\n"
        "  RAISE EXCEPTION 'Stock piece observations must begin at revision 1.' USING "
        "ERRCODE='23514';\n"
        " END IF;\n"
        " RETURN NEW;\n"
        "END IF;\n"
        "IF NEW.id IS DISTINCT FROM OLD.id OR NEW.company_id IS DISTINCT FROM OLD.company_id\n"
        " OR NEW.label IS DISTINCT FROM OLD.label OR NEW.created_by IS DISTINCT FROM "
        "OLD.created_by\n"
        " OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN\n"
        " RAISE EXCEPTION 'Stock piece identity is immutable.' USING ERRCODE='23514';\n"
        "END IF;\n"
        "IF NEW.version <> OLD.version+1 OR NEW.latest_observation_number <> "
        "OLD.latest_observation_number+1 THEN\n"
        " RAISE EXCEPTION 'Stock piece updates require the next observation.' USING "
        "ERRCODE='23514';\n"
        "END IF;\n"
        "EXECUTE format('SELECT EXISTS(SELECT 1 FROM %I.stock_piece_observations\n"
        " WHERE company_id=$1 AND stock_piece_id=$2 AND observation_number=$3)', TG_TABLE_SCHEMA)\n"
        " INTO previous_exists USING OLD.company_id, OLD.id, OLD.latest_observation_number;\n"
        "IF NOT previous_exists THEN\n"
        " RAISE EXCEPTION 'The preceding stock observation is missing.' USING ERRCODE='23514';\n"
        "END IF;\n"
        "RETURN NEW;\n"
        "END; $$",
        "DROP TRIGGER IF EXISTS tr_stock_piece_identity_guard_insert ON stock_pieces",
        "CREATE TRIGGER tr_stock_piece_identity_guard_insert BEFORE INSERT ON stock_pieces FOR "
        "EACH ROW EXECUTE FUNCTION stock_piece_identity_guard()",
        "DROP TRIGGER IF EXISTS tr_stock_piece_identity_guard_update ON stock_pieces",
        "CREATE TRIGGER tr_stock_piece_identity_guard_update BEFORE UPDATE ON stock_pieces FOR "
        "EACH ROW EXECUTE FUNCTION stock_piece_identity_guard()",
        "DROP TRIGGER IF EXISTS tr_stock_piece_identity_guard_delete ON stock_pieces",
        "CREATE TRIGGER tr_stock_piece_identity_guard_delete BEFORE DELETE ON stock_pieces FOR "
        "EACH ROW EXECUTE FUNCTION stock_piece_identity_guard()",
        "DROP TRIGGER IF EXISTS tr_stock_piece_identity_guard_truncate ON stock_pieces",
        "CREATE TRIGGER tr_stock_piece_identity_guard_truncate BEFORE TRUNCATE ON stock_pieces FOR "
        "EACH STATEMENT EXECUTE FUNCTION stock_piece_identity_guard()",
    ),
    "stock_piece_observations": (
        "ALTER TABLE stock_piece_observations ENABLE ROW LEVEL SECURITY",
        "REVOKE ALL ON TABLE stock_piece_observations FROM PUBLIC",
        "REVOKE ALL ON SEQUENCE stock_piece_observations_id_seq FROM PUBLIC",
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN\n"
        "REVOKE ALL ON TABLE stock_piece_observations FROM anon; REVOKE ALL ON "
        "SEQUENCE stock_piece_observations_id_seq FROM anon;\n"
        "END IF; END $$",
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = "
        "'authenticated') THEN\n"
        "REVOKE ALL ON TABLE stock_piece_observations FROM authenticated; REVOKE ALL "
        "ON SEQUENCE stock_piece_observations_id_seq FROM authenticated;\n"
        "END IF; END $$",
        "CREATE OR REPLACE FUNCTION stock_observation_guard() RETURNS TRIGGER\n"
        "LANGUAGE plpgsql SET search_path = '' AS $$\n"
        "DECLARE parent_number integer; previous_record record; source_matches "
        "boolean; BEGIN\n"
        "IF TG_OP <> 'INSERT' THEN\n"
        " RAISE EXCEPTION 'Stock piece observations are immutable; append a new "
        "observation.' USING ERRCODE='23514';\n"
        "END IF;\n"
        "EXECUTE format('SELECT latest_observation_number FROM %I.stock_pieces\n"
        " WHERE company_id=$1 AND id=$2 FOR UPDATE', TG_TABLE_SCHEMA)\n"
        " INTO parent_number USING NEW.company_id, NEW.stock_piece_id;\n"
        "IF parent_number IS NULL OR parent_number <> NEW.observation_number THEN\n"
        " RAISE EXCEPTION 'Observation requires its current tenant piece counter.' "
        "USING ERRCODE='23514';\n"
        "END IF;\n"
        "IF NEW.observation_number > 1 THEN\n"
        " EXECUTE format('SELECT * FROM %I.stock_piece_observations\n"
        " WHERE company_id=$1 AND stock_piece_id=$2 AND observation_number=$3', "
        "TG_TABLE_SCHEMA)\n"
        " INTO previous_record USING NEW.company_id, NEW.stock_piece_id, "
        "NEW.observation_number-1;\n"
        " IF previous_record.id IS NULL THEN\n"
        "  RAISE EXCEPTION 'The preceding stock observation is missing.' USING "
        "ERRCODE='23514';\n"
        " END IF;\n"
        "END IF;\n"
        "IF NEW.state='RECORDED' THEN\n"
        " EXECUTE format('SELECT EXISTS(SELECT 1 FROM %I.inventory_items AS i JOIN "
        "%I.parts AS p ON p.id=i.part_id\n"
        " WHERE i.id=$1 AND p.id=$2 AND i.company_id=$3 AND p.company_id=$3 AND "
        "COALESCE(p.is_deleted,false)=false)',\n"
        " TG_TABLE_SCHEMA,TG_TABLE_SCHEMA)\n"
        " INTO source_matches USING NEW.source_inventory_item_id, NEW.source_part_id, "
        "NEW.company_id;\n"
        " IF NOT source_matches THEN\n"
        "  RAISE EXCEPTION 'Recorded source must match the tenant inventory item and "
        "Part.' USING ERRCODE='23514';\n"
        " END IF;\n"
        "ELSIF NEW.state='WITHDRAWN' THEN\n"
        " IF NEW.observation_number=1 THEN\n"
        "  RAISE EXCEPTION 'A first stock observation cannot be withdrawn.' USING "
        "ERRCODE='23514';\n"
        " END IF;\n"
        " IF previous_record.state <> 'RECORDED'\n"
        "  OR NEW.source_inventory_item_id IS DISTINCT FROM "
        "previous_record.source_inventory_item_id\n"
        "  OR NEW.source_part_id IS DISTINCT FROM previous_record.source_part_id\n"
        "  OR NEW.source_snapshot_json::jsonb IS DISTINCT FROM "
        "previous_record.source_snapshot_json::jsonb\n"
        "  OR NEW.source_sha256 IS DISTINCT FROM previous_record.source_sha256\n"
        "  OR NEW.payload_json::jsonb IS DISTINCT FROM "
        "previous_record.payload_json::jsonb\n"
        "  OR NEW.payload_sha256 IS DISTINCT FROM previous_record.payload_sha256\n"
        "  OR NEW.payload_bytes IS DISTINCT FROM previous_record.payload_bytes\n"
        "  OR NEW.payload_schema_version IS DISTINCT FROM "
        "previous_record.payload_schema_version THEN\n"
        "  RAISE EXCEPTION 'Withdrawal must preserve the preceding recorded source and "
        "measurements.' USING ERRCODE='23514';\n"
        " END IF;\n"
        "END IF;\n"
        "RETURN NEW;\n"
        "END; $$",
        "DROP TRIGGER IF EXISTS tr_stock_observation_guard_insert ON " "stock_piece_observations",
        "CREATE TRIGGER tr_stock_observation_guard_insert BEFORE INSERT ON "
        "stock_piece_observations FOR EACH ROW EXECUTE FUNCTION "
        "stock_observation_guard()",
        "DROP TRIGGER IF EXISTS tr_stock_observation_guard_update ON " "stock_piece_observations",
        "CREATE TRIGGER tr_stock_observation_guard_update BEFORE UPDATE ON "
        "stock_piece_observations FOR EACH ROW EXECUTE FUNCTION "
        "stock_observation_guard()",
        "DROP TRIGGER IF EXISTS tr_stock_observation_guard_delete ON " "stock_piece_observations",
        "CREATE TRIGGER tr_stock_observation_guard_delete BEFORE DELETE ON "
        "stock_piece_observations FOR EACH ROW EXECUTE FUNCTION "
        "stock_observation_guard()",
        "DROP TRIGGER IF EXISTS tr_stock_observation_guard_truncate ON " "stock_piece_observations",
        "CREATE TRIGGER tr_stock_observation_guard_truncate BEFORE TRUNCATE ON "
        "stock_piece_observations FOR EACH STATEMENT EXECUTE FUNCTION "
        "stock_observation_guard()",
    ),
}

SQLITE_DDL = {
    "stock_pieces": (
        "CREATE TRIGGER IF NOT EXISTS tr_stock_piece_insert BEFORE INSERT ON stock_pieces\n"
        "WHEN NEW.version <> 1 OR NEW.latest_observation_number <> 1\n"
        "BEGIN SELECT RAISE(ABORT, 'Stock piece observations must begin at revision 1.'); END",
        "CREATE TRIGGER IF NOT EXISTS tr_stock_piece_update BEFORE UPDATE ON stock_pieces\n"
        "WHEN NEW.id IS NOT OLD.id OR NEW.company_id IS NOT OLD.company_id OR NEW.label IS NOT "
        "OLD.label\n"
        " OR NEW.created_by IS NOT OLD.created_by OR NEW.created_at IS NOT OLD.created_at\n"
        " OR NEW.version <> OLD.version+1 OR NEW.latest_observation_number <> "
        "OLD.latest_observation_number+1\n"
        " OR NOT EXISTS(SELECT 1 FROM stock_piece_observations WHERE company_id=OLD.company_id\n"
        " AND stock_piece_id=OLD.id AND observation_number=OLD.latest_observation_number)\n"
        "BEGIN SELECT RAISE(ABORT, 'Stock piece identity is immutable; updates require the next "
        "observation.'); END",
        "CREATE TRIGGER IF NOT EXISTS tr_stock_piece_delete BEFORE DELETE ON stock_pieces\n"
        "BEGIN SELECT RAISE(ABORT, 'Stock piece identity is immutable; append a withdrawal.'); "
        "END",
    ),
    "stock_piece_observations": (
        "CREATE TRIGGER IF NOT EXISTS tr_stock_observation_insert BEFORE INSERT ON "
        "stock_piece_observations\n"
        "BEGIN\n"
        " SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM stock_pieces WHERE "
        "company_id=NEW.company_id AND id=NEW.stock_piece_id\n"
        " AND latest_observation_number=NEW.observation_number) THEN RAISE(ABORT, "
        "'Observation requires its current tenant piece counter.') END;\n"
        " SELECT CASE WHEN NEW.observation_number>1 AND NOT EXISTS(SELECT 1 FROM "
        "stock_piece_observations\n"
        " WHERE company_id=NEW.company_id AND stock_piece_id=NEW.stock_piece_id AND "
        "observation_number=NEW.observation_number-1)\n"
        " THEN RAISE(ABORT, 'The preceding stock observation is missing.') END;\n"
        " SELECT CASE WHEN NEW.state='RECORDED' AND NOT EXISTS(SELECT 1 FROM "
        "inventory_items AS i JOIN parts AS p ON p.id=i.part_id\n"
        " WHERE i.id=NEW.source_inventory_item_id AND p.id=NEW.source_part_id AND "
        "i.company_id=NEW.company_id\n"
        " AND p.company_id=NEW.company_id AND COALESCE(p.is_deleted,0)=0)\n"
        " THEN RAISE(ABORT, 'Recorded source must match the tenant inventory item and "
        "Part.') END;\n"
        " SELECT CASE WHEN NEW.state='WITHDRAWN' AND NOT EXISTS(SELECT 1 FROM "
        "stock_piece_observations AS prior\n"
        " WHERE prior.company_id=NEW.company_id AND "
        "prior.stock_piece_id=NEW.stock_piece_id\n"
        " AND prior.observation_number=NEW.observation_number-1 AND "
        "prior.state='RECORDED'\n"
        " AND NEW.source_inventory_item_id=prior.source_inventory_item_id AND "
        "NEW.source_part_id=prior.source_part_id\n"
        " AND NEW.source_snapshot_json=prior.source_snapshot_json AND "
        "NEW.source_sha256=prior.source_sha256\n"
        " AND NEW.payload_json=prior.payload_json AND "
        "NEW.payload_sha256=prior.payload_sha256\n"
        " AND NEW.payload_bytes=prior.payload_bytes AND "
        "NEW.payload_schema_version=prior.payload_schema_version)\n"
        " THEN RAISE(ABORT, 'Withdrawal must preserve the preceding recorded source "
        "and measurements.') END;\n"
        "END",
        "CREATE TRIGGER IF NOT EXISTS tr_stock_observation_update BEFORE UPDATE ON "
        "stock_piece_observations\n"
        "BEGIN SELECT RAISE(ABORT, 'Stock piece observations are immutable; append a "
        "new observation.'); END",
        "CREATE TRIGGER IF NOT EXISTS tr_stock_observation_delete BEFORE DELETE ON "
        "stock_piece_observations\n"
        "BEGIN SELECT RAISE(ABORT, 'Stock piece observations are immutable; append a "
        "new observation.'); END",
    ),
}


@event.listens_for(StockPieceObservation, "before_update")
@event.listens_for(StockPieceObservation, "before_delete")
def _refuse_observation_mutation(_mapper, _connection, _target):
    raise ValueError("Stock piece observations are immutable; append a new observation.")


@event.listens_for(StockPiece, "before_delete")
def _refuse_piece_deletion(_mapper, _connection, _target):
    raise ValueError("Stock piece identity is immutable; append a withdrawal.")


for _table in (StockPiece.__table__, StockPieceObservation.__table__):
    for _dialect, _statements in (
        ("postgresql", POSTGRES_DDL[_table.name]),
        ("sqlite", SQLITE_DDL[_table.name]),
    ):
        for _statement in _statements:
            event.listen(
                _table,
                "after_create",
                DDL(_statement.replace("%", "%%")).execute_if(dialect=_dialect),
            )
    _function = "stock_piece_identity_guard" if _table.name == "stock_pieces" else "stock_observation_guard"
    event.listen(
        _table,
        "after_drop",
        DDL(f"DROP FUNCTION IF EXISTS {_function}()").execute_if(dialect="postgresql"),
    )
