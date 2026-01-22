from sqlalchemy import Column, Integer, String, DateTime, Float, Text, ForeignKey, Enum as SQLEnum, Boolean, Date
from sqlalchemy.orm import relationship
from datetime import datetime, date
import enum
from app.db.database import Base


class TransactionType(str, enum.Enum):
    RECEIVE = "receive"  # Receiving from PO
    ISSUE = "issue"  # Issue to work order
    RETURN = "return"  # Return to stock
    ADJUST = "adjust"  # Inventory adjustment
    SCRAP = "scrap"  # Scrap/dispose
    TRANSFER = "transfer"  # Location transfer
    SHIP = "ship"  # Ship to customer
    COUNT = "count"  # Physical count adjustment


class LocationType(str, enum.Enum):
    WAREHOUSE = "warehouse"
    RACK = "rack"
    BIN = "bin"
    FLOOR = "floor"
    QUARANTINE = "quarantine"
    SHIPPING = "shipping"
    RECEIVING = "receiving"


class CycleCountStatus(str, enum.Enum):
    SCHEDULED = "scheduled"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class InventoryLocation(Base):
    """Warehouse locations/bins"""
    __tablename__ = "inventory_locations"
    
    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(50), unique=True, index=True, nullable=False)  # e.g., WH1-A-01-01
    name = Column(String(255))
    
    # Hierarchy
    warehouse = Column(String(50), nullable=False)
    zone = Column(String(50))  # A, B, C (for ABC analysis)
    aisle = Column(String(20))
    rack = Column(String(20))
    shelf = Column(String(20))
    bin = Column(String(20))
    
    location_type = Column(SQLEnum(LocationType), default=LocationType.BIN)
    
    # Capacity
    max_quantity = Column(Float, nullable=True)
    max_weight = Column(Float, nullable=True)
    
    # Status
    is_active = Column(Boolean, default=True)
    is_pickable = Column(Boolean, default=True)  # Can pick from this location
    is_receivable = Column(Boolean, default=True)  # Can receive to this location
    
    # Cycle count
    last_count_date = Column(Date, nullable=True)
    count_frequency_days = Column(Integer, default=90)  # How often to count
    
    created_at = Column(DateTime, default=datetime.utcnow)


class CycleCount(Base):
    """Cycle count session"""
    __tablename__ = "cycle_counts"
    
    id = Column(Integer, primary_key=True, index=True)
    count_number = Column(String(50), unique=True, index=True, nullable=False)
    
    # Scope
    location_id = Column(Integer, ForeignKey("inventory_locations.id"), nullable=True)
    warehouse = Column(String(50), nullable=True)  # Count entire warehouse
    part_id = Column(Integer, ForeignKey("parts.id"), nullable=True)  # Count specific part
    
    # Status
    status = Column(SQLEnum(CycleCountStatus), default=CycleCountStatus.SCHEDULED)
    scheduled_date = Column(Date, nullable=False)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    
    # Results
    total_items = Column(Integer, default=0)
    items_counted = Column(Integer, default=0)
    items_adjusted = Column(Integer, default=0)
    total_variance_value = Column(Float, default=0.0)
    
    # Assignment
    assigned_to = Column(Integer, ForeignKey("users.id"), nullable=True)
    completed_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    
    notes = Column(Text)
    
    created_at = Column(DateTime, default=datetime.utcnow)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    
    # Relationships
    items = relationship("CycleCountItem", back_populates="cycle_count", cascade="all, delete-orphan")


class CycleCountItem(Base):
    """Individual item in a cycle count"""
    __tablename__ = "cycle_count_items"
    
    id = Column(Integer, primary_key=True, index=True)
    cycle_count_id = Column(Integer, ForeignKey("cycle_counts.id"), nullable=False)
    inventory_item_id = Column(Integer, ForeignKey("inventory_items.id"), nullable=False)
    
    # Expected vs Actual
    system_quantity = Column(Float, nullable=False)  # What system shows
    counted_quantity = Column(Float, nullable=True)  # What was counted
    variance = Column(Float, nullable=True)  # counted - system
    
    # Cost impact
    unit_cost = Column(Float, default=0.0)
    variance_value = Column(Float, default=0.0)
    
    # Status
    is_counted = Column(Boolean, default=False)
    requires_recount = Column(Boolean, default=False)
    
    notes = Column(Text)
    counted_at = Column(DateTime, nullable=True)
    counted_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    
    # Relationships
    cycle_count = relationship("CycleCount", back_populates="items")
    inventory_item = relationship("InventoryItem")


class InventoryItem(Base):
    """Inventory on hand - tracks quantity at location"""
    __tablename__ = "inventory_items"
    
    id = Column(Integer, primary_key=True, index=True)
    part_id = Column(Integer, ForeignKey("parts.id"), nullable=False, index=True)
    
    # Location
    location = Column(String(100), nullable=False)  # Warehouse/bin location
    warehouse = Column(String(50), default="MAIN")
    
    # Quantity
    quantity_on_hand = Column(Float, default=0.0)
    quantity_allocated = Column(Float, default=0.0)  # Reserved for work orders
    quantity_available = Column(Float, default=0.0)  # on_hand - allocated
    
    # Lot/Serial tracking for AS9100D traceability
    lot_number = Column(String(100), index=True)
    serial_number = Column(String(100), index=True)
    
    # Receiving info
    received_date = Column(DateTime)
    supplier_id = Column(Integer, nullable=True)
    po_number = Column(String(100))
    
    # Certificate/Documentation for compliance
    cert_number = Column(String(100))
    heat_lot = Column(String(100))  # Material heat lot
    
    # Expiration for shelf-life items
    expiration_date = Column(DateTime, nullable=True)
    
    # Cost tracking
    unit_cost = Column(Float, default=0.0)
    
    # Status
    status = Column(String(50), default="available")  # available, on_hold, quarantine, rejected
    is_active = Column(Boolean, default=True)
    
    # Audit fields
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    part = relationship("Part", back_populates="inventory_items")


class InventoryTransaction(Base):
    """Transaction history for inventory movements"""
    __tablename__ = "inventory_transactions"
    
    id = Column(Integer, primary_key=True, index=True)
    inventory_item_id = Column(Integer, ForeignKey("inventory_items.id"), nullable=True)
    part_id = Column(Integer, ForeignKey("parts.id"), nullable=False, index=True)
    
    # Transaction details
    transaction_type = Column(SQLEnum(TransactionType), nullable=False, index=True)
    quantity = Column(Float, nullable=False)  # Positive for in, negative for out
    
    # Reference
    reference_type = Column(String(50))  # work_order, purchase_order, sales_order
    reference_id = Column(Integer)
    reference_number = Column(String(100))
    
    # Location
    from_location = Column(String(100))
    to_location = Column(String(100))
    
    # Lot tracking
    lot_number = Column(String(100))
    serial_number = Column(String(100))
    
    # Cost at time of transaction
    unit_cost = Column(Float, default=0.0)
    total_cost = Column(Float, default=0.0)
    
    # Notes
    notes = Column(Text)
    reason_code = Column(String(100))
    
    # Audit fields - CMMC Level 2 requires full audit trail
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    
    # Relationships
    user = relationship("User")
    part = relationship("Part")
