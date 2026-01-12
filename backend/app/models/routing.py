from sqlalchemy import Column, Integer, String, Boolean, DateTime, Float, Text, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime
from app.db.database import Base
from app.db.mixins import SoftDeleteMixin


class Routing(Base, SoftDeleteMixin):
    """Master routing for a part - defines standard manufacturing process"""
    __tablename__ = "routings"
    
    id = Column(Integer, primary_key=True, index=True)
    part_id = Column(Integer, ForeignKey("parts.id"), nullable=False, index=True)
    revision = Column(String(20), default="A")
    description = Column(Text)
    
    # Status
    status = Column(String(50), default="draft")  # draft, released, obsolete
    is_active = Column(Boolean, default=True)
    
    # Effective dates
    effective_date = Column(DateTime, nullable=True)
    obsolete_date = Column(DateTime, nullable=True)
    
    # Totals (calculated)
    total_setup_hours = Column(Float, default=0.0)
    total_run_hours_per_unit = Column(Float, default=0.0)
    total_labor_cost = Column(Float, default=0.0)
    total_overhead_cost = Column(Float, default=0.0)
    
    # Audit
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    approved_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    
    # Relationships
    part = relationship("Part", backref="routing")
    operations = relationship("RoutingOperation", back_populates="routing", cascade="all, delete-orphan", order_by="RoutingOperation.sequence")


class RoutingOperation(Base):
    """Individual operation step in a routing"""
    __tablename__ = "routing_operations"
    
    id = Column(Integer, primary_key=True, index=True)
    routing_id = Column(Integer, ForeignKey("routings.id"), nullable=False, index=True)
    
    # Operation identification
    sequence = Column(Integer, nullable=False)  # 10, 20, 30, etc.
    operation_number = Column(String(20))  # Op 10, Op 20, etc.
    name = Column(String(255), nullable=False)
    description = Column(Text)
    
    # Work center assignment
    work_center_id = Column(Integer, ForeignKey("work_centers.id"), nullable=False)
    
    # Time standards (in hours)
    setup_hours = Column(Float, default=0.0)  # One-time setup per batch
    run_hours_per_unit = Column(Float, default=0.0)  # Time per piece
    move_hours = Column(Float, default=0.0)  # Transport to next operation
    queue_hours = Column(Float, default=0.0)  # Wait time before operation
    
    # For machine operations
    cycle_time_seconds = Column(Float, nullable=True)  # Machine cycle time
    pieces_per_cycle = Column(Integer, default=1)
    
    # Costing
    labor_rate_override = Column(Float, nullable=True)  # Override work center rate
    overhead_rate = Column(Float, default=0.0)
    
    # Quality requirements
    is_inspection_point = Column(Boolean, default=False)
    inspection_instructions = Column(Text)
    
    # Documentation
    work_instructions = Column(Text)
    setup_instructions = Column(Text)
    
    # Tool/fixture requirements
    tooling_requirements = Column(Text)
    fixture_requirements = Column(Text)
    
    # Outside processing
    is_outside_operation = Column(Boolean, default=False)
    vendor_id = Column(Integer, nullable=True)
    outside_cost = Column(Float, default=0.0)
    outside_lead_days = Column(Integer, default=0)
    
    # Status
    is_active = Column(Boolean, default=True)
    
    # Audit
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    routing = relationship("Routing", back_populates="operations")
    work_center = relationship("WorkCenter")
