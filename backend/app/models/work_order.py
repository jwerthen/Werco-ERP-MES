import enum
from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, Column, Date, DateTime
from sqlalchemy import Enum as SQLEnum
from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from app.db.database import Base
from app.db.mixins import SoftDeleteMixin, TenantMixin


class WorkOrderStatus(str, enum.Enum):
    DRAFT = "draft"
    RELEASED = "released"
    IN_PROGRESS = "in_progress"
    ON_HOLD = "on_hold"
    COMPLETE = "complete"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class WorkOrderType(str, enum.Enum):
    PRODUCTION = "production"
    LASER_CUTTING = "laser_cutting"


class OperationStatus(str, enum.Enum):
    PENDING = "pending"
    READY = "ready"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    ON_HOLD = "on_hold"


class WorkOrder(Base, SoftDeleteMixin, TenantMixin):
    """Manufacturing Work Order / Job"""

    __tablename__ = "work_orders"
    __table_args__ = (
        UniqueConstraint('company_id', 'work_order_number', name='uq_work_orders_company_wo_number'),
        # part_id is nullable ONLY for standalone laser-cutting nest WOs (sheet-run
        # jobs born from an Ermaksan nest package, no finished-good part). Every
        # other work_order_type still requires a part -- enforced here at the model
        # level so create_all test DBs / fresh bootstraps carry the CHECK, and
        # mirrored byte-identically in Alembic migration 067 for migrated Postgres.
        CheckConstraint(
            "part_id IS NOT NULL OR work_order_type = 'laser_cutting'",
            name="ck_work_orders_part_required_unless_laser",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    work_order_number = Column(String(50), index=True, nullable=False)

    # Part/Assembly being made. NULLABLE only for work_order_type='laser_cutting'
    # (standalone nest WOs) -- see the table CHECK constraint above.
    part_id = Column(Integer, ForeignKey("parts.id"), nullable=True)
    parent_work_order_id = Column(Integer, ForeignKey("work_orders.id"), nullable=True, index=True)
    work_order_type = Column(String(50), default=WorkOrderType.PRODUCTION.value, nullable=False, index=True)
    quantity_ordered = Column(Float, nullable=False)
    quantity_complete = Column(Float, default=0.0)
    quantity_scrapped = Column(Float, default=0.0)
    scrap_reason = Column(String(255), nullable=True)
    # Lean Phase 1: structured scrap categorization. Nullable -- historical rows and
    # scrap=0 writes have no code; the free-text scrap_reason stays as narrative detail.
    scrap_reason_code_id = Column(Integer, ForeignKey("scrap_reason_codes.id"), nullable=True)

    # Status tracking
    status = Column(SQLEnum(WorkOrderStatus), default=WorkOrderStatus.DRAFT, index=True)
    priority = Column(Integer, default=5, index=True)  # 1=highest, 10=lowest

    # Scheduling
    scheduled_start = Column(DateTime, nullable=True)
    scheduled_end = Column(DateTime, nullable=True)
    actual_start = Column(DateTime, nullable=True)
    actual_end = Column(DateTime, nullable=True)
    due_date = Column(Date, nullable=True, index=True)
    must_ship_by = Column(Date, nullable=True)  # "Must Leave By" date

    # Customer/Sales Order reference
    customer_name = Column(String(255))
    customer_po = Column(String(100))
    po_line_item = Column(String(50), nullable=True)
    po_date = Column(Date, nullable=True)
    sales_order_id = Column(Integer, nullable=True)

    # Lot/Serial tracking for AS9100D traceability
    lot_number = Column(String(100), index=True)
    serial_numbers = Column(Text)  # JSON array for serialized items

    # Notes
    notes = Column(Text)
    special_instructions = Column(Text)

    # Current operation tracking
    current_operation_id = Column(Integer, nullable=True)

    # Costing
    estimated_hours = Column(Float, default=0.0)
    actual_hours = Column(Float, default=0.0)
    estimated_cost = Column(Float, default=0.0)
    actual_cost = Column(Float, default=0.0)

    # Audit fields
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_by = Column(Integer, nullable=True)
    released_by = Column(Integer, nullable=True)
    released_at = Column(DateTime, nullable=True)

    # Relationships
    part = relationship("Part")
    parent_work_order = relationship("WorkOrder", remote_side=[id], backref="child_work_orders")
    operations = relationship("WorkOrderOperation", back_populates="work_order", order_by="WorkOrderOperation.sequence")
    time_entries = relationship("TimeEntry", back_populates="work_order")
    scrap_reason_code = relationship("ScrapReasonCode", foreign_keys=[scrap_reason_code_id])


class WorkOrderOperation(Base, TenantMixin):
    """Individual operation/step in a work order routing"""

    __tablename__ = "work_order_operations"
    # Lock-step with migration 042_wo_completion_perf_indexes: backs
    # has_incomplete_predecessors (WHERE work_order_id=? AND sequence<?) and
    # release_next_ready_operation (WHERE work_order_id=? ORDER BY sequence) in
    # app/services/work_order_state_service.py.
    __table_args__ = (Index("ix_woo_work_order_sequence", "work_order_id", "sequence"),)

    id = Column(Integer, primary_key=True, index=True)

    # Optimistic locking (Batch 2 / SFI-2 / LOCK-1). The ``version`` column was
    # added at the DB level by migration ``004_add_optimistic_locking`` but was
    # never mapped, leaving locking inert. We map it here (scoped to this
    # completion-path model rather than the shared OptimisticLockMixin) so
    # SQLAlchemy enforces ``version_id_col`` on UPDATE: a concurrent stale write
    # to the same operation row raises StaleDataError, which the endpoint layer
    # translates to HTTP 409. Requires every row to have a non-null version;
    # migration 004 set server_default='1' and the Batch 2 migration backfills
    # any residual NULLs and re-asserts NOT NULL + server_default.
    version = Column(Integer, nullable=False, server_default="1", default=1)
    __mapper_args__ = {"version_id_col": version}

    work_order_id = Column(Integer, ForeignKey("work_orders.id"), nullable=False)
    work_center_id = Column(Integer, ForeignKey("work_centers.id"), nullable=False)

    # Component tracking (for assembly WOs with BOM)
    component_part_id = Column(Integer, ForeignKey("parts.id"), nullable=True)
    component_quantity = Column(Float, default=0.0)  # Qty of this component needed

    # Operation details
    sequence = Column(Integer, nullable=False)  # 10, 20, 30...
    operation_number = Column(String(20))  # OP10, OP20...
    name = Column(String(255), nullable=False)
    description = Column(Text)

    # Grouping for batch operations
    operation_group = Column(String(50), nullable=True)  # e.g., "LASER", "BEND", "WELD"

    # Work instructions
    setup_instructions = Column(Text)
    run_instructions = Column(Text)

    # Time estimates
    setup_time_hours = Column(Float, default=0.0)
    run_time_hours = Column(Float, default=0.0)
    run_time_per_piece = Column(Float, default=0.0)

    # Actual time tracking
    actual_setup_hours = Column(Float, default=0.0)
    actual_run_hours = Column(Float, default=0.0)

    # Status
    status = Column(SQLEnum(OperationStatus), default=OperationStatus.PENDING)
    quantity_complete = Column(Float, default=0.0)
    quantity_scrapped = Column(Float, default=0.0)
    # Lean Phase 1: rework quantity alongside complete/scrapped. server_default so
    # pre-existing rows read as 0 rather than NULL (migration 063 backfills via DEFAULT).
    quantity_reworked = Column(Float, default=0.0, server_default="0")
    scrap_reason = Column(String(255), nullable=True)
    # Lean Phase 1: structured scrap categorization. Nullable -- historical rows and
    # scrap=0 writes have no code; the free-text scrap_reason stays as narrative detail.
    scrap_reason_code_id = Column(Integer, ForeignKey("scrap_reason_codes.id"), nullable=True)

    # Scheduling
    scheduled_start = Column(DateTime, nullable=True)
    scheduled_end = Column(DateTime, nullable=True)
    actual_start = Column(DateTime, nullable=True)
    actual_end = Column(DateTime, nullable=True)

    # Quality requirements
    requires_inspection = Column(Boolean, default=False)
    inspection_type = Column(String(100))  # first_article, in_process, final
    inspection_complete = Column(Boolean, default=False)

    # Audit fields
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    started_by = Column(Integer, nullable=True)
    completed_by = Column(Integer, nullable=True)

    # Relationships
    work_order = relationship("WorkOrder", back_populates="operations")
    work_center = relationship("WorkCenter", back_populates="operations")
    time_entries = relationship("TimeEntry", back_populates="operation")
    component_part = relationship("Part", foreign_keys=[component_part_id])
    laser_nest = relationship("LaserNest", back_populates="operation", uselist=False)
    scrap_reason_code = relationship("ScrapReasonCode", foreign_keys=[scrap_reason_code_id])
