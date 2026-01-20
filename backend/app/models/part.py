from sqlalchemy import Column, Integer, String, Boolean, DateTime, Enum as SQLEnum, Float, Text, ForeignKey
from sqlalchemy.orm import relationship
from datetime import datetime
import enum
from app.db.database import Base
from app.db.mixins import SoftDeleteMixin


class PartType(str, enum.Enum):
    MANUFACTURED = "manufactured"  # Parts we make
    PURCHASED = "purchased"  # Off the shelf / buy parts
    ASSEMBLY = "assembly"  # Assemblies we build
    RAW_MATERIAL = "raw_material"  # Raw stock material (sheets, bars, etc.)
    HARDWARE = "hardware"  # COTS hardware (bolts, nuts, washers, fasteners)
    CONSUMABLE = "consumable"  # Consumables (adhesives, lubricants, etc.)


class UnitOfMeasure(str, enum.Enum):
    EACH = "each"
    FEET = "feet"
    INCHES = "inches"
    POUNDS = "pounds"
    KILOGRAMS = "kilograms"
    SHEETS = "sheets"
    GALLONS = "gallons"
    LITERS = "liters"


class Part(Base, SoftDeleteMixin):
    __tablename__ = "parts"
    
    id = Column(Integer, primary_key=True, index=True)
    part_number = Column(String(100), unique=True, index=True, nullable=False)
    revision = Column(String(20), default="A")
    name = Column(String(255), nullable=False)
    description = Column(Text)
    part_type = Column(SQLEnum(PartType), nullable=False)
    unit_of_measure = Column(SQLEnum(UnitOfMeasure), default=UnitOfMeasure.EACH)
    
    # Costing
    standard_cost = Column(Float, default=0.0)
    material_cost = Column(Float, default=0.0)
    labor_cost = Column(Float, default=0.0)
    overhead_cost = Column(Float, default=0.0)
    
    # Lead times (in days)
    lead_time_days = Column(Integer, default=0)
    
    # Inventory settings
    safety_stock = Column(Float, default=0.0)
    reorder_point = Column(Float, default=0.0)
    reorder_quantity = Column(Float, default=0.0)
    
    # Classification for AS9100D
    is_critical = Column(Boolean, default=False)  # Critical characteristic tracking
    requires_inspection = Column(Boolean, default=True)
    inspection_requirements = Column(Text)
    
    # Status
    is_active = Column(Boolean, default=True)
    status = Column(String(50), default="active")  # active, obsolete, pending_approval
    
    # Customer/Supplier info
    customer_name = Column(String(255), nullable=True)
    customer_part_number = Column(String(100))
    primary_supplier_id = Column(Integer, nullable=True)
    
    # Drawing/Document references
    drawing_number = Column(String(100))
    
    # Audit fields
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_by = Column(Integer, nullable=True)
    
    # Relationships
    bom = relationship("BOM", back_populates="part", uselist=False)
    inventory_items = relationship("InventoryItem", back_populates="part")
    documents = relationship("Document", back_populates="part")
