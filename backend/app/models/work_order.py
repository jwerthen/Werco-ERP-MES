from sqlalchemy import Column, Integer, String, Boolean, DateTime, Enum as SQLEnum, Float, Text, ForeignKey, Date
from sqlalchemy.orm import relationship
from datetime import datetime, date
import enum
from app.db.database import Base


class WorkOrderStatus(str, enum.Enum):
    DRAFT = "draft"
    RELEASED = "released"
    IN_PROGRESS = "in_progress"
    ON_HOLD = "on_hold"
    COMPLETE = "complete"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class OperationStatus(str, enum.Enum):
    PENDING = "pending"
    READY = "ready"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    ON_HOLD = "on_hold"


class WorkOrder(Base):
    """Manufacturing Work Order / Job"""
    __tablename__ = "work_orders"
    
    id = Column(Integer, primary_key=True, index=True)
    work_order_number = Column(String(50), unique=True, index=True, nullable=False)
    
    # Part/Assembly being made
    part_id = Column(Integer, ForeignKey("parts.id"), nullable=False)
    quantity_ordered = Column(Float, nullable=False)
    quantity_complete = Column(Float, default=0.0)
    quantity_scrapped = Column(Float, default=0.0)
    
    # Status tracking
    status = Column(SQLEnum(WorkOrderStatus), default=WorkOrderStatus.DRAFT)
    priority = Column(Integer, default=5)  # 1=highest, 10=lowest
    
    # Scheduling
    scheduled_start = Column(DateTime, nullable=True)
    scheduled_end = Column(DateTime, nullable=True)
    actual_start = Column(DateTime, nullable=True)
    actual_end = Column(DateTime, nullable=True)
    due_date = Column(Date, nullable=True)
    must_ship_by = Column(Date, nullable=True)  # "Must Leave By" date
    
    # Customer/Sales Order reference
    customer_name = Column(String(255))
    customer_po = Column(String(100))
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
    operations = relationship("WorkOrderOperation", back_populates="work_order", order_by="WorkOrderOperation.sequence")
    time_entries = relationship("TimeEntry", back_populates="work_order")


class WorkOrderOperation(Base):
    """Individual operation/step in a work order routing"""
    __tablename__ = "work_order_operations"
    
    id = Column(Integer, primary_key=True, index=True)
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
